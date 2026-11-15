#!/usr/bin/env python3
"""Offline VOC-LS E0 exact PathWyse benchmark with separated timing and diagnostics.

No third-party Python deps. Linux/Windows host used only for file transfer; execute Linux.
"""
from __future__ import annotations
import argparse
import collections
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime,timezone

KEYS=["instance_id","group","source","split","profile","phase","repeat","status","objective","tour","consumption","wall_s","wall_ns","solver_s","model_used","inference_calls","inference_time_ns","exit_code","timed_out","log_path"]
ITER_KEYS=["instance_id","group","source","split","profile","repeat","iteration","extensions_fw","extensions_bw","extensions_total","dom_checks_fw","dom_checks_bw","dom_checks_total","dom_probe_time_ns","inference_calls","inference_time_ns","nlabels_inserted","ndominated","nclosed","ins_attempts_fw","ins_attempts_bw","njoin","algo_iteration_s"]
TOTAL_KEYS=["instance_id","group","source","split","profile","repeat","iterations","extensions_total","dom_checks_total","dom_probe_time_ns","inference_calls","inference_time_ns","nlabels_inserted_sum","ndominated_sum","nclosed_sum","ins_attempts_sum","njoin_sum","diagnostic_wall_s"]
SUMMARY_KEYS=["instance_id","group","source","split","profile","successful_runs","total_runs","median_wall_s","mean_wall_s","p95_wall_s","sd_wall_s","median_solver_s","objective","extensions_total_diagnostic","dom_checks_diagnostic","inference_calls_diagnostic","inference_time_ns_diagnostic","speedup_over_B0_R"]

PROFILES={"B0_R":{"algo/default/parallel":"0","algo/default/dssr":"restricted"},
          "B0_Strong":{"algo/default/parallel":"1","algo/default/dssr":"standard"}}
BASE={"verbosity":"1","algo_names":"PWDefault","algo_active":"1","algo_parallel":"0",
      "algo/default/autoconfig":"0","algo/default/search":"bidirectional","algo/default/candidate/type":"node",
      "algo/default/ng":"off","algo/default/timelimit":"0","algo/requested_solutions":"1"}

NUMBER=r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

def utc():return datetime.now(timezone.utc).isoformat()
def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def source_hash(root):
    h=hashlib.sha256()
    for p in sorted((root/'src').rglob('*')):
        if p.is_file() and p.suffix in {'.h','.cpp','.hpp'}:
            h.update(str(p.relative_to(root)).encode());h.update(bytes.fromhex(sha_file(p)))
    for f in ['CMakeLists.txt','src/CMakeLists.txt','pathwyse.set']:
        p=root/f
        if p.exists():h.update(f.encode());h.update(bytes.fromhex(sha_file(p)))
    return h.hexdigest()
