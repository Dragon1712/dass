#!/usr/bin/env python3
"""Local offline fixture test: patch anchors, config generation, mock PathWyse process, CSV outputs.
Does NOT claim real PathWyse solver validation.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import tempfile

BASE=Path(__file__).resolve().parent

def main():
    with tempfile.TemporaryDirectory(prefix='e0standard_selftest_') as td:
        d=Path(td); repo=d/'pathwyse';repo.mkdir()
        (repo/'src').mkdir();(repo/'instances').mkdir()
        (repo/'src'/'CMakeLists.txt').write_text('# fake project\n')
        (repo/'pathwyse.set').write_text('verbosity = 1\nalgo/default/parallel = 0\nalgo/default/dssr = restricted\n')
        (repo/'instances'/'toy.txt').write_text('NAME : Toy\nSIZE : 5\nDIRECTED : 1\nCYCLIC : 1\nORIGIN : 0\nDESTINATION : 4\nRESOURCES : 1\nRES_NAMES : 0\nRES_TYPE\n0 CAP\nEND\nRES_BOUND\n0 0 12\nEND\nEDGE_COST\n0 2 100\n2 1 100\n1 4 100\nEND\nNODE_COST\n0 -10735\nEND\nNODE_CONSUMPTION\n0 0 0\n0 1 2\n0 2 1\n0 4 6\nEND\n')
        (repo/'instances'/'spprclib').mkdir()
        (repo/'instances'/'spprclib'/'A-n54-k7-149.sppcc').write_text('placeholder')
        bin=repo/'bin';bin.mkdir()
        fake=bin/'pathwyse'
        fake.write_text('''#!/usr/bin/env python3
import sys,os,pathlib,time
p=pathlib.Path(sys.argv[1]);cfg=pathlib.Path(sys.argv[3]).read_text()
diagnostic='output/write = 1' in cfg
strong='algo/default/dssr = standard' in cfg
is_toy=p.name=='toy.txt'
obj=-10435 if is_toy else -12492
if diagnostic:
  out=pathlib.Path('output')/'mock-run';out.mkdir(parents=True,exist_ok=True)
  (out/'PWDefault.csv').write_text('iterations,it_ext_fw,it_ext_bw,time\\n1,6,3,0.001\\n2,10,12,0.002\\n')
  (out/'PWDefault_LM.csv').write_text('iterations,e0_dom_checks_fw,e0_dom_checks_bw,e0_dom_time_fw_ns,e0_dom_time_bw_ns,e0_inference_calls,e0_inference_time_ns,nlabels_inserted,ndominated,nclosed,ins_attempts_fw,ins_attempts_bw,njoin\\n1,14,25,2000,3000,0,0,50,12,14,100,110,2\\n2,16,24,3000,4000,0,0,60,13,15,120,130,5\\n')
print('[ Optimization ]')
print('PWDefault Time : 0.002')
print('[ Solution ]')
print('  Solution Status : Optimal')
print('  Obj : '+str(obj))
print('  Tour : 0 2 1 4')
print('  Consumption : 9')
''')
        fake.chmod(0o755)
        out=d/'manifest.json'
        subprocess.run([sys.executable,str(BASE/'make_dataset.py'),'--repo',str(repo),'--out',str(out),'--synthetic-count','18','--ood-count','0'],check=True,capture_output=True)
        mani=json.loads(out.read_text())
        assert mani['counts']=={'smoke':2,'train':12,'val':3,'test':3} or len(mani['instances'])==20
        run=d/'results'
        subprocess.run([sys.executable,str(BASE/'e0_standard_runner.py'),'--repo',str(repo),'--manifest',str(out),'--out',str(run),'--splits','smoke','--phase','both','--timing-repeats','2','--diagnostic-repeats','1','--warmups','1','--timeout','5'],check=True,capture_output=True)
        summary=run/'summary_per_instance.csv'; diag=run/'diagnostics_totals.csv'
        assert summary.is_file() and diag.is_file()
        assert len(summary.read_text().splitlines())==5 # 2 instances x2 profiles
        assert len(diag.read_text().splitlines())==5
        assert (run/'metadata.json').is_file()
        import csv
        with diag.open() as f:records=list(csv.DictReader(f))
        for x in records:
            assert int(x['extensions_total'])==31
            assert int(x['dom_checks_total'])==79
            assert int(x['inference_calls'])==0
        print('MOCK RUNNER SELFTEST PASS: 2 instances x2 profiles, wall timing, per-round counts, inference=0, split manifest')
        # Fixture patch anchor test
        mockroot=d/'mocksource';(mockroot/'src/core/algorithms/dynamic_programming/PW_default').mkdir(parents=True)
        base=mockroot/'src/core/algorithms/dynamic_programming/PW_default'
        (base/'LM_default.h').write_text('#include <set>\nclass A {\n    bool dominates(LabelAdv *l1, LabelAdv *l2);\nprotected:\n\n    std::string name;\n};\n')
        (base/'LM_default.cpp').write_text('#include <algorithm>\nLMDefault::LMDefault(Problem* problem) {\n}\n//Returns true if l1 dominates l2\nbool LMDefault::dominates(LabelAdv* l1, LabelAdv* l2)  {\nreturn false;\n}\n    collector.init("ndominated", 0);\n    collector.collect("ndominated", ndominated_fw + ndominated_bw);\n')
        (base/'PW_default.cpp').write_text('    label_manager->setIteration(iterations);\n    label_manager->initLM();\n')
        subprocess.run([sys.executable,str(BASE/'patch_pathwyse.py'),'--repo',str(mockroot)],check=True,capture_output=True)
        assert 'VOC_E0_PROFILE_PATCH_V1' in (base/'LM_default.cpp').read_text()
        subprocess.run([sys.executable,str(BASE/'patch_pathwyse.py'),'--repo',str(mockroot),'--restore'],check=True,capture_output=True)
        assert 'VOC_E0_PROFILE_PATCH_V1' not in (base/'LM_default.cpp').read_text()
        print('MOCK PATCH SELFTEST PASS: exact anchors, deterministic restore')
if __name__=='__main__':main()
