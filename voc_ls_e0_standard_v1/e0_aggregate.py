#!/usr/bin/env python3
"""Aggregate E0 by independent graph (not by repeated solver process) with bootstrap CI."""
from __future__ import annotations
import argparse
import collections
import csv
import json
import math
from pathlib import Path
import random
import statistics

def readcsv(p):
    with p.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))
def q(sorted_a,p):
    if not sorted_a:return None
    x=(len(sorted_a)-1)*p;lo=math.floor(x);hi=math.ceil(x)
    return sorted_a[lo]+(sorted_a[hi]-sorted_a[lo])*(x-lo)
def describe(vals,seed=20261115,boots=4000):
    vals=list(vals)
    if not vals:return {}
    rng=random.Random(seed)
    draws=[statistics.mean(rng.choices(vals,k=len(vals))) for _ in range(boots)]
    draws.sort()
    return {'n':len(vals),'mean':statistics.mean(vals),'median':statistics.median(vals),
            'mean_bootstrap_95_ci':[q(draws,.025),q(draws,.975)],
            'p95_across_instances':q(sorted(vals),.95)}
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);p.add_argument('--seed',type=int,default=20261115);a=p.parse_args()
    out=a.out.expanduser().resolve();file=out/'summary_per_instance.csv'
    if not file.is_file():raise SystemExit('Missing summary_per_instance.csv')
    rows=readcsv(file)
    groups=collections.defaultdict(list)
    for r in rows:
        if not r['median_wall_s']:continue
        groups[(r['split'],r['source'],r['profile'])].append(r)
    report=[]
    for (split,source,profile),items in sorted(groups.items()):
        # Unit for confidence intervals: source graph group, not individual queries.
        bygraph=collections.defaultdict(list)
        for x in items:bygraph[x.get('group') or x['instance_id']].append(x)
        wall=[statistics.mean(float(x['median_wall_s']) for x in g) for g in bygraph.values()]
        speedup=[]
        for g in bygraph.values():
            ratios=[float(x['speedup_over_B0_R']) for x in g if x.get('speedup_over_B0_R')]
            if ratios and all(z>0 for z in ratios):
                speedup.append(math.exp(statistics.mean(math.log(z) for z in ratios)))
        sol=[x for x in items if int(x['successful_runs'])==int(x['total_runs'])]
        d={'split':split,'source':source,'profile':profile,'n_independent_graph_groups':len(bygraph),'n_query_instances':len(items),
           'all_repeats_successful_instances':len(sol),'per_instance_median_wall_s':describe(wall,a.seed),
           'speedup_over_B0_R':describe(speedup,a.seed)}
        if speedup and all(s>0 for s in speedup):
            d['geomean_speedup']=math.exp(statistics.mean(math.log(s) for s in speedup))
        report.append(d)
    output=out/'aggregate_by_split.json'
    output.write_text(json.dumps({'warning':'Unit of inference is independent problem/graph, not repeated process runs; synthetic and real kept separate.',
                                  'report':report},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for row in report:
        t=row['per_instance_median_wall_s']
        print(f"{row['split']:6s} {row['source']:14s} {row['profile']:12s} n={row['n_independent_graph_groups']:3d} mean_per_graph={t.get('mean',0):.6f}s",flush=True)
    print('Saved',output)
if __name__=='__main__':main()
