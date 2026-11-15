#!/usr/bin/env python3
"""VOC-LS E1 offline training-trace collection or strict B0/B1 paired benchmark.

Only stdlib. Same binary, same config per pair, policy differs only by env.
No network, git checkout, or PyTorch required on the Linux server.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
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
import subprocess
import sys
import threading
import time

CFG = {
 "algo_names":"PWDefault", "algo_active":"1", "algo_parallel":"0",
 "algo/default/autoconfig":"0", "algo/default/search":"bidirectional",
 "algo/default/candidate/type":"node", "algo/default/use_visited":"1",
 "algo/default/compare_unreachables":"1", "algo/two_cycle_elimination":"1",
 "algo/default/ng":"off", "algo/default/timelimit":"0",
 "algo/requested_solutions":"1", "data_collection/level":"-1", "output/write":"0", "verbosity":"1",
}
PROFILES={"p0_r":{"algo/default/parallel":"0","algo/default/dssr":"restricted"},
          "p1_s":{"algo/default/parallel":"1","algo/default/dssr":"standard"}}
NUMBER=r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
FIELDS=["instance","instance_sha256","profile","round","order","policy","status","objective","tour","consumption","wall_s","wall_ns","solver_s","candidate_calls","scored_labels","selector_ns","trace_rows","reordered_choices","dssr_iterations","exit_code","timeout","log"]


def sha(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1<<20),b""):h.update(b)
    return h.hexdigest()


def source_hash(root:Path)->str:
    paths=sorted(p for p in (root/"src").rglob("*") if p.is_file() and p.suffix in (".cpp",".h",".hpp",".c",".cc"))
    paths += [root/"CMakeLists.txt",root/"src/CMakeLists.txt",root/"pathwyse.set"]
    h=hashlib.sha256()
    for p in paths:
        h.update(str(p.relative_to(root)).encode()+b"\0"+sha(p).encode()+b"\n")
    return h.hexdigest()


def read_instances(root:Path,filename:Path)->list[Path]:
    path=filename.expanduser().resolve()
    if not path.is_file():raise ValueError(f"Instance manifest not found: {path}")
    result=[]
    for ln,line in enumerate(path.read_text(encoding="utf-8").splitlines(),1):
        name=line.strip()
        if not name or name.startswith("#"):continue
        f=(Path(name) if Path(name).is_absolute() else root/name).resolve()
        if not f.is_file():raise ValueError(f"Missing instance {filename}:{ln}: {f}")
        if f in result:raise ValueError(f"Duplicate instance path: {f}")
        result.append(f)
    if not result:raise ValueError("Instance file list is empty")
    hashes=[sha(f) for f in result]
    if len(hashes)!=len(set(hashes)):
        raise ValueError("Duplicate instance file contents in the dataset: use instance-level unique files")
    return result


def make_conf(root:Path,out:Path,profile:str)->Path:
    old=(root/"pathwyse.set").read_text(encoding="utf-8").splitlines()
    changes={**CFG,**PROFILES[profile]};seen=set();new=[]
    for line in old:
        m=re.match(r"^\s*([^#=]+?)\s*=",line)
        k=m.group(1).strip() if m else None
        if k in changes:
            if k in seen:raise ValueError(f"Duplicate config entry in pathwyse.set: {k}")
            seen.add(k);new.append(f"{k} = {changes[k]}")
        else:new.append(line)
    for k in sorted(changes.keys()-seen):new.append(f"{k} = {changes[k]}")
    target=out/"configs"/(profile+".set")
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text("\n".join(new)+"\n",encoding="utf-8")
    return target


def run(cmd:list[str],cwd:Path,log:Path,timeout:int,env:dict):
    """Blocking waitpid, watchdog kills process group; accurate E2E wall clock."""
    log.parent.mkdir(parents=True,exist_ok=True)
    timed=threading.Event()
    with log.open("w",encoding="utf-8") as f:
        f.write("# "+" ".join(cmd)+"\n");f.flush()
        start=time.perf_counter_ns()
        proc=subprocess.Popen(cmd,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        def kill():
            if proc.poll() is None:
                timed.set()
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        watchdog=threading.Timer(timeout,kill);watchdog.daemon=True;watchdog.start()
        try:
            code=proc.wait()
            ns=time.perf_counter_ns()-start
        finally:watchdog.cancel();watchdog.join()
        if timed.is_set():f.write("\n[E1] PROCESS TIMEOUT\n")
    return (124 if timed.is_set() else code),ns,timed.is_set()


def parse(log:Path)->dict:
    s=log.read_text(encoding="utf-8",errors="replace")
    def field(label):
        found=re.findall(r"^\s*"+re.escape(label)+r"\s*:\s*(.*?)\s*$",s,re.M)
        return found[-1] if found else None
    obj=field("Obj");solver=field("PWDefault Time")
    statline=re.findall(r"^E1_STATS\s+(.+)$",s,re.M)
    stats={}
    if statline:
        for tok in statline[-1].strip().split():
            if "=" in tok:
                k,v=tok.split("=",1);stats[k]=v
    return {"status":field("Solution Status"),"objective":float(obj) if obj and re.fullmatch(NUMBER,obj) else None,
            "tour":field("Tour"),"consumption":field("Consumption"),
            "solver_s":float(solver) if solver and re.fullmatch(NUMBER,solver) else None,"stats":stats}


def validate_run(row:dict,mode:str):
    if row["exit_code"]!=0 or row["timeout"] or row["status"]!="Optimal" or row["objective"] is None:
        raise RuntimeError(f"Solver failed: {row}")
    stat=row["stats"]
    if stat.get("policy")!=mode:
        raise RuntimeError(f"Binary not E1-patched OR wrong E1_POLICY: expected={mode}, stats={stat}")
    calls=int(stat.get("candidate_calls","0"))
    if calls<1:raise RuntimeError(f"No E1 candidate calls in {row['log']}; choose a non-trivial instance")
    if mode=="learned" and int(stat.get("scored_labels","0"))<calls:
        raise RuntimeError(f"B1 did not actually score labels: {row['log']}")


def execute_one(root:Path,out:Path,exe:Path,conf:Path,inst:Path,env:dict,policy:str,round_no:int,order:int,tag:str,timeout:int,trace:Path|None=None):
    log=out/"logs"/(hashlib.sha256(str(inst).encode()).hexdigest()[:10]+"_"+inst.stem[:25]+f"__{tag}__r{round_no:03d}__{order}__{conf.stem}__{policy}.log")
    env={**env,"E1_POLICY":policy,"E1_TOP_K":env.get("E1_TOP_K","8")}
    env.pop("E1_MODEL",None);env.pop("E1_TRACE_FILE",None)
    # model passed externally via e1EnvModel
    if policy=="learned":env["E1_MODEL"]=execute_one.model_path
    if trace:env["E1_TRACE_FILE"]=str(trace)
    cmd=[str(exe),str(inst),"-param",str(conf)]
    code,elapsed,timed=run(cmd,out/"work",log,timeout,env)
    parsed=parse(log)
    row={"instance":str(inst.relative_to(root)) if inst.is_relative_to(root) else str(inst),
         "instance_sha256":sha(inst),"profile":conf.stem,"round":round_no,"order":order,"policy":policy,
         "status":parsed["status"],"objective":parsed["objective"],"tour":parsed["tour"],"consumption":parsed["consumption"],
         "wall_s":elapsed/1e9,"wall_ns":elapsed,"solver_s":parsed["solver_s"],"candidate_calls":int(parsed["stats"].get("candidate_calls","0")),
         "scored_labels":int(parsed["stats"].get("scored_labels","0")),"selector_ns":int(parsed["stats"].get("selector_ns","0")),
         "trace_rows":int(parsed["stats"].get("trace_rows","0")),"reordered_choices":int(parsed["stats"].get("reordered_choices","0")),"dssr_iterations":int(parsed["stats"].get("iterations","0")),
         "exit_code":code,"timeout":timed,"log":str(log.relative_to(out)),"stats":parsed["stats"]}
    validate_run(row,policy)
    if trace and (not trace.is_file() or trace.stat().st_size<100 or row["trace_rows"]<1):
        raise RuntimeError(f"No B0 supervision trace: {trace}")
    return row


def run_build(root:Path,out:Path,jobs:int,timeout:int,env:dict):
    cmds=[("configure",["cmake","-S",str(root),"-B",str(root/"build"),"-DCMAKE_BUILD_TYPE=Release"]),
          ("build",["cmake","--build",str(root/"build"),"-j",str(jobs)])]
    for name,cmd in cmds:
        code,_,timed=run(cmd,root,out/"logs"/(name+".log"),timeout,env)
        if code!=0 or timed:raise RuntimeError(f"CMake {name} failed; inspect {out/'logs'/(name+'.log')}")


def main():
    ap=argparse.ArgumentParser(description="VOC-LS E1: offline B0 traces / B0 vs genuine learned B1 paired timings")
    ap.add_argument("mode",choices=["collect","eval"])
    ap.add_argument("--repo",type=Path,default=Path.home()/"code/dass/pathwyse")
    ap.add_argument("--instances-file",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--model",type=Path,help="Required for eval; trained by e1_train.py")
    ap.add_argument("--profiles",default="p0_r,p1_s",help="Eval config profiles: p0_r,p1_s; collect config profiles can also use this")
    ap.add_argument("--repeats",type=int,default=10)
    ap.add_argument("--warmups",type=int,default=2)
    ap.add_argument("--seed",type=int,default=20261115)
    ap.add_argument("--top-k",type=int,default=8)
    ap.add_argument("--timeout",type=int,default=300)
    ap.add_argument("--jobs",type=int,default=8)
    ap.add_argument("--skip-build",action="store_true")
    ap.add_argument("--allow-overlap-smoke",action="store_true",help="Non-publishable debugging only; explicitly permit train/test overlap")
    args=ap.parse_args()
    if args.repeats<1 or args.warmups<0 or args.timeout<1 or args.jobs<1 or not 1<=args.top_k<=64:ap.error("Invalid repeats/warmups/timeout/jobs/top-k")
    root=args.repo.expanduser().resolve()
    if not (root/"pathwyse.set").is_file() and (root/"pathwyse-main"/"pathwyse.set").is_file():root=root/"pathwyse-main"
    if not (root/"pathwyse.set").is_file():ap.error("--repo must point to a PathWyse project root")
    instances=read_instances(root,args.instances_file)
    out=args.out.expanduser().resolve()
    if (out/"metadata.json").exists():ap.error("Output already exists: choose a fresh --out path (strict no overwrite)")
    out.mkdir(parents=True,exist_ok=True);(out/"work").mkdir(exist_ok=True)
    env=os.environ.copy();env.update({"LC_ALL":"C","OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","E1_TOP_K":str(args.top_k)})
    model_meta=None
    profiles=[x.strip() for x in args.profiles.split(",") if x.strip()]
    if not profiles or len(set(profiles))!=len(profiles) or any(x not in PROFILES for x in profiles):
        ap.error("Bad --profiles; allowed p0_r,p1_s")
    if args.mode=="eval":
        if args.model is None:ap.error("--model is required in eval mode")
        model=args.model.expanduser().resolve()
        model_meta_path=model.parent/(model.name+".json")
        if not model.is_file() or not model_meta_path.is_file():ap.error("Missing model or model metadata: run e1_train.py")
        model_meta=json.loads(model_meta_path.read_text(encoding="utf-8"))
        if sha(model)!=model_meta.get("model_sha256"):ap.error("Model SHA256 does not match training metadata")
        overlap=set(sha(p) for p in instances)&set(model_meta["train_instance_sha256"])
        if overlap and not args.allow_overlap_smoke:ap.error("TRAIN/TEST CONTENT LEAKAGE: test instance SHA256 appears in training set. Use distinct data; --allow-overlap-smoke only for debugging")
        execute_one.model_path=str(model)

    current_source_sha=source_hash(root)
    if args.mode=="eval" and model_meta.get("training_source_tree_sha256") not in (None,current_source_sha):
        ap.error("SOURCE DRIFT: evaluation C++ source/config SHA differs from training trace collection. Rebuild/recollect/retrain from one frozen version.")
    meta={"kind":"VOC-LS/E1-"+args.mode+"-v1","started_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
          "project_root":str(root),"source_tree_sha256":current_source_sha,"instance_paths":[str(x) for x in instances],"instance_sha256":{str(x):sha(x) for x in instances},
          "profiles":profiles,"binary_sha256":None,"model_meta":str(args.model) if args.model else None,
          "model_sha256":model_meta["model_sha256"] if model_meta else None,
          "overlap_smoke":bool(args.allow_overlap_smoke),"top_k":args.top_k,"repeats":args.repeats,"warmups":args.warmups,
          "python":platform.python_version(),"platform":platform.platform(),"timing":"monotonic blocking Popen.wait + watchdog", "completed":False}
    (out/"metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n")
    try:
        if not args.skip_build:run_build(root,out,args.jobs,max(args.timeout,600),env)
        exe=root/"bin"/"pathwyse"
        if not exe.is_file():raise RuntimeError("Missing compiled PathWyse binary: "+str(exe))
        meta["binary_sha256"]=sha(exe)
        configs={n:make_conf(root,out,n) for n in profiles}
        meta["config_sha256"]={n:sha(p) for n,p in configs.items()}
        rng=random.Random(args.seed)
        if args.mode=="collect":
            trace_meta=[]
            for i,inst in enumerate(instances):
                reference_obj=None
                for order,profile in enumerate(profiles,1):
                    trace=(out/"traces"/(f"train_{i:03d}_{profile}_"+sha(inst)[:12]+".csv"));trace.parent.mkdir(parents=True,exist_ok=True)
                    row=execute_one(root,out,exe,configs[profile],inst,env,"baseline",i+1,order,"collect",args.timeout,trace)
                    if reference_obj is not None and not math.isclose(reference_obj,row["objective"],abs_tol=1e-6,rel_tol=0):
                        raise RuntimeError(f"Training B0 different objectives across profiles: {inst}")
                    reference_obj=row["objective"]
                    trace_meta.append({"instance":row["instance"],"instance_sha256":sha(inst),"profile":profile,
                                       "trace_path":str(trace),"trace_sha256":sha(trace),"trace_rows":row["trace_rows"],"objective":row["objective"]})
                    print(f"E1 B0 TRACE {inst.name} {profile} rows={row['trace_rows']} objective={row['objective']}",flush=True)
            manifest={"kind":"VOC-LS/E1-TRAIN-TRACE-v1","source_tree_sha256":meta["source_tree_sha256"],"binary_sha256":meta["binary_sha256"],"traces":trace_meta}
            (out/"train_manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n")
        else:
            rows=[]
            outf=out/"measurements.csv"
            for inst in instances:
                for warmup in range(args.warmups):
                    for p in profiles:
                        pairs=["baseline","learned"];rng.shuffle(pairs)
                        for order,pol in enumerate(pairs,1):
                            execute_one(root,out,exe,configs[p],inst,env,pol,-warmup-1,order,"warmup",args.timeout)
                for round_id in range(1,args.repeats+1):
                    order_profiles=profiles.copy();rng.shuffle(order_profiles)
                    for p in order_profiles:
                        pairs=["baseline","learned"];rng.shuffle(pairs)
                        vals={}
                        for order,pol in enumerate(pairs,1):
                            r=execute_one(root,out,exe,configs[p],inst,env,pol,round_id,order,"measure",args.timeout)
                            vals[pol]=r;rows.append({k:r.get(k) for k in FIELDS})
                            with outf.open("w",newline="",encoding="utf-8") as f:
                                w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
                        if not math.isclose(vals["baseline"]["objective"],vals["learned"]["objective"],rel_tol=0,abs_tol=1e-6):
                            raise RuntimeError("PRECISION FAILURE B0/B1: "+str(vals))
                        print(f"{inst.name} {p} round={round_id:02d}: B0={vals['baseline']['wall_s']:.6f}s B1={vals['learned']['wall_s']:.6f}s "
                              f"ext=({vals['baseline']['candidate_calls']},{vals['learned']['candidate_calls']}) obj={vals['baseline']['objective']}",flush=True)
            if args.allow_overlap_smoke:print("NON-PUBLISHABLE SMOKE: train/test overlap was explicitly allowed.")
        meta.update({"completed":True,"finished_utc":dt.datetime.now(dt.timezone.utc).isoformat()})
        print("E1",args.mode.upper(),"COMPLETE, output:",out)
    except Exception as e:
        meta.update({"error":str(e),"finished_utc":dt.datetime.now(dt.timezone.utc).isoformat()})
        raise
    finally:(out/"metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n")


if __name__=="__main__":main()
