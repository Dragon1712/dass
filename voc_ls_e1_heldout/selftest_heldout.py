#!/usr/bin/env python3
"""Mock E1 integration test: train/val/test isolation, resumability, analysis.
Does not execute or validate real PathWyse computational results.
"""
from __future__ import annotations
import hashlib,json,os
from pathlib import Path
import subprocess,sys,tempfile
HERE=Path(__file__).resolve().parent
E1=Path(__file__).resolve().parents[1]/"voc_ls_e1"

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def invoke(*args,ok=True):
    v=subprocess.run([sys.executable,*map(str,args)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if ok and v.returncode:raise AssertionError(f"FAILED rc={v.returncode} args={args}\n{v.stdout}")
    if not ok and v.returncode==0:raise AssertionError("Expected failure: "+str(args))
    print(v.stdout[-650:].strip())
    return v

def main():
    if not E1.is_dir():raise RuntimeError("Original E1 scripts needed for selftest")
    with tempfile.TemporaryDirectory(prefix="e1_heldout_test_") as temp:
        t=Path(temp);root=t/"repo"; (root/"src").mkdir(parents=True);(root/"bin").mkdir()
        (root/"CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.20)\n')
        (root/"src"/"CMakeLists.txt").write_text('project(fake)\n')
        (root/"src"/"a.cpp").write_text('//fake')
        (root/"pathwyse.set").write_text('verbosity=1\nalgo/default/parallel=0\nalgo/default/dssr=restricted\n')
        (root/"instances").mkdir()
        training=[];test=[]
        for i in range(12):
            f=root/"instances"/f"dataset_{i:02d}.txt"
            f.write_text(f"distinct mocked instance {i}\n")
            row={"path":"instances/"+f.name,"sha256":sha(f)}
            (training if i<9 else test).append(row)
        split=t/"split";split.mkdir()
        (split/"train.txt").write_text(''.join(r['path']+'\n' for r in training))
        (split/"test.txt").write_text(''.join(r['path']+'\n' for r in test))
        (split/"split_manifest.json").write_text(json.dumps({"train_instances":training,"test_instances":test}))
        exe=root/"bin"/"pathwyse"
        exe.write_text(r'''#!/bin/sh
policy="${E1_POLICY:-baseline}"
if [ -n "${E1_TRACE_FILE:-}" ]; then
  printf 'iteration,objective,critical,unreachable,visited,degree,node_frac,direction,attempted,accepted\n' > "$E1_TRACE_FILE"
  i=1
  while [ "$i" -le 60 ]; do
    printf '1,-%s,2,3,1,5,0.5,1,4,1\n' "$i" >> "$E1_TRACE_FILE"
    i=$((i+1))
  done
  trace_rows=60
else trace_rows=0; fi
if [ "$policy" = baseline ]; then calls=210; scored=0; selector=100000; reorder=0
else calls=202; scored=1010; selector=150000; reorder=20; fi
printf 'Solution Status : Optimal\nObj : -12492.0\nTour : 0 2 1 4\nConsumption : 100\nPWDefault Time : 0.005\n'
printf 'E1_STATS policy=%s candidate_calls=%s scored_labels=%s selector_ns=%s trace_rows=%s reordered_choices=%s iterations=3 top_k=8\n' "$policy" "$calls" "$scored" "$selector" "$trace_rows" "$reorder"
''')
        exe.chmod(0o755)
        work=t/"run";base=["--repo",root,"--split-dir",split,"--e1-dir",E1,"--work",work,"--timeout","5","--repeats","2","--warmups","0"]
        print('=== Prepare ===');invoke(HERE/"e1_heldout.py","prepare",*base)
        dataset=json.loads((work/"dataset.json").read_text())
        assert len(dataset['train'])+len(dataset['validation'])==9 and len(dataset['test'])==3
        print('=== Collect interrupted after one instance ===')
        invoke(HERE/"e1_heldout.py","collect",*base,"--max-new","1")
        print('=== Resume collect ===');invoke(HERE/"e1_heldout.py","collect",*base)
        print('=== Train model ===');invoke(HERE/"e1_heldout.py","train",*base,"--max-rows","30")
        print('=== Early test disallowed ===')
        bad=invoke(HERE/"e1_heldout.py","eval",*base,"--subset","test",ok=False)
        assert 'Validation coverage report' in bad.stdout
        print('=== Validation ===');invoke(HERE/"e1_heldout.py","eval",*base,"--subset","validation")
        invoke(HERE/"e1_heldout_analyze.py","--work",work,"--subset","validation","--repeats","2")
        print('=== Test ===');invoke(HERE/"e1_heldout.py","eval",*base,"--subset","test")
        invoke(HERE/"e1_heldout_analyze.py","--work",work,"--subset","test","--repeats","2")
        test_report=json.loads((work/"test_report.json").read_text())
        assert test_report["complete_all_expected"] and test_report["n_completed_instances"]==3
        assert set(test_report['cross_instance'])=={'p0_r','p1_s'}
        print('=== Leakage check: edit frozen input ===')
        (root/"instances/dataset_11.txt").write_text('CHANGED DATASET\n')
        bad=invoke(HERE/"e1_heldout.py","eval",*base,"--subset","test",ok=False)
        assert 'DATASET DRIFT' in bad.stdout
        print('ALL HELDOUT SELFTESTS PASSED - FAKE SOLVER ONLY')
if __name__=='__main__':main()
