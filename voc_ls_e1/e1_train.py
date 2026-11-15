#!/usr/bin/env python3
"""Train an honest, tiny *learned* linear candidate ranker on B0 expansion traces.

This is a supervised proxy for immediate non-dominated insertion rate, NOT
VOC, NOT an original published dominant-state classifier, and NOT a promise
of search/runtime improvement. All training data originate from the provided
training-instance manifest. No third-party Python dependencies.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

FEATURES = ("objective", "critical", "unreachable", "visited", "degree", "node_frac", "direction")
MODEL_MAGIC = "E1LINEAR_V1"


def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1<<20),b""):
            h.update(block)
    return h.hexdigest()


def solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for small, ridge-regularized matrix."""
    n=len(b)
    aug=[a[i][:]+[b[i]] for i in range(n)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda j: abs(aug[j][col]))
        if abs(aug[pivot][col]) < 1e-14: raise ValueError("Training matrix is singular; increase --ridge")
        aug[pivot],aug[col]=aug[col],aug[pivot]
        factor=aug[col][col]
        for j in range(col,n+1): aug[col][j] /= factor
        for row in range(n):
            if row==col:continue
            val=aug[row][col]
            for j in range(col,n+1): aug[row][j]-=val*aug[col][j]
    return [aug[i][-1] for i in range(n)]


