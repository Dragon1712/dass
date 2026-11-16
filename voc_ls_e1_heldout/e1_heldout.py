#!/usr/bin/env python3
"""VOC-LS E1 held-out continuation: freeze 31/14 -> train/val/test, run/resume
one PathWyse instance at a time, fit existing B1 model, evaluate and audit.

Compatible with unmodified voc_ls_e1/e1_run.py and e1_train.py. Offline stdlib.
Never skips unsuccessful instances silently, never touches the original e1_split.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import statistics as st
import subprocess
import sys
import time

PROFILES=("p0_r", "p1_s")


def digest(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def jread(p: Path):return json.loads(p.read_text(encoding="utf-8"))
def jwrite(p: Path,x):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+".tmp")
    tmp.write_text(json.dumps(x,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    tmp.replace(p)

def resolve_inst(root: Path,s:str)->Path:
    return (Path(s) if Path(s).is_absolute() else root/s).resolve()

def read_list(path: Path,root:Path):
    if not path.is_file():raise ValueError("Missing instance list: "+str(path))
    values=[x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip() and not x.lstrip().startswith("#")]
    if not values:raise ValueError("Empty instance list: "+str(path))
    rec=[]
    for s in values:
        inst=resolve_inst(root,s)
        if not inst.is_file():raise ValueError(f"Missing instance: {inst}")
        rec.append({"path":s,"sha256":digest(inst),"bytes":inst.stat().st_size})
    hashes=[r["sha256"] for r in rec]
    if len(set(hashes))!=len(hashes):raise ValueError(f"Duplicate instance CONTENT in {path}")
    return rec

def matches_manifest(ref_file:Path,train:list,test:list):
    if not ref_file.is_file():raise ValueError("split_manifest.json is required: "+str(ref_file))
    manifest=jread(ref_file)
    for key,rec in [("train_instances",train),("test_instances",test)]:
        expectation={r["path"]:r["sha256"] for r in manifest[key]}
        actual={r["path"]:r["sha256"] for r in rec}
        if actual!=expectation:
            missing=set(expectation)-set(actual)
            excess=set(actual)-set(expectation)
            drift=[p for p in (set(actual)&set(expectation)) if actual[p]!=expectation[p]]
            raise ValueError(f"FROZEN SPLIT MISMATCH {key}: missing={sorted(missing)}, excess={sorted(excess)}, sha drift={sorted(drift)}. Do not train on modified files; regenerate the split explicitly.")
    return digest(ref_file)

def frozen(a):
    p=a.work/"dataset.json"
    if not p.is_file():raise ValueError(f"Missing dataset: {p}. First run prepare.")
    data=jread(p)
    if data["repo"]!=str(a.repo):raise ValueError("Repository path differs from frozen dataset")
    for name in ["train","validation","test"]:
        for r in data[name]:
            p=resolve_inst(a.repo,r["path"])
            if not p.exists() or digest(p)!=r["sha256"]:
                raise ValueError(f"DATASET DRIFT: {p} modified after freeze; refuse to run")
    return data

def file_name(r):
    # Complete digest prevents two same-named instances being mixed.
    return r["sha256"][:16]+"_"+Path(r["path"]).stem[:24]

def project_version(root:Path):
    # MUST reproduce source_hash() in existing voc_ls_e1/e1_run.py exactly.
    paths=sorted(p for p in (root/"src").rglob("*") if p.is_file() and p.suffix in (".cpp",".h",".hpp",".c",".cc"))
    paths += [root/"CMakeLists.txt",root/"src"/"CMakeLists.txt",root/"pathwyse.set"]
    h=hashlib.sha256()
    for p in paths:
        if not p.is_file():raise ValueError(f"Missing source {p}")
        h.update(str(p.relative_to(root)).encode()+b"\0"+digest(p).encode()+b"\n")
    return h.hexdigest()

def attest(a):
    exe=a.repo/"bin/pathwyse"
    if not exe.is_file():raise ValueError(f"Missing binary {exe}; compile patched E1 first")
    shared=sorted((a.repo/"bin").glob("libpathwyse_core.so*"))
    return {"source_tree_sha256":project_version(a.repo),"executable_sha256":digest(exe),
            "shared_libraries_sha256":{p.name:digest(p) for p in shared if p.is_file()},"saved_utc":dt.datetime.now(dt.timezone.utc).isoformat()}

def check_frozen_code(a):
    data=frozen(a)
    proof=attest(a)
    p=a.work/"code_attestation.json"
    if p.exists():
        old=jread(p)
        for k in ("source_tree_sha256","executable_sha256","shared_libraries_sha256"):
            if old.get(k)!=proof[k]:raise ValueError(f"CODE DRIFT {k}: built solver changed between phases; use NEW --work to start a fully frozen experiment")
    else:jwrite(p,proof)
    return data

def run_subproc(cmd, logfile):
    logfile.parent.mkdir(parents=True,exist_ok=True)
    print("COMMAND:"," ".join(map(str,cmd)),flush=True)
    with logfile.open("w",encoding="utf-8") as f:
        p=subprocess.run(list(map(str,cmd)),stdout=f,stderr=subprocess.STDOUT)
    if p.returncode:
        tail=logfile.read_text(encoding="utf-8",errors="replace").splitlines()[-12:]
        print(f"FAILED rc={p.returncode}: {logfile}\n"+"\n".join(tail),file=sys.stderr,flush=True)
    return p.returncode

def signature(a):
    return {"profiles":list(PROFILES),"timeout":a.timeout,"repeats":a.repeats,"warmups":a.warmups,"top_k":a.top_k,"seed":a.seed}

def prepare(a):
    if (a.work/"dataset.json").exists():
        frozen(a)
        print("PREPARE already frozen: "+str(a.work/"dataset.json"))
        return
    raw_train=read_list(a.split_dir/"train.txt",a.repo)
    test=read_list(a.split_dir/"test.txt",a.repo)
    reference=matches_manifest(a.split_dir/"split_manifest.json",raw_train,test)
    if set(x["sha256"] for x in raw_train)&set(x["sha256"] for x in test):
        raise ValueError("CONTENT LEAKAGE: original train/test not disjoint")
    if len(raw_train)<5 or len(test)<2:raise ValueError("Insufficient independent instances")
    rng=random.Random(a.seed)
    pool=raw_train[:];rng.shuffle(pool)
    nval=max(2,min(len(pool)-2,round(a.validation_fraction*len(pool))))
    val=sorted(pool[:nval],key=lambda x:x["path"])
    train=sorted(pool[nval:],key=lambda x:x["path"])
    for name,recs in [("train",train),("validation",val),("test",test)]:
        f=a.work/(name+".txt");f.parent.mkdir(parents=True,exist_ok=True)
        f.write_text("\n".join(r["path"] for r in recs)+"\n",encoding="utf-8")
    data={"kind":"VOC-LS-E1-Heldout-Frozen-v1","repo":str(a.repo),"split_manifest_sha256":reference,
          "seed":a.seed,"validation_fraction":a.validation_fraction,"preregistered_top_k":a.top_k,
          "original_train":len(raw_train),"train":train,"validation":val,"test":test,
          "rule":"DO NOT train on validation or test; DO NOT select hyperparameters on test."}
    jwrite(a.work/"dataset.json",data)
    print(f"FROZEN DATASET: train={len(train)}, validation={len(val)}, test={len(test)}. SHA256 content disjoint.")

def collected_ok(folder:Path,r:dict):
    meta=folder/"metadata.json"; manifest=folder/"train_manifest.json"
    if not meta.is_file() or not manifest.is_file():return False
    m=jread(meta)
    if not m.get("completed") or m.get("kind")!="VOC-LS/E1-collect-v1":return False
    d=jread(manifest)
    if len(d.get("traces",[]))!=len(PROFILES):return False
    if set(x["profile"] for x in d["traces"])!=set(PROFILES):return False
    for x in d["traces"]:
        t=Path(x["trace_path"])
        if x["instance_sha256"]!=r["sha256"] or not t.is_file() or digest(t)!=x["trace_sha256"]:return False
    return True

def evaluated_ok(folder:Path,r:dict,a):
    md=folder/"metadata.json"; csv_path=folder/"measurements.csv"
    if not md.is_file() or not csv_path.is_file():return False
    m=jread(md)
    if not m.get("completed") or m.get("kind")!="VOC-LS/E1-eval-v1" or m.get("overlap_smoke"):return False
    if m.get("repeats")!=a.repeats or m.get("warmups")!=a.warmups or m.get("top_k")!=a.top_k:return False
    if set(m.get("profiles",[]))!=set(PROFILES):return False
    coverage_file=a.work/"training_coverage.json"
    if not coverage_file.exists() or m.get("model_sha256")!=jread(coverage_file).get("model_sha256"):
        return False
    proof_file=a.work/"code_attestation.json"
    if proof_file.exists():
        proof=jread(proof_file)
        if m.get("source_tree_sha256")!=proof.get("source_tree_sha256") or m.get("binary_sha256")!=proof.get("executable_sha256"):
            return False
    with csv_path.open(newline="",encoding="utf-8") as f:rows=list(csv.DictReader(f))
    if len(rows)!=a.repeats*len(PROFILES)*2:return False
    for x in rows:
        if x["instance_sha256"]!=r["sha256"] or x["status"]!="Optimal" or x["exit_code"]!="0" or x["timeout"]!="False":return False
    return True

def execute_instances(a,mode,subset):
    data=check_frozen_code(a)
    if mode=="eval":
        if a.top_k!=data.get("preregistered_top_k",8):
            raise ValueError(f"TOP-K DRIFT: frozen K={data.get('preregistered_top_k')}, requested K={a.top_k}. Testing different K requires a NEW frozen workdir; do not tune on test.")
        if subset=="test":
            prior=a.work/"validation_report.json"
            if not prior.exists() or not jread(prior).get("complete_all_expected"):
                raise ValueError("Validation coverage report must be complete before unsealing TEST; run e1_heldout_analyze.py --subset validation")
    target=data["train" if mode=="collect" else subset]
    if mode=="eval":
        model=a.work/"model"/"rank_linear.v1"
        if not model.is_file() or not model.with_suffix(model.suffix+".json").is_file():
            raise ValueError("No trained B1 model. Run train first")
        meta=jread(Path(str(model)+".json"))
        provenance=a.work/"training_coverage.json"
        if not provenance.exists() or digest(model)!=jread(provenance)["model_sha256"]:
            raise ValueError("MODEL DRIFT since training; do not mix model versions in one evaluation")
        if set(meta.get("train_instance_sha256",[])) & set(x["sha256"] for x in data["validation"]+data["test"]):
            raise ValueError("TRAIN/VALIDATION/TEST LEAKAGE detected in model metadata")
        if meta.get("training_source_tree_sha256")!=jread(a.work/"code_attestation.json")["source_tree_sha256"]:
            raise ValueError("Training/source version mismatch")
    done=0;failed=[];skipped=0
    for i,r in enumerate(target):
        if a.max_new>0 and done>=a.max_new:break
        # Keep validation and test results separate and unambiguous.
        folder=(a.work/"collect"/file_name(r)) if mode=="collect" else (a.work/"eval"/subset/file_name(r))
        good=collected_ok(folder,r) if mode=="collect" else evaluated_ok(folder,r,a)
        if good:
            skipped+=1
            continue
        if folder.exists():
            if not a.retry_failed:
                failed.append({"path":r["path"],"reason":"incomplete_existing_directory","location":str(folder)})
                print(f"INCOMPLETE {folder}: pass --retry-failed to archive prior attempt and retry",flush=True)
                continue
            archive=a.work/"failed_attempts"/(mode+"_"+(subset or "train"))
            archive.mkdir(parents=True,exist_ok=True)
            target_archive=archive/(folder.name+"_"+str(time.time_ns()))
            shutil.move(str(folder),str(target_archive))
        folder.parent.mkdir(parents=True,exist_ok=True)
        instlist=a.work/"instance_lists"/(file_name(r)+".txt")
        instlist.parent.mkdir(parents=True,exist_ok=True)
        instlist.write_text(r["path"]+"\n",encoding="utf-8")
        cmd=[sys.executable,str(a.e1_dir/"e1_run.py"),mode,"--repo",str(a.repo),"--instances-file",str(instlist),
             "--out",str(folder),"--profiles",",".join(PROFILES),"--skip-build","--timeout",str(a.timeout)]
        if mode=="eval":
            cmd += ["--model",str(a.work/"model"/"rank_linear.v1"),"--repeats",str(a.repeats),
                    "--warmups",str(a.warmups),"--top-k",str(a.top_k),"--seed",str(a.seed+i)]
        rc=run_subproc(cmd,a.work/"driver_logs"/(mode+"_"+(subset or "train")) /(file_name(r)+".log"))
        done+=1
        ok=collected_ok(folder,r) if mode=="collect" else evaluated_ok(folder,r,a)
        if rc!=0 or not ok:failed.append({"path":r["path"],"reason":f"rc={rc},validation={ok}","location":str(folder)})
        else:print(f"SUCCESS {mode} {r['path']}",flush=True)
    status={"phase":mode,"subset":subset if mode=="eval" else "train","total_expected":len(target),
            "valid_completed":sum(bool(collected_ok(a.work/"collect"/file_name(r),r) if mode=="collect" else evaluated_ok(a.work/"eval"/subset/file_name(r),r,a)) for r in target),
            "new_attempts":done,"skipped":skipped,"failed_this_invocation":failed,"params":signature(a)}
    jwrite(a.work/"phase_status"/(mode+"_"+(subset or "train")+".json"),status)
    print("PHASE STATUS",json.dumps({k:v for k,v in status.items() if k!="failed_this_invocation"},ensure_ascii=False),flush=True)
    if failed:print("FAILED/INCOMPLETE",json.dumps(failed,ensure_ascii=False,indent=2),flush=True)
    if status["valid_completed"]!=len(target):
        print("INCOMPLETE: summary is not a publishable complete-data analysis",flush=True)

def train(a):
    data=check_frozen_code(a)
    entries=[];miss=[]
    for r in data["train"]:
        sub=a.work/"collect"/file_name(r)
        if not collected_ok(sub,r):miss.append(r["path"]);continue
        m=jread(sub/"train_manifest.json")
        entries.extend(m["traces"])
    if miss and not a.allow_incomplete_train:
        raise ValueError(f"Training collection incomplete ({len(miss)} missing), first: {miss[:6]}. Retry failed samples or explicitly --allow-incomplete-train for a NON-PUBLISHABLE pilot.")
    if not entries:raise ValueError("No valid training traces")
    model=a.work/"model"/"rank_linear.v1"
    if model.exists():raise ValueError("Model already exists; do not silently overwrite it. Use a new --work directory for a new experiment.")
    m={"kind":"VOC-LS/E1-TRAIN-TRACE-v1","source_tree_sha256":jread(a.work/"code_attestation.json")["source_tree_sha256"],
       "binary_sha256":jread(a.work/"code_attestation.json")["executable_sha256"],"traces":entries}
    jwrite(a.work/"train_manifest.json",m)
    cmd=[sys.executable,str(a.e1_dir/"e1_train.py"),"--manifest",str(a.work/"train_manifest.json"),
         "--model",str(model),"--ridge",str(a.ridge),"--max-rows-per-instance",str(a.max_rows),"--seed",str(a.seed)]
    rc=run_subproc(cmd,a.work/"driver_logs"/"train.log")
    if rc or not model.is_file():raise RuntimeError("Model training failed: see driver_logs/train.log")
    jwrite(a.work/"training_coverage.json",{"instances_expected":len(data["train"]),"instances_used":len({x["instance_sha256"] for x in entries}),
         "used_incomplete_train":bool(miss),"missing":miss,"model_sha256":digest(model),"ridge":a.ridge,"max_rows":a.max_rows})
    print(f"TRAIN COMPLETE model={model}, instances={len({x['instance_sha256'] for x in entries})}; incomplete={bool(miss)}")


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("phase",choices=["prepare","collect","train","eval","status"])
    ap.add_argument("--repo",type=Path,default=Path.home()/"code/dass/pathwyse")
    ap.add_argument("--e1-dir",type=Path,default=Path.home()/"code/dass/voc_ls_e1")
    ap.add_argument("--split-dir",type=Path,default=Path.home()/"code/dass/e1_split")
    ap.add_argument("--work",type=Path,default=Path.home()/"code/dass/e1_heldout_v1")
    ap.add_argument("--validation-fraction",type=float,default=0.20)
    ap.add_argument("--seed",type=int,default=20261115)
    ap.add_argument("--subset",choices=["validation","test"],default="validation")
    ap.add_argument("--timeout",type=int,default=600)
    ap.add_argument("--repeats",type=int,default=10)
    ap.add_argument("--warmups",type=int,default=2)
    ap.add_argument("--top-k",type=int,default=8)
    ap.add_argument("--max-new",type=int,default=0,help="Limit NEW per-instance launches; 0=all; supports batches")
    ap.add_argument("--ridge",type=float,default=.01)
    ap.add_argument("--max-rows",type=int,default=8000,help="Max trace rows sampled per profile/instance")
    ap.add_argument("--retry-failed",action="store_true",help="Archive incomplete output and rerun")
    ap.add_argument("--allow-incomplete-train",action="store_true",help="Pilot only; visible audit marker")
    args=ap.parse_args()
    args.repo=args.repo.expanduser().resolve()
    if not (args.repo/"CMakeLists.txt").exists() and (args.repo/"pathwyse-main"/"CMakeLists.txt").exists():args.repo=args.repo/"pathwyse-main"
    args.e1_dir=args.e1_dir.expanduser().resolve();args.split_dir=args.split_dir.expanduser().resolve();args.work=args.work.expanduser().resolve()
    if args.phase!="status" and not (args.repo/"pathwyse.set").is_file():ap.error("Invalid --repo")
    if args.phase in ("collect","train","eval") and any(not (args.e1_dir/n).is_file() for n in ("e1_run.py","e1_train.py")):ap.error("Cannot find original e1_run.py and e1_train.py")
    if not 0<args.validation_fraction<.5 or args.timeout<1 or args.repeats<1 or args.warmups<0 or not 1<=args.top_k<=64 or args.max_new<0:ap.error("Invalid fraction/timeout/repeats/warmups/top-k/max-new")
    if args.phase=="prepare":prepare(args)
    elif args.phase=="collect":execute_instances(args,"collect",None)
    elif args.phase=="train":train(args)
    elif args.phase=="eval":execute_instances(args,"eval",args.subset)
    elif args.phase=="status":
        print("DATASET",str(args.work/"dataset.json"))
        for p in sorted((args.work/"phase_status").glob("*.json")):
            s=jread(p);print(p.name, s.get("valid_completed"),"/",s.get("total_expected"),"valid")

if __name__=="__main__":main()