def write_json(path,x):path.write_text(json.dumps(x,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
def write_csv(path,fields,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
def read_csv(path):
    if not path.is_file():return []
    with path.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))

def settings_file(src,dest,profile,diagnostic):
    if profile not in PROFILES:raise ValueError(profile)
    mods={**BASE,**PROFILES[profile],"data_collection/level":"0" if diagnostic else "-1","output/write":"1" if diagnostic else "0"}
    text=src.read_text(encoding='utf-8');out=[];seen=set()
    for line in text.splitlines():
        m=re.match(r'^\s*([^=#\s][^=#]*?)\s*=\s*',line)
        k=m.group(1).strip() if m else None
        if k in mods:
            if k in seen:raise ValueError('Duplicate setting '+k)
            seen.add(k);out.append(f'{k} = {mods[k]}')
        else:out.append(line)
    for k in sorted(mods.keys()-seen):out.append(f'{k} = {mods[k]}')
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text('\n'.join(out)+'\n',encoding='utf-8')
    return {k:mods[k] for k in mods}

def check_config_pair(a,b):
    # Ensure only parallel and DSSR differ between B0_R and B0_Strong.
    return sorted(k for k in a if a[k]!=b[k])==['algo/default/dssr','algo/default/parallel']

def parse_output(text):
    text=re.sub(r'\x1b\[[0-9;]*[a-zA-Z]','',text)
    def get(k):
        m=re.findall(r'^\s*'+re.escape(k)+r'\s*:\s*(.*?)\s*$',text,re.M|re.I)
        return m[-1].strip() if m else None
    status=get('Solution Status'); obj=get('Obj');solver=get('PWDefault Time')
    def num(s):
        m=re.match(NUMBER,s) if s else None
        return float(m.group()) if m else None
    return {'status':status,'objective':num(obj),'solver_s':num(solver),'tour':get('Tour'), 'consumption':get('Consumption')}

def command(cmd,cwd,log,timeout_s,env,cpus):
    log.parent.mkdir(parents=True,exist_ok=True)
    preexec=(lambda: os.sched_setaffinity(0,set(cpus))) if cpus else None
    with log.open('w',encoding='utf-8',errors='replace') as f:
        f.write('# cwd: '+str(cwd)+'\n# command: '+subprocess.list2cmdline(cmd)+'\n');f.flush()
        t0=time.perf_counter_ns()
        p=subprocess.Popen(cmd,cwd=str(cwd),stdout=f,stderr=subprocess.STDOUT,env=env,
                           start_new_session=True,preexec_fn=preexec)
        timed=threading.Event()
        def kill():
            if p.poll() is None:
                timed.set()
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        watchdog=threading.Timer(timeout_s,kill);watchdog.daemon=True;watchdog.start()
        try:code=p.wait() # Blocking waitpid, not Popen.wait(timeout=...) polling
        finally:watchdog.cancel()
        ns=time.perf_counter_ns()-t0
        if timed.is_set():f.write(f'\n[E0] TIMEOUT after {timeout_s}s\n')
    return code,ns,timed.is_set()

def verify_synthetic_or_toy_path(instance:Path, solution:dict):
    """Independently reconstruct objective/resource for textual one-CAP PathWyse graphs.

    Native SPPRCLIB depot-duplication semantics are different; verify only synthetic + toy.
    """
    lines=instance.read_text(encoding='utf-8').splitlines()
    sections={};simple={};idx=0
    while idx<len(lines):
        line=lines[idx].strip();idx+=1
        if not line or line.startswith('#'):continue
        if ':' in line:
            a,b=line.split(':',1);simple[a.strip()]=b.strip();continue
        if line in ('EDGE_COST','NODE_COST','NODE_CONSUMPTION','RES_BOUND'):
            vals=[]
            while idx<len(lines) and lines[idx].strip()!='END':
                vals.append(lines[idx].strip().split());idx+=1
            idx+=1;sections[line]=vals
    edges={(int(v[0]),int(v[1])):int(v[2]) for v in sections['EDGE_COST']}
    costs={int(v[0]):int(v[1]) for v in sections['NODE_COST']}
    cons={int(v[1]):int(v[2]) for v in sections['NODE_CONSUMPTION'] if v[0]=='0'}
    bounds=[x for x in sections['RES_BOUND'] if x[0]=='0']
    tour=[int(v) for v in solution['tour'].split()]
    if not tour or tour[0]!=int(simple['ORIGIN']) or tour[-1]!=int(simple['DESTINATION']):
        raise RuntimeError(f'Incorrect path endpoints for {instance}: {tour}')
    if len(tour)!=len(set(tour)):raise RuntimeError(f'Non-elementary tour for {instance}')
    obj=sum(costs.get(n,0) for n in tour)
    for u,v in zip(tour,tour[1:]):
        if (u,v) not in edges:raise RuntimeError(f'Tour arc {u}->{v} missing from {instance}')
        obj+=edges[(u,v)]
    res=sum(cons.get(n,0) for n in tour)
    if not bounds or not (int(bounds[0][1])<=res<=int(bounds[0][2])):
        raise RuntimeError(f'Capacity violation {res}: {instance}')
    if not math.isclose(float(solution['objective']),float(obj),abs_tol=1e-6,rel_tol=0):
        raise RuntimeError(f'Objective mismatch for {instance}: reported={solution["objective"]}, reconstructed={obj}')
    observed=float(solution['consumption'].split()[0])
    if not math.isclose(observed,res,abs_tol=1e-6,rel_tol=0):
        raise RuntimeError(f'Resource mismatch for {instance}: reported={observed}, reconstructed={res}')

def parse_diagnostics(cwd):
    # Distinct cwd for every diagnostic run isolates PathWyse native output files.
    roots=sorted((cwd/'output').rglob('PWDefault.csv'))
    if len(roots)!=1:raise ValueError(f'Expected exactly 1 PWDefault.csv, got {len(roots)} in {cwd}')
    algo=read_csv(roots[0]);lm=read_csv(roots[0].with_name('PWDefault_LM.csv'))
    if len(algo)!=len(lm) or not algo:raise ValueError('Algorithm and LM iteration record count mismatch')
    required={'e0_dom_checks_fw','e0_dom_checks_bw','e0_dom_time_fw_ns','e0_dom_time_bw_ns','e0_inference_calls','e0_inference_time_ns'}
    if not required.issubset(set(lm[0])):raise ValueError('Patched E0 profile counters are missing: '+str(sorted(required-set(lm[0]))))
    def integer(record,key):return int(round(float(record[key])))
    iterations=[]
    for a,m in zip(algo,lm):
        if integer(a,'iterations')!=integer(m,'iterations'):raise ValueError('DSSR iteration alignment mismatch')
        r={'iteration':integer(a,'iterations'),'extensions_fw':integer(a,'it_ext_fw'),'extensions_bw':integer(a,'it_ext_bw'),
           'dom_checks_fw':integer(m,'e0_dom_checks_fw'),'dom_checks_bw':integer(m,'e0_dom_checks_bw'),
           'dom_probe_time_ns':integer(m,'e0_dom_time_fw_ns')+integer(m,'e0_dom_time_bw_ns'),
           'inference_calls':integer(m,'e0_inference_calls'),'inference_time_ns':integer(m,'e0_inference_time_ns'),
           'nlabels_inserted':integer(m,'nlabels_inserted'),'ndominated':integer(m,'ndominated'),
           'nclosed':integer(m,'nclosed'),'ins_attempts_fw':integer(m,'ins_attempts_fw'),
           'ins_attempts_bw':integer(m,'ins_attempts_bw'),'njoin':integer(m,'njoin'),
           'algo_iteration_s':float(a['time'])}
        r['extensions_total']=r['extensions_fw']+r['extensions_bw'];r['dom_checks_total']=r['dom_checks_fw']+r['dom_checks_bw']
        iterations.append(r)
    return iterations

def generate_summary(timing,totals):
    diag={(r['instance_id'],r['profile']):r for r in totals}
    keys=sorted(set((r['instance_id'],r['profile']) for r in timing))
    summary=[]
    for key in keys:
        grp=[r for r in timing if (r['instance_id'],r['profile'])==key]
        good=[r for r in grp if r['status']=='Optimal' and int(r['exit_code'])==0 and str(r['timed_out']).lower()=='false']
        seq=sorted(float(r['wall_s']) for r in good)
        def quantile(q):
            if not seq:return None
            pos=(len(seq)-1)*q;lo=int(math.floor(pos));hi=int(math.ceil(pos))
            return seq[lo]+(seq[hi]-seq[lo])*(pos-lo)
        d=diag.get(key,{})
        out={'instance_id':key[0],'profile':key[1], 'group':grp[0]['group'],'source':grp[0]['source'],'split':grp[0]['split'],
             'successful_runs':len(good),'total_runs':len(grp),
             'median_wall_s':quantile(.5),'mean_wall_s':statistics.mean(seq) if seq else None,
             'p95_wall_s':quantile(.95),'sd_wall_s':statistics.stdev(seq) if len(seq)>1 else None,
             'median_solver_s':statistics.median(float(r['solver_s']) for r in good if r['solver_s'] not in (None,'','None')) if good else None,
             'objective':good[0]['objective'] if good else None,
             'extensions_total_diagnostic':d.get('extensions_total'), 'dom_checks_diagnostic':d.get('dom_checks_total'),
             'inference_calls_diagnostic':d.get('inference_calls'),'inference_time_ns_diagnostic':d.get('inference_time_ns')}
        summary.append(out)
    base={r['instance_id']:r for r in summary if r['profile']=='B0_R'}
    for r in summary:
        b=base.get(r['instance_id'])
        if b and r['median_wall_s'] and b['median_wall_s']:
            r['speedup_over_B0_R']=b['median_wall_s']/r['median_wall_s']
    return summary

def main():
    pa=argparse.ArgumentParser(description='VOC-LS E0 standard reproducible performance runner')
    pa.add_argument('--repo',type=Path,required=True)
    pa.add_argument('--manifest',type=Path,required=True)
    pa.add_argument('--out',type=Path,required=True)
    pa.add_argument('--splits',default='smoke',help='comma separated: smoke,train,val,test,ood')
    pa.add_argument('--profiles',default='B0_R,B0_Strong')
    pa.add_argument('--phase',choices=['timing','diagnostic','both'],default='both')
    pa.add_argument('--timing-repeats',type=int,default=5)
    pa.add_argument('--diagnostic-repeats',type=int,default=1)
    pa.add_argument('--warmups',type=int,default=1)
    pa.add_argument('--timeout',type=int,default=300)
    pa.add_argument('--cpus',default='',help='Example 2,3; must be in current Linux CPU affinity mask')
    pa.add_argument('--seed',type=int,default=20261115)
    pa.add_argument('--limit',type=int,default=0,help='Optional max independent instance count for development only')
    pa.add_argument('--build',action='store_true',help='Build PathWyse from local source (no network)')
    pa.add_argument('--jobs',type=int,default=8)
    pa.add_argument('--resume',action='store_true',help='Append to an interrupted run with matching config')
    pa.add_argument('--final-test',action='store_true',help='Explicitly approve evaluating held-out test/ood sets')
    args=pa.parse_args()
    if min(args.timing_repeats,args.diagnostic_repeats,args.timeout,args.jobs)<1 or args.warmups<0:pa.error('invalid repeats/timeout/jobs/warmups')
    root=args.repo.expanduser().resolve();manifest=args.manifest.expanduser().resolve();out=args.out.expanduser().resolve()
    if not (root/'src/CMakeLists.txt').is_file():pa.error('wrong --repo: PathWyse root expected')
    if not manifest.is_file():pa.error('manifest not found')
    m=json.loads(manifest.read_text(encoding='utf-8'))
    if m.get('format')!='VOC-LS/E0-manifest-v1':pa.error('bad manifest format')
    splits=[s.strip() for s in args.splits.split(',') if s.strip()]
    if any(s not in {'smoke','train','val','test','ood'} for s in splits):pa.error('unknown split')
    if any(s in {'test','ood'} for s in splits) and not args.final_test:pa.error('--final-test required to release test/ood data')
    profiles=[s.strip() for s in args.profiles.split(',') if s.strip()]
    if not profiles or len(set(profiles))!=len(profiles) or any(p not in PROFILES for p in profiles):pa.error('invalid profiles')
    instances=[r for r in m['instances'] if r['split'] in splits]
    if args.limit>0:instances=instances[:args.limit]
    if not instances:pa.error('no instances in selected splits')
    for r in instances:
        p=Path(r['path'])
        if not p.is_file() or sha_file(p)!=r['sha256']:raise SystemExit(f'Instance missing/changed: {p}')
    cpus=None
    if args.cpus:
        cpus=[int(x.strip()) for x in args.cpus.split(',')]
        allowed=os.sched_getaffinity(0)
        if len(cpus)!=len(set(cpus)) or not set(cpus).issubset(allowed):pa.error(f'bad cpus, allowed={sorted(allowed)}')
        if 'B0_Strong' in profiles and len(cpus)<2:pa.error('B0_Strong requires two or more allowed CPUs')
    out.mkdir(parents=True,exist_ok=True)
    shutil.copy2(manifest,out/'manifest_snapshot.json')
    if (out/'timing.csv').exists() and not args.resume:pa.error('output exists; use another --out or --resume')
    meta={'experiment':'VOC-LS/E0-standard-v1','created_utc':utc(),'source_tree_sha256':source_hash(root),
          'project_root':str(root),'manifest':str(manifest),'manifest_sha256':sha_file(manifest),
          'platform':platform.platform(),'python':sys.version.split()[0],'cmake_version':shutil.which('cmake'),
          'compiler_version':shutil.which('g++'),'profiles':profiles,'splits':splits,'phase':args.phase,
          'warmups':args.warmups,'timing_repeats':args.timing_repeats,'diagnostic_repeats':args.diagnostic_repeats,
          'timeout_s':args.timeout,'cpus':cpus,'source_count':len(instances),'seed':args.seed,
          'separate_measurement_and_diagnostic':True,'inference_semantics':'B0 no model: 0 calls and 0 ns; E1 must explicitly instrument model invocation',
          'progress':'started'}
    meta_path=out/'metadata.json'
    if args.resume and meta_path.is_file():
        old=json.loads(meta_path.read_text());
        for k in ('source_tree_sha256','manifest_sha256','profiles','splits','phase'):
            if old.get(k)!=meta.get(k):raise SystemExit(f'Resume conflict for {k}')
    else:write_json(meta_path,meta)
    if args.build:
        for cmd in (["cmake","-S",str(root),"-B",str(root/'build'),"-DCMAKE_BUILD_TYPE=Release"],["cmake","--build",str(root/'build'),"-j",str(args.jobs)]):
            print('Building:',cmd,flush=True);subprocess.run(cmd,check=True,cwd=root)
    exe=root/'bin/pathwyse';
    if not exe.is_file():raise SystemExit('Missing bin/pathwyse; use --build')
    meta['binary_sha256']=sha_file(exe)
    cfg={}
    for profile in profiles:
        cfg[profile]={}
        for diagnostic in (False,True):
            key='diagnostic' if diagnostic else 'timing'
            dest=out/'configs'/f'{profile}_{key}.set'
            cfg[profile][key]=settings_file(root/'pathwyse.set',dest,profile,diagnostic)
    if 'B0_R' in cfg and 'B0_Strong' in cfg:
        for stage in ('timing','diagnostic'):
            if not check_config_pair(cfg['B0_R'][stage],cfg['B0_Strong'][stage]):raise ValueError('baseline configs differ beyond parallel and DSSR!')
    meta['config_sha256']={f'{p}_{stage}':sha_file(out/'configs'/f'{p}_{stage}.set') for p in profiles for stage in ('timing','diagnostic')}
    write_json(meta_path,meta)
    rng=random.Random(args.seed)
    timing=read_csv(out/'timing.csv');diags=read_csv(out/'diagnostics_iterations.csv');totals=read_csv(out/'diagnostics_totals.csv')
    existing_t={(x['instance_id'],x['profile'],x['phase'],int(x['repeat'])) for x in timing}
    existing_d={(x['instance_id'],x['profile'],int(x['repeat'])) for x in totals}
    env=dict(os.environ);env.update({'OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','LC_ALL':'C','VOC_E0_PROFILE':'0'})
    error_count=0
    def execute(inst,profile,kind,repeat,prewarm=False):
        iid=inst['id'];path=Path(inst['path']);level='diagnostic' if kind=='diagnostic' else 'timing'
        cwd=(out/'diagnostic_work'/(iid+'__'+profile+f'__r{repeat:03d}')) if kind=='diagnostic' else root
        if kind=='diagnostic' and cwd.exists():shutil.rmtree(cwd) # safe retry of incomplete diagnostic
        cwd.mkdir(parents=True,exist_ok=True)
        log=out/'logs'/(f'{iid}__{profile}__{kind}__r{repeat:03d}'+('__warmup' if prewarm else '')+'.log')
        e=dict(env);e['VOC_E0_PROFILE']='1' if kind=='diagnostic' else '0'
        cmd=[str(exe),str(path),'-param',str(out/'configs'/f'{profile}_{level}.set')]
        code,ns,timeout=command(cmd,cwd,log,args.timeout,e,cpus)
        parsed=parse_output(log.read_text(encoding='utf-8',errors='replace'))
        row={"instance_id":iid,"group":inst['group'],"source":inst['source'],"split":inst['split'],"profile":profile,"phase":kind,"repeat":repeat,
             **parsed,'wall_s':round(ns/1e9,9),'wall_ns':ns,'model_used':False,'inference_calls':0,'inference_time_ns':0,
             'exit_code':code,'timed_out':timeout,'log_path':str(log.relative_to(out))}
        # Exactness status alone is not independent proof; check reference and cross-baseline parity.
        if parsed['status']!='Optimal' or parsed['objective'] is None or parsed['solver_s'] is None or code or timeout:
            raise RuntimeError(f'Non-optimal/failed: {iid} {profile} {kind}; see {log}')
        if inst['source']=='toy' and abs(parsed['objective']+10435)>1e-6:
            raise RuntimeError('Toy objective differs from independent oracle')
        if inst['source'] in {'toy','synthetic','synthetic_ood'}:
            verify_synthetic_or_toy_path(path,parsed)
        return row,cwd
    for inst in instances:
        for phase in (('timing','diagnostic') if args.phase=='both' else (args.phase,)):
            count=args.timing_repeats if phase=='timing' else args.diagnostic_repeats
            for rep in range(1,count+1):
                order=profiles[:];rng.shuffle(order)
                for profile in order:
                    id_key=(inst['id'],profile,phase,rep)
                    if (phase=='timing' and id_key in existing_t) or (phase=='diagnostic' and (inst['id'],profile,rep) in existing_d):continue
                    if phase=='timing' and rep==1:
                        for w in range(args.warmups):
                            execute(inst,profile,'timing',-(w+1),prewarm=True)
                    row,cwd=execute(inst,profile,phase,rep)
                    if phase=='timing':
                        timing.append(row);write_csv(out/'timing.csv',KEYS,timing)
                    else:
                        try:items=parse_diagnostics(cwd)
                        except Exception as ex:raise RuntimeError(f'{inst["id"]}, {profile}: {ex}') from ex
                        for x in items:diags.append({**{k:row[k] for k in ('instance_id','group','source','split','profile','repeat')},**x})
                        total={k:row[k] for k in ('instance_id','group','source','split','profile','repeat')}
                        total['iterations']=len(items)
                        for key in ('extensions_total','dom_checks_total','dom_probe_time_ns','inference_calls','inference_time_ns','nlabels_inserted','ndominated','nclosed','njoin'):
                            total[('nlabels_inserted_sum' if key=='nlabels_inserted' else 'ndominated_sum' if key=='ndominated' else 'nclosed_sum' if key=='nclosed' else 'njoin_sum' if key=='njoin' else key)]=sum(x[key] for x in items)
                        total['ins_attempts_sum']=sum(x['ins_attempts_fw']+x['ins_attempts_bw'] for x in items)
                        total['diagnostic_wall_s']=row['wall_s'];totals.append(total)
                        write_csv(out/'diagnostics_iterations.csv',ITER_KEYS,diags)
                        write_csv(out/'diagnostics_totals.csv',TOTAL_KEYS,totals)
                    print(f'[{phase}] {inst["id"]} {profile} {rep}/{count}: {row["status"]} obj={row["objective"]} wall={row["wall_s"]:.6f}s',flush=True)
    # Check cross-run correctness, including independently captured diagnostics.
    objective_by_instance=collections.defaultdict(list)
    for x in timing:objective_by_instance[x['instance_id']].append(float(x['objective']))
    for instid,values in objective_by_instance.items():
        if any(abs(x-values[0])>1e-6 for x in values):raise RuntimeError(f'Cross-profile objective mismatch: {instid}')
    summary=generate_summary(timing,totals)
    write_csv(out/'summary_per_instance.csv',SUMMARY_KEYS,summary)
    meta['completed_utc']=utc();meta['progress']='complete';meta['timing_count']=len(timing)
    meta['diagnostic_round_rows']=len(diags);meta['diagnostic_run_count']=len(totals)
    meta['all_optimal']=True;meta['objective_consistent']=True
    write_json(meta_path,meta)
    print('\nE0 STANDARD PASS; outputs:',out,flush=True)

if __name__=='__main__':main()
