#!/usr/bin/env python3
"""Make independent graph-level train/val/test/ood manifests for VOC-LS.

Synthetic graphs are ENGINEERING datasets, not substitutes for real-world SPPRC evaluation.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
from pathlib import Path
import random
import re

def sha256(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def generate_graph(path:Path,n:int,seed:int,density:float):
    rng=random.Random(seed)
    node_consumption=[0]+[rng.randint(1,3) for _ in range(n-2)]+[1]
    budget=max(n//2+2, round(sum(node_consumption[1:])*0.56))
    edges={(u,u+1) for u in range(n-1)}|{(0,n-1)}
    for u in range(n-1):
        for v in range(1,n):
            if u!=v and rng.random()<density:edges.add((u,v))
    arcs={(u,v):rng.randint(700,5000) for u,v in sorted(edges)}
    for edge in [(u,u+1) for u in range(n-1)]: arcs[edge]=rng.randint(400,2000)
    arcs[(0,n-1)]=rng.randint(2000,8000)
    benefits=[0]+[-rng.randint(500,6000) for _ in range(n-2)]+[0]
    parts=[f'NAME : VOC_SYNTH_{seed}',f'COMMENT : Independent synthetic graph seed={seed}; E0 engineering only',
          f'SIZE : {n}','DIRECTED : 1','CYCLIC : 1','ORIGIN : 0',f'DESTINATION : {n-1}',
          'RESOURCES : 1','RES_NAMES : 0','RES_TYPE','0 CAP','END','RES_BOUND',f'0 0 {budget}','END','EDGE_COST']
    parts.extend(f'{u} {v} {arcs[u,v]}' for u,v in sorted(arcs))
    parts.extend(['END','NODE_COST'])
    parts.extend(f'{u} {benefits[u]}' for u in range(n))
    parts.extend(['END','NODE_CONSUMPTION'])
    parts.extend(f'0 {u} {node_consumption[u]}' for u in range(n))
    parts.extend(['END',''])
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(parts),encoding='utf-8')
    return {'n_nodes':n,'edge_count':len(edges),'capacity':budget,'density':density,'seed':seed}

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--repo',required=True,type=Path,help='PathWyse project root')
    a.add_argument('--out',required=True,type=Path,help='Manifest JSON path; synthetic data stored in sibling synthetic/ folder')
    a.add_argument('--synthetic-count',type=int,default=60,help='Independent train/val/test graphs; should be divisible by 3')
    a.add_argument('--ood-count',type=int,default=12,help='Unseen larger graphs, separate OOD split')
    a.add_argument('--seed',type=int,default=20261115)
    a.add_argument('--real-dir',action='append',default=[],help='Additional offline independent .sppcc datasets; repeat option')
    a.add_argument('--real-groups',type=Path,help='Optional JSON map: full path/name -> graph group ID (recommended for variants of one original graph)')
    args=a.parse_args()
    if args.synthetic_count<6 or args.ood_count<0:raise SystemExit('Need at least six independent graphs')
    repo=args.repo.expanduser().resolve();out=args.out.expanduser().resolve()
    out.parent.mkdir(parents=True,exist_ok=True)
    rng=random.Random(args.seed); records=[]
    # Official Toy is an oracle/smoke test; a single SPPRCLIB source graph is never used as an independent test set.
    original_hashes=set()
    for source,name in [('toy','instances/toy.txt'),('spprclib','instances/spprclib/A-n54-k7-149.sppcc')]:
        p=repo/name
        if p.is_file():
            original_hashes.add(sha256(p))
            group='real_'+re.sub(r'-\d+$','',p.stem) if source=='spprclib' else 'toy_original'
            records.append({'id':source+'_smoke','path':str(p),'split':'smoke','group':group,'source':source,'sha256':sha256(p),'original_instance':True,'label':'smoke_only'})
    # Optional independent real SPPRC dataset. Group queries of the same source graph
    # together; never split the same source graph between train/val/test.
    custom_groups={}
    if args.real_groups:
        custom_groups=json.loads(args.real_groups.expanduser().read_text(encoding='utf-8'))
    extra=[]
    for rawdir in args.real_dir:
        folder=Path(rawdir).expanduser().resolve()
        if not folder.is_dir():raise SystemExit('Missing --real-dir: '+str(folder))
        extra.extend(folder.rglob('*.sppcc'))
    unique_real=[];seen_hashes=set(original_hashes)
    for path in sorted(set(extra)):
        digest=sha256(path)
        if digest in seen_hashes:continue
        seen_hashes.add(digest)
        family=re.sub(r'-\d+$','',path.stem)
        group=custom_groups.get(str(path),custom_groups.get(path.name,'real_'+family))
        if not group or not isinstance(group,str):raise SystemExit('Missing valid real group for: '+str(path))
        unique_real.append({'id':'real_'+digest[:12],'path':str(path),'group':group,
                            'source':'real_spprclib','sha256':digest,'original_instance':True})
    bygroup=collections.defaultdict(list)
    for rec in unique_real:bygroup[rec['group']].append(rec)
    groups=sorted(bygroup)
    rng.shuffle(groups)
    reserved_groups={r['group'] for r in records if r['split']=='smoke'}
    if len([g for g in groups if g not in reserved_groups])<5:
        for rec in unique_real:rec['split']='smoke'
        records.extend(unique_real)
    else:
        groups=[g for g in groups if g not in reserved_groups]
        for g in reserved_groups:
            for rec in bygroup.get(g,[]):rec['split']='smoke';records.append(rec)
        n_train=round(len(groups)*0.6);n_val=round(len(groups)*0.2)
        for i,g in enumerate(groups):
            target='train' if i<n_train else 'val' if i<n_train+n_val else 'test'
            for rec in bygroup[g]:rec['split']=target;records.append(rec)
    sizes=[10,14,18]
    staged=[]
    for i in range(args.synthetic_count):
        n=sizes[i%len(sizes)];seed=args.seed*1000+100+i;density=[0.12,0.18,0.25][(i//len(sizes))%3]
        path=out.parent/'synthetic'/f'n{n:02d}_graph{i:04d}.txt'
        info=generate_graph(path,n,seed,density)
        group=f'synthetic_graph_{i:04d}'
        rec={'id':group,'path':str(path),'group':group,'source':'synthetic','sha256':sha256(path),'original_instance':False,**info}
        staged.append(rec)  # stage independent graphs before size-stratified assignment
    # Stratify by size, but keep each independently generated graph in exactly one split.
    bucket=collections.defaultdict(list)
    for rec in staged: bucket[rec['n_nodes']].append(rec)
    for n,items in sorted(bucket.items()):
        rng.shuffle(items)
        c=len(items);n_train=round(c*0.6);n_val=round(c*0.2)
        for j,rec in enumerate(items):
            rec['split']='train' if j<n_train else 'val' if j<n_train+n_val else 'test'
            records.append(rec)
    for i in range(args.ood_count):
        n=24;seed=args.seed*1000+10000+i;density=[0.12,0.2][i%2]
        path=out.parent/'synthetic_ood'/f'n{n:02d}_ood{i:04d}.txt'
        info=generate_graph(path,n,seed,density)
        records.append({'id':f'ood_graph_{i:04d}','path':str(path),'split':'ood','group':f'ood_graph_{i:04d}',
                        'source':'synthetic_ood','sha256':sha256(path),'original_instance':False,**info})
    groups_by_split=collections.defaultdict(set)
    for rec in records:groups_by_split[rec['split']].add(rec['group'])
    for split1,s1 in groups_by_split.items():
        for split2,s2 in groups_by_split.items():
            if split1!=split2 and s1&s2:raise AssertionError(f'Leakage {split1}, {split2}: {s1&s2}')
    meta={'format':'VOC-LS/E0-manifest-v1','seed':args.seed,'splitting':'independent graph groups, size-stratified 60/20/20',
          'warning':'Synthetic instances are engineering validation only; original SPPRCLIB source is SMOKE, not independent test. Extra real group identifiers must be audited.',
          'counts':dict(collections.Counter(r['split'] for r in records)), 'instances':sorted(records,key=lambda x:(x['split'],x['id']))}
    out.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('DATASET MANIFEST:',out)
    print('Split counts:',meta['counts'])
    print('Source graph reuse across splits: NONE')

if __name__=='__main__':main()
