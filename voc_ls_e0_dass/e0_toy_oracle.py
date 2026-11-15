#!/usr/bin/env python3
"""Independent brute-force verifier for the official PathWyse 5-node toy instance.

Not a general RCSPP solver. Intended only for tiny instances with one CAP resource
and node-consumption semantics like the official toy. Enumerates elementary paths.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def solve_toy(path: Path):
    tags = {}
    blocks = {"RES_BOUND", "NODE_COST", "NODE_CONSUMPTION", "EDGE_COST"}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in blocks:
            current = line
            tags[current] = []
            continue
        if line == "END":
            current = None
            continue
        if current is not None:
            tags[current].append(line.split())
        elif ":" in line:
            k, v = line.split(":", 1)
            tags[k.strip()] = v.strip()
    n = int(tags["SIZE"])
    if n > 12 or tags.get("DIRECTED") != "1" or tags.get("RESOURCES") != "1":
        raise ValueError("This independent oracle only accepts the official tiny directed single-resource toy format")
    origin, dest = int(tags["ORIGIN"]), int(tags["DESTINATION"])
    rb = tags["RES_BOUND"]
    if len(rb) != 1 or rb[0][0] != "0":
        raise ValueError("Expected one capacity resource with lower/upper bound")
    lb, ub = float(rb[0][1]), float(rb[0][2])
    node_cost = {int(a):float(b) for a,b in tags["NODE_COST"]}
    node_consumption = {int(node):float(val) for rid,node,val in tags["NODE_CONSUMPTION"] if rid=="0"}
    edge_cost = {(int(a),int(b)):float(c) for a,b,c in tags["EDGE_COST"]}
    best = None
    explored = 0
    def walk(u, visited, path_seq, cost, consumption):
        nonlocal explored, best
        if u == dest:
            explored += 1
            if lb <= consumption <= ub and (best is None or cost < best["objective"]):
                best = {"objective": cost, "tour": path_seq[:], "consumption": consumption}
            return
        for v in range(n):
            if v not in visited and (u,v) in edge_cost:
                walk(v, visited|{v}, path_seq+[v], cost+edge_cost[(u,v)]+node_cost.get(v,0), consumption+node_consumption.get(v,0))
    walk(origin,{origin},[origin],node_cost.get(origin,0),node_consumption.get(origin,0))
    return {"instance":str(path), "best":best, "elementary_paths_explored": explored,
            "verified": best is not None and best["objective"] == -10435.0,
            "scope":"independent exhaustive oracle, official toy only"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instance", type=Path)
    args=ap.parse_args()
    result=solve_toy(args.instance)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
