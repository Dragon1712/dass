#!/usr/bin/env python3
"""Pipeline tests using a fake solver. No claims about real PathWyse results."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent


def call(*args,expect_ok=True):
    p=subprocess.run([sys.executable,*map(str,args)],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if expect_ok and p.returncode:raise AssertionError(f"Test command failed {args}:\n{p.stdout}")
    if not expect_ok and not p.returncode:raise AssertionError(f"Command should have failed: {args}")
    return p.stdout


def main():
    with tempfile.TemporaryDirectory(prefix="voc_ls_e1_selftest_") as temp:
        t=Path(temp)
        root=t/'pathwyse';(root/'src').mkdir(parents=True);(root/'bin').mkdir()
        (root/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\n')
        (root/'src'/'CMakeLists.txt').write_text('project(fake)\n')
        (root/'src'/'fake.cpp').write_text('//fake\n')
        (root/'pathwyse.set').write_text('verbosity=1\nalgo/default/parallel=0\nalgo/default/dssr=restricted\n')
        (root/'instances').mkdir()
        for inst in ['train_a.txt','train_b.txt','test.txt']:(root/'instances'/inst).write_text(inst+'\n')
        exe=root/'bin'/'pathwyse'
        exe.write_text('''#!/usr/bin/env python3
import os,sys,time,csv,math
from pathlib import Path
policy=os.getenv("E1_POLICY","baseline")
if policy=="learned":
    model=Path(os.environ["E1_MODEL"])
    assert model.read_text().startswith("E1LINEAR_V1")
instance=Path(sys.argv[1]).name
s="0 2 1 4"
trace=os.environ.get("E1_TRACE_FILE")
if trace:
    with open(trace,"w") as f:
        w=csv.writer(f)
        w.writerow(["iteration","objective","critical","unreachable","visited","degree","node_frac","direction","attempted","accepted"])
        for i in range(120):
            w.writerow([1+i//30,-i,1+i%13,i%7,i%4,6+i%5,i/120,i%2,1+i%4,(i//7+i%3)%(2+i%4)])
steps=210 if policy=="baseline" else 190
elapsed=.008 if policy=="baseline" else .011
time.sleep(elapsed)
print("Solution Status                  : Optimal")
print("Obj                              : -12492.0")
print("Tour                             : 0 2 1 4")
print("Consumption                      : 100")
print("PWDefault Time                   :",elapsed)
print("E1_STATS policy=%s candidate_calls=%d scored_labels=%d selector_ns=%d trace_rows=%d iterations=%d top_k=8"%(
    policy,steps, 0 if policy=="baseline" else 8*steps, 0 if policy=="baseline" else 80000,
    120 if trace else 0,3))
''')
        exe.chmod(0o755)
        train=t/'train.txt';test=t/'test.txt'
        train.write_text('instances/train_a.txt\ninstances/train_b.txt\n')
        test.write_text('instances/test.txt\n')
        collect=t/'collected';model=t/'model'/'linear.v1';evaluation=t/'evaluation'
        print('== Collect B0 supervision traces ==')
        call(HERE/'e1_run.py','collect','--repo',root,'--instances-file',train,'--out',collect,'--skip-build','--timeout','5')
        assert (collect/'train_manifest.json').exists()
        print('== Fit a genuine linear model, no test leakage ==')
        call(HERE/'e1_train.py','--manifest',collect/'train_manifest.json','--model',model)
        assert model.exists() and (model.parent/(model.name+'.json')).exists()
        print('== Paired B0/B1 evaluation ==')
        call(HERE/'e1_run.py','eval','--repo',root,'--instances-file',test,'--out',evaluation,'--model',model,'--skip-build','--repeats','3','--warmups','1','--timeout','5')
        call(HERE/'e1_analyze.py','--out',evaluation)
        r=json.loads((evaluation/'e1_analysis.json').read_text())
        assert r['per_instance_profile'] and r['per_instance_profile'][0]['pairs']==3
        assert set(r['cross_instance'])=={'p0_r','p1_s'}
        print('== Prove test instance overlap is rejected ==')
        result=call(HERE/'e1_run.py','eval','--repo',root,'--instances-file',train,'--out',t/'bad_test','--model',model,'--skip-build','--repeats','1','--warmups','0','--timeout','5',expect_ok=False)
        assert 'TRAIN/TEST CONTENT LEAKAGE' in result
        print('ALL E1 SELFTESTS PASSED (simulation only)')


if __name__=='__main__':main()
