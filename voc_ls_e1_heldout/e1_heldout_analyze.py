#!/usr/bin/env python3
"""Coverage-aware per-instance E1 held-out analysis; NO exclusion of failures.

Primary metric: complete-process paired time B0-B1 (positive: learned faster).
Intervals bootstrap INDEPENDENT INSTANCES, not repeated runs as IID samples.
Results are preliminary if any held-out instance is missing/failed.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import random
import statistics as st
import sys


def read_json(p):return json.loads(p.read_text(encoding="utf-8"))
def bootstrap(x, seed=4242, iters=5000):
    if len(x)<2:return None
    rng=random.Random(seed)
    samples=sorted(st.mean(rng.choices(x,k=len(x))) for _ in range(iters))
    return [samples[int(.025*(iters-1))],samples[int(.975*(iters-1))]]

def evaluate(work:Path,subset:str,expected_repeats:int|None=None):
    dataset=read_json(work/"dataset.json")
    expected=dataset[subset]
    profile_names=("p0_r","p1_s")
    byprofile=defaultdict(list)
    per_instance=[];missing=[]
    for item in expected:
        folder=work/"eval"/subset/(item["sha256"][:16]+"_"+Path(item["path"]).stem[:24])
        md=folder/"metadata.json"
        csvpath=folder/"measurements.csv"
        if not md.exists() or not csvpath.exists():
            missing.append({"instance":item["path"],"reason":"missing_result", "path":str(folder)})
            continue
        meta=read_json(md)
        if not meta.get("completed") or meta.get("overlap_smoke") or meta.get("kind")!="VOC-LS/E1-eval-v1":
            missing.append({"instance":item["path"],"reason":meta.get("error","incomplete_or_smoke")})
            continue
        repeats=meta["repeats"]
        if expected_repeats is not None and repeats!=expected_repeats:
            raise ValueError(f"Changed repeats for {item['path']}: {repeats} vs {expected_repeats}")
        if set(meta.get("profiles",[]))!=set(profile_names):
            raise ValueError(f"Missing profiles for {item['path']}: {meta.get('profiles')}")
        with csvpath.open(newline="",encoding="utf-8") as f:rows=list(csv.DictReader(f))
        if len(rows)!=repeats*2*len(profile_names):
            missing.append({"instance":item["path"],"reason":f"incomplete_measurements {len(rows)} vs {repeats*4}"})
            continue
        groups=defaultdict(dict)
        for r in rows:
            if r["instance_sha256"]!=item["sha256"] or r["timeout"]!="False" or r["exit_code"]!="0" or r["status"]!="Optimal":
                raise ValueError(f"Invalid measured result {item['path']} {r['round']} {r['policy']}: {r}")
            key=(r["profile"],int(r["round"]))
            pol=r["policy"]
            if pol in groups[key]:raise ValueError("Duplicate pair "+str(key))
            groups[key][pol]=r
        all_good=True
        temp_summaries=[]
        for profile in profile_names:
            pairs=[]
            for rn in range(1,repeats+1):
                pair=groups.get((profile,rn),{})
                if set(pair)!={"baseline","learned"}:
                    missing.append({"instance":item["path"],"reason":f"missing pair profile {profile} round {rn}"})
                    all_good=False;break
                a,b=pair["baseline"],pair["learned"]
                if abs(float(a["objective"])-float(b["objective"]))>1e-6:
                    raise ValueError(f"EXACTNESS FAILURE: {item['path']}, {profile}, round {rn}")
                t0,t1=float(a["wall_s"]),float(b["wall_s"])
                if not math.isfinite(t0+t1) or min(t0,t1)<=0:raise ValueError("Invalid duration")
                ext0,ext1=int(a["candidate_calls"]),int(b["candidate_calls"])
                if ext0<=0 or ext1<=0 or int(b["scored_labels"])<ext1:
                    raise ValueError(f"No active learned scoring in {item['path']}")
                pairs.append({"time_delta_s":t0-t1,"saving_pct":100*(t0-t1)/t0,"log_speedup":math.log(t0/t1),
                              "baseline_s":t0,"learned_s":t1,"ext_delta":ext0-ext1,"b1_ext":ext1,
                              "b0_ext":ext0,"iter_delta":int(a["dssr_iterations"])-int(b["dssr_iterations"]),
                              "b0_selector_ms":int(a["selector_ns"])/1e6,"b1_selector_ms":int(b["selector_ns"])/1e6,
                              "scored":int(b["scored_labels"]),"reordered":int(b["reordered_choices"]),"num_rounds":repeats})
            if not all_good:break
            mean=lambda k:st.mean(p[k] for p in pairs)
            summary={"instance":item["path"],"profile":profile,"repeats":repeats,
                    "b0_mean_s":mean("baseline_s"),"b1_mean_s":mean("learned_s"),
                    "paired_mean_B0_minus_B1_s":mean("time_delta_s"),
                    "saving_pct":mean("saving_pct"),"mean_log_speedup":mean("log_speedup"),
                    "geomean_speedup":math.exp(mean("log_speedup")),
                    "extension_delta":mean("ext_delta"),"iteration_delta":mean("iter_delta"),
                    "b1_scored_mean":mean("scored"),"b1_reordered_mean":mean("reordered"),
                    "selector_increment_ms":mean("b1_selector_ms")-mean("b0_selector_ms"),
                    "selector_b1_mean_ms":mean("b1_selector_ms"),
                    "faster_rounds":sum(p["time_delta_s"]>0 for p in pairs),
                    "per_round_ci_s":bootstrap([p["time_delta_s"] for p in pairs]),
                    "node_count_from_name":int(__import__("re").search(r"-n(\d+)-",item["path"]).group(1)) if __import__("re").search(r"-n(\d+)-",item["path"]) else None}
            temp_summaries.append(summary)
        if all_good and len(temp_summaries)==len(profile_names):
            for summary in temp_summaries:
                per_instance.append(summary)
                byprofile[summary["profile"]].append(summary)
    full=(len({d["instance"] for d in per_instance})==len(expected) and not missing)
    aggregate={}
    for profile in profile_names:
        items=byprofile.get(profile,[])
        if not items:continue
        deltas=[x["paired_mean_B0_minus_B1_s"] for x in items]
        logs=[x["mean_log_speedup"] for x in items]
        aggregate[profile]={"n_completed_instances":len(items),"n_expected_instances":len(expected),
            "coverage":len(items)/len(expected),"mean_time_delta_s_equal_instance":st.mean(deltas),
            "cross_instance_95pct_ci_s":bootstrap(deltas),"median_time_delta_s":st.median(deltas),
            "geometric_mean_speedup":math.exp(st.mean(logs)),"cross_instance_95pct_ci_log_speedup":bootstrap(logs),
            "win_instances":sum(x>0 for x in deltas),"lose_instances":sum(x<0 for x in deltas),
            "zero_extension_reduction_instances":sum(x["extension_delta"]==0 for x in items),
            "mean_extension_delta":st.mean(x["extension_delta"] for x in items),
            "mean_selector_increment_ms":st.mean(x["selector_increment_ms"] for x in items),
            "mean_reordered_choices":st.mean(x["b1_reordered_mean"] for x in items)}
    report={"kind":"VOC-LS/E1-Heldout-Report-v1","subset":subset,"complete_all_expected":full,
            "n_expected":len(expected),"n_completed_instances":len({d["instance"] for d in per_instance}),
            "n_missing_or_failed":len(missing),"missing_or_failed":missing,"per_instance":per_instance,"cross_instance":aggregate,
            "publication_note":"Even if fully complete: evidence is for a specific frozen dataset. Per-instance bootstrap is exploratory. Partial runs must never be presented as unbiased generalization.",
            "result_status":"COMPLETE" if full else "INCOMPLETE_DO_NOT_CLAIM_GENERALIZATION"}
    target=work/(subset+"_report.json")
    target.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    if per_instance:
        details_csv=work/(subset+"_per_instance.csv")
        with details_csv.open("w",newline="",encoding="utf-8") as f:
            fields=list(per_instance[0].keys())
            w=csv.DictWriter(f,fieldnames=fields)
            w.writeheader();w.writerows(per_instance)
    print(f"E1 {subset.upper()} COVERAGE: {report['n_completed_instances']}/{len(expected)}; {report['result_status']}")
    for prof in profile_names:
        if prof not in aggregate:continue
        x=aggregate[prof]
        print(f"{prof}: instances={x['n_completed_instances']}, wins={x['win_instances']} losses={x['lose_instances']}, "
              f"delta={x['mean_time_delta_s_equal_instance']:+.6f}s, speedup={x['geometric_mean_speedup']:.4f}x, "
              f"CI={x['cross_instance_95pct_ci_s']}, unchanged_ext={x['zero_extension_reduction_instances']}")
    if missing:
        print("MISSING/FAILED INSTANCES (NOT OMITTED):")
        for x in missing:print(" -",x["instance"],x["reason"])
    print("Report:",target)
    return report


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--work",type=Path,default=Path.home()/"code/dass/e1_heldout_v1")
    ap.add_argument("--subset",choices=["validation","test"],default="validation")
    ap.add_argument("--repeats",type=int,default=10)
    a=ap.parse_args()
    r=evaluate(a.work.expanduser().resolve(),a.subset,a.repeats)
    if not r["complete_all_expected"]:sys.exit(3)

if __name__=="__main__":main()