def read_trace(path: Path, maximum: int, seed: int):
    if not path.is_file(): raise FileNotFoundError(path)
    rng=random.Random(seed)
    data=[]
    with path.open(newline="",encoding="utf-8") as f:
        reader=csv.DictReader(f)
        required=set(FEATURES)|{"iteration","attempted","accepted"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Invalid B0 trace CSV {path}: missing {required-set(reader.fieldnames or [])}")
        count=0
        for r in reader:
            count+=1
            x=[float(r[k]) for k in FEATURES]
            attempts=int(r["attempted"]);accepted=int(r["accepted"])
            if attempts<0 or accepted<0 or accepted>attempts or not all(math.isfinite(v) for v in x):
                raise ValueError(f"Invalid training row {count} in {path}")
            # This label is an *immediate expansion proxy*. It does not use
            # future search work or post-hoc oracle decisions as a feature.
            y=accepted/(attempts+1.0)
            point=(x,y)
            if len(data)<maximum: data.append(point)
            else:
                j=rng.randrange(count)
                if j<maximum:data[j]=point
    if count==0:raise ValueError(f"Empty B0 trace: {path}")
    return data,count


def fit(dataset: list[tuple[list[float],float,float]], lam:float):
    nfeat=len(FEATURES)
    # Per-instance weighting is already applied in the third tuple element.
    # Independent of number of traces per instance.
    mass=sum(w for _,_,w in dataset)
    means=[sum(x[j]*w for x,_,w in dataset)/mass for j in range(nfeat)]
    variance=[sum(w*(x[j]-means[j])**2 for x,_,w in dataset)/mass for j in range(nfeat)]
    scales=[math.sqrt(v) if v>1e-14 else 1.0 for v in variance]
    n=nfeat+1
    xtx=[[0.0]*n for _ in range(n)]
    xty=[0.0]*n
    for x,y,w in dataset:
        z=[1.0]+[max(-8.0,min(8.0,(x[j]-means[j])/scales[j])) for j in range(nfeat)]
        for i in range(n):
            xty[i]+=w*z[i]*y
            for j in range(i,n):xtx[i][j]+=w*z[i]*z[j]
    for i in range(n):
        for j in range(i):xtx[i][j]=xtx[j][i]
        if i>0:xtx[i][i]+=lam*mass
    beta=solve_linear(xtx,xty)
    loss=sum(w*(y-(beta[0]+sum(beta[j+1]*max(-8.0,min(8.0,(x[j]-means[j])/scales[j])) for j in range(nfeat))))**2 for x,y,w in dataset)/mass
    return means,scales,beta[1:],beta[0],loss


def main():
    p=argparse.ArgumentParser(description="Offline B0 expansion-trace linear ranker; no dependencies beyond stdlib")
    p.add_argument("--manifest",type=Path,required=True,help="e1_run.py collect -> train_manifest.json")
    p.add_argument("--model",type=Path,required=True,help="output E1LINEAR_V1 model file")
    p.add_argument("--ridge",type=float,default=0.01)
    p.add_argument("--max-rows-per-instance",type=int,default=50000)
    p.add_argument("--seed",type=int,default=42)
    args=p.parse_args()
    if args.ridge<=0 or args.max_rows_per_instance<2: p.error("--ridge must be >0, --max-rows-per-instance >= 2")
    manifest_path=args.manifest.expanduser().resolve()
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind")!="VOC-LS/E1-TRAIN-TRACE-v1" or not manifest.get("traces"):
        p.error("Invalid training manifest. Run e1_run.py collect first")
    dataset=[];summary=[];all_sha=set()
    groups={}
    for i,r in enumerate(manifest["traces"]):
        digest=r["instance_sha256"]
        groups.setdefault(digest,[]).append((i,r))
    ninst=len(groups)
    for digest, files in groups.items():
        profiles_seen=set()
        for i,r in files:
            profile=r.get("profile","p0_r")
            if profile in profiles_seen:raise ValueError(f"Duplicate train trace for same instance and profile: {digest}, {profile}")
            profiles_seen.add(profile)
            trace=Path(r["trace_path"])
            if not trace.is_absolute(): trace=(manifest_path.parent/trace).resolve()
            if sha(trace)!=r["trace_sha256"]: raise ValueError(f"Training trace changed since collection: {trace}")
            sample,total=read_trace(trace,args.max_rows_per_instance,args.seed+i)
            # Each source instance has equal total weight, even when profiling
            # two DSSR modes and when their trace lengths differ considerably.
            sample_weight=1.0/(ninst*len(files)*len(sample))
            dataset.extend((x,y,sample_weight) for x,y in sample)
            summary.append({"instance":r["instance"],"profile":profile,"sha256":digest,
                            "all_labels":total,"sampled_labels":len(sample),"mean_proxy":statistics.mean(y for _,y in sample)})
        all_sha.add(digest)
    means,scales,weights,bias,loss=fit(dataset,args.ridge)
    output=args.model.expanduser().resolve();output.parent.mkdir(parents=True,exist_ok=True)
    lines=[MODEL_MAGIC,str(len(FEATURES))]
    for vec in [means,scales,weights]:lines.append(" ".join(format(v,".17g") for v in vec))
    lines.append(format(bias,".17g"))
    output.write_text("\n".join(lines)+"\n",encoding="utf-8")
    meta={"kind":"VOC-LS/E1-LINEAR-PROXY-v1","model_path":str(output),"model_sha256":sha(output),"feature_order":list(FEATURES),"target":"accepted_insertions/(attempted_insertions+1)","train_instances":summary,"train_instance_sha256":sorted(all_sha),"trace_manifest":str(manifest_path),"training_source_tree_sha256":manifest.get("source_tree_sha256"),"ridge":args.ridge,"max_rows_per_instance":args.max_rows_per_instance,"train_weighted_mse":loss,"training_rows":len(dataset),"means":means,"scales":scales,"weights":weights,"bias":bias,"limitations":"Immediate expansion proxy; not a VOC prediction or guarantee of runtime gain. Held-out evaluation must be instance-disjoint."}
    (output.parent/(output.name+".json")).write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("E1 TRAIN COMPLETE: model",output)
    print("Independent training instances:",ninst," training traces:",len(summary)," sampled label rows:",len(dataset))
    print("Weighted training proxy MSE:",round(loss,8))
    print("Model SHA256:",meta["model_sha256"])
    if ninst<3:print("WARNING: fewer than three training instances; treat as engineering smoke, not evidence of generalization")


if __name__=="__main__":main()
