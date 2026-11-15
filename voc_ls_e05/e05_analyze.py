#!/usr/bin/env python3
"""Analyze E0.5 2x2 factorial using blocked, paired measurements.

Uses one instance at a time; reports descriptive statistics and paired bootstrap CIs.
No claims of statistical significance are made from isolated example instances.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import random
import statistics as stat

CORE=["p0_r","p1_r","p0_s","p1_s"]
EXTRA=["p1_s_auto","p0_r_collect","stock"]

def ci_bootstrap(nums,seed=42,n=4000):
    if not nums:return None
    rnd=random.Random(seed)
    b=[stat.mean(rnd.choices(nums,k=len(nums))) for _ in range(n)]
    b.sort()
    return [b[int(.025*(n-1))],b[int(.975*(n-1))]]

def evaluate(out:Path):
    with (out/"measurements.csv").open(newline="",encoding="utf-8") as f:rows=list(csv.DictReader(f))
    if not rows:raise ValueError("No measurement rows")
    groups=defaultdict(list)
    for x in rows:
        if x["run_type"]!="measure":continue
        if x["status"]!="Optimal" or x["exit_code"]!="0" or x["timeout"]!="False":
            raise ValueError("Failed run detected: "+str(x))
        groups[x["instance"]].append(x)
    all_reports=[]
    for instance,one in groups.items():
        profiles=defaultdict(list)
        rounds=defaultdict(dict)
        for row in one:
            p=row["profile"];profiles[p].append(float(row["wall_s"]))
            key=int(row["round"])
            if p in rounds[key]:raise ValueError(f"Duplicate round/profile: {instance},{key},{p}")
            rounds[key][p]=float(row["wall_s"])
        summary={p:{"n":len(v),"mean_s":stat.mean(v),"median_s":stat.median(v),"sd_s":stat.stdev(v) if len(v)>1 else 0,"cv_pct":100*stat.stdev(v)/stat.mean(v) if len(v)>1 else 0,"min_s":min(v),"max_s":max(v)} for p,v in profiles.items()}
        required_core=all(all(k in r for k in CORE) for r in rounds.values())
        if not required_core:raise ValueError(f"Incomplete factorial blocks for {instance}; refusing to compute interactions")
        d={"parallel_under_restricted":[],"parallel_under_standard":[],"standard_under_serial":[],"standard_under_parallel":[],"interaction_difference_in_differences":[],"interaction_log_ratio":[],"auto_under_parallel_standard":[],"collection_under_serial_restricted":[],"stock_vs_parallel_standard_auto":[]}
        for rnd in sorted(rounds):
            x=rounds[rnd]
            d["parallel_under_restricted"].append(x["p1_r"]-x["p0_r"])
            d["parallel_under_standard"].append(x["p1_s"]-x["p0_s"])
            d["standard_under_serial"].append(x["p0_s"]-x["p0_r"])
            d["standard_under_parallel"].append(x["p1_s"]-x["p1_r"])
            d["interaction_difference_in_differences"].append((x["p1_s"]-x["p0_s"])-(x["p1_r"]-x["p0_r"]))
            d["interaction_log_ratio"].append(math.log(x["p1_s"]/x["p0_s"])-math.log(x["p1_r"]/x["p0_r"]))
            if "p1_s_auto" in x:d["auto_under_parallel_standard"].append(x["p1_s_auto"]-x["p1_s"])
            if "p0_r_collect" in x:d["collection_under_serial_restricted"].append(x["p0_r_collect"]-x["p0_r"])
            if "stock" in x and "p1_s_auto" in x:d["stock_vs_parallel_standard_auto"].append(x["stock"]-x["p1_s_auto"])
        effects={k:{"mean_difference_s":stat.mean(v),"bootstrap_95pct_ci_s":ci_bootstrap(v),"paired_rounds":len(v)} for k,v in d.items() if v}
        # This value is exponentiated log interaction, and its CI is NOT in seconds.
        l=d["interaction_log_ratio"]
        effects["interaction_log_ratio"]={"mean_log_effect":stat.mean(l),"ratio_of_parallel_ratios":math.exp(stat.mean(l)),"bootstrap_95pct_ci_ratio":[math.exp(x) for x in ci_bootstrap(l)],"paired_rounds":len(l)}
        report={"instance":instance,"summary":summary,"effects":effects,"rounds":len(rounds)}
        all_reports.append(report)
        print("\n===",instance,"===")
        print("Profile             n      median(s)        mean(s)       CV(%)")
        for p in CORE+EXTRA:
            if p in summary:
                s=summary[p];print(f"{p:<20}{s['n']:>3d}{s['median_s']:>15.6f}{s['mean_s']:>15.6f}{s['cv_pct']:>12.2f}")
        print("\nPaired effects (positive = SLOWER second condition, negative = FASTER):")
        for k,e in effects.items():
            if k!="interaction_log_ratio":print(f"  {k}: {e['mean_difference_s']:+.6f}s, bootstrap CI {e['bootstrap_95pct_ci_s']}")
            else:print(f"  interaction ratio-of-ratios: {e['ratio_of_parallel_ratios']:.4f}, bootstrap CI {e['bootstrap_95pct_ci_ratio']}")
    (out/"e05_analysis.json").write_text(json.dumps(all_reports,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    diag_file=out/"diagnostics.json"
    if diag_file.is_file():
        print("\nSearch diagnostics (ONE RUN per cell; do not compare their wall times):")
        for z in json.loads(diag_file.read_text(encoding="utf-8")):
            print(f"  {z['profile']:<5} {Path(z['instance']).name:<28} DSSR rounds={z['per_iteration_records']}, total label extensions={z.get('total_extensions_across_iterations')}, final-round extensions={z.get('last_iteration_extensions')}")
    print("\nWrote",out/"e05_analysis.json")
    print("WARNING: bootstrap CIs describe run-to-run variability on these instances; they DO NOT establish generalization across problem instances.")

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--out",type=Path,default=Path.home()/"code/dass/e05_results")
    args=ap.parse_args();evaluate(args.out.expanduser().resolve())

if __name__=="__main__":main()
