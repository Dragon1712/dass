#!/usr/bin/env python3
"""Offline, instance-disjoint SPPRCLIB train/test split (fixed seed, SHA checks)."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import random


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def main():
    ap=argparse.ArgumentParser(description='Create a reproducible instance-level SPPRCLIB train/test split')
    ap.add_argument('--repo',type=Path,default=Path.home()/'code/dass/pathwyse')
    ap.add_argument('--glob',default='instances/spprclib/*.sppcc',help='PathWyse-relative glob')
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--train-fraction',type=float,default=0.7)
    ap.add_argument('--seed',type=int,default=20261115)
    args=ap.parse_args()
    if not 0.1<args.train_fraction<0.9:ap.error('Train fraction must be between 0.1 and 0.9')
    repo=args.repo.expanduser().resolve()
    if not (repo/'pathwyse.set').is_file() and (repo/'pathwyse-main'/'pathwyse.set').is_file():repo=repo/'pathwyse-main'
    files=sorted(x.resolve() for x in repo.glob(args.glob) if x.is_file())
    if len(files)<3:ap.error(f'Need >=3 independent instances, found {len(files)}. Add more SPPRCLIB files or run an explicitly labeled smoke test.')
    hashes=[sha(x) for x in files]
    if len(set(hashes))<len(files):ap.error('Found duplicate instance file contents; deduplicate dataset first')
    rng=random.Random(args.seed);rng.shuffle(files)
    ntrain=max(1,min(len(files)-1,round(len(files)*args.train_fraction)))
    train,test=files[:ntrain],files[ntrain:]
    out=args.out.expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
    def rel(x):
        try:return str(x.relative_to(repo))
        except ValueError:return str(x)
    (out/'train.txt').write_text('\n'.join(map(rel,train))+'\n',encoding='utf-8')
    (out/'test.txt').write_text('\n'.join(map(rel,test))+'\n',encoding='utf-8')
    manifest={'seed':args.seed,'train_fraction':args.train_fraction,
              'train_instances':[{ 'path':rel(x),'sha256':sha(x)} for x in train],
              'test_instances':[{ 'path':rel(x),'sha256':sha(x)} for x in test]}
    (out/'split_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(f'E1 SPLIT: {len(train)} train, {len(test)} test; manifests in {out}')


if __name__=='__main__':main()
