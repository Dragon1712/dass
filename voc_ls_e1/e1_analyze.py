#!/usr/bin/env python3
"""Analyze exact paired B0 vs Always Learned B1 runs from e1_run.py.

Per-instance/condition differences are paired by round. Cross-instance summaries
weight each *test instance* equally, never treating labels or repeats as IID.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import math
import random
import statistics as st


def bootstrap_ci(x,seed=14,draws=4000):
    rng=random.Random(seed)
    if len(x)==1:return [x[0],x[0]]
    v=sorted(st.mean(rng.choices(x,k=len(x))) for _ in range(draws))
    return [v[int(.025*(draws-1))],v[int(.975*(draws-1))]]


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args();out=args.out.expanduser().resolve()
    md=json.loads((out/'metadata.json').read_text(encoding='utf-8'))
    if md.get('kind')!='VOC-LS/E1-eval-v1' or not md.get('completed'):
        ap.error('Evaluation must be complete before analysis')
    with (out/'measurements.csv').open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
    if not rows:raise ValueError('Empty measurements.csv')
    datasets=defaultdict(lambda:defaultdict(dict))
    for r in rows:
        if r['exit_code']!='0' or r['timeout']!='False' or r['status']!='Optimal':
            raise ValueError('Failed or timed out row: '+str(r))
        key=(r['instance'],r['profile'])
        rnd=int(r['round']);pol=r['policy']
        if pol in datasets[key][rnd]:raise ValueError(f'Duplicate run: {key}, {rnd}, {pol}')
        datasets[key][rnd][pol]=r
    reports=[]
    for (instance,profile), rounds in sorted(datasets.items()):
        changes=[];pct=[];ext_diffs=[];selector_ms=[];scored=[];reordered=[];rel=[];steps=[];b0=[];b1=[]
        for rn,pair in sorted(rounds.items()):
            if set(pair)!={'baseline','learned'}:raise ValueError(f'Incomplete pair {instance}/{profile}/{rn}')
            a,b=pair['baseline'],pair['learned']
            if abs(float(a['objective'])-float(b['objective']))>1e-6:raise ValueError('B0/B1 objective mismatch')
            t0,t1=float(a['wall_s']),float(b['wall_s'])
            if t0<=0 or t1<=0:raise ValueError('Invalid wall time')
            b0.append(t0);b1.append(t1);changes.append(t0-t1);pct.append((t0-t1)/t0*100)
            ext_diffs.append(int(a['candidate_calls'])-int(b['candidate_calls']))
            selector_ms.append(int(b['selector_ns'])/1e6)
            scored.append(int(b['scored_labels']))
            reordered.append(int(b.get('reordered_choices') or 0))
            steps.append((int(a['dssr_iterations']),int(b['dssr_iterations'])))
            rel.append(t0/t1)
        report={'instance':instance,'profile':profile,'pairs':len(changes),
                'B0_mean_wall_s':st.mean(b0),'B1_mean_wall_s':st.mean(b1),
                'B0_median_wall_s':st.median(b0),'B1_median_wall_s':st.median(b1),
                'paired_mean_B0_minus_B1_s':st.mean(changes),
                'paired_bootstrap_95pct_ci_s':bootstrap_ci(changes),
                'paired_mean_percentage_saving':st.mean(pct),
                'mean_speedup_B0_over_B1':st.mean(rel),
                'paired_mean_extension_reduction':st.mean(ext_diffs),
                'B1_mean_selector_ms':st.mean(selector_ms),'B1_mean_scored_labels':st.mean(scored),
                'B1_mean_reordered_choices':st.mean(reordered),
                'iteration_counts_by_round':steps,
                'B1_faster_rounds':sum(x>0 for x in changes),
                'B0_faster_rounds':sum(x<0 for x in changes)}
        reports.append(report)
        print(f'{instance} [{profile}]: B0={report["B0_mean_wall_s"]:.6f}s '
              f'B1={report["B1_mean_wall_s"]:.6f}s, B0-B1={report["paired_mean_B0_minus_B1_s"]:+.6f}s '
              f'(95% paired CI={report["paired_bootstrap_95pct_ci_s"]}), '
              f'B1 faster {report["B1_faster_rounds"]}/{len(changes)} rounds; '
              f'extension difference={report["paired_mean_extension_reduction"]:+.1f}')
    cross={}
    for profile in sorted({p['profile'] for p in reports}):
        data=[p for p in reports if p['profile']==profile]
        changes=[p['paired_mean_B0_minus_B1_s'] for p in data]
        cross[profile]={'n_instances':len(data),'equal_instance_mean_delta_s':st.mean(changes),
                        'instance_bootstrap_95pct_ci_s':bootstrap_ci(changes),
                        'win_instances':sum(x>0 for x in changes),'lose_instances':sum(x<0 for x in changes)}
        print('CROSS-INSTANCE',profile,cross[profile])
        if len(data)<5: print('WARNING: too few held-out instances to support a generalization claim')
    if md.get('overlap_smoke'):
        print('WARNING: TRAIN/TEST OVERLAP SMOKE -- results are NOT valid for publishable effectiveness claims')
    (out/'e1_analysis.json').write_text(json.dumps({'per_instance_profile':reports,'cross_instance':cross,'train_test_overlap_smoke':md.get('overlap_smoke'), 'limits':'No causal effect of learning can be claimed from same-instance training smoke; per-round bootstrap is not across-instance uncertainty.'},indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('Wrote',out/'e1_analysis.json')


if __name__=='__main__':main()
