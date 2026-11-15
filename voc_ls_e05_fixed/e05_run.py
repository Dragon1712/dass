#!/usr/bin/env python3
"""VOC-LS E0.5: reproducible 2x2 parallel x DSSR factorial study.

No GitHub/server network access required; Python 3 standard library only.
Benchmark and diagnostic collection intentionally run separately.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import threading
import re
import shutil
import statistics
import subprocess
import sys
import time

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TOY_EXPECTED = -10435.0
SPPRC_REFERENCE = -12492.0  # From E0 PathWyse run, NOT independently certified.
DEFAULT_SPP = "instances/spprclib/A-n54-k7-149.sppcc"

# Factorial: p=0 (serial) or 1 (two directional std::threads), r/s = restricted/standard DSSR.
CORE = {
    "p0_r": {"parallel": "0", "dssr": "restricted"},
    "p1_r": {"parallel": "1", "dssr": "restricted"},
    "p0_s": {"parallel": "0", "dssr": "standard"},
    "p1_s": {"parallel": "1", "dssr": "standard"},
}
EXTRAS = {
    "p1_s_auto": {"parallel": "1", "dssr": "standard", "autoconfig": "1"},
    "p0_r_collect": {"parallel": "0", "dssr": "restricted", "collection": "0"},
}
FIELDS = ["run_type", "instance", "round", "order", "profile", "status", "objective", "tour", "consumption", "wall_s", "wall_ns", "solver_s", "exit_code", "timeout", "log"]

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(2**20), b""):
            h.update(chunk)
    return h.hexdigest()

def source_hash(root: Path) -> str:
    """Stable digest independent of .git metadata, build binaries and file mtimes."""
    paths = [p for p in (root / "src").rglob("*") if p.is_file() and p.suffix in (".cpp", ".h", ".hpp", ".c", ".cc")]
    paths += [root / "CMakeLists.txt", root / "src" / "CMakeLists.txt", root / "pathwyse.set"]
    h = hashlib.sha256()
    for p in sorted(set(paths)):
        h.update(str(p.relative_to(root)).encode("utf-8") + b"\0")
        h.update(checksum(p).encode("ascii") + b"\n")
    return h.hexdigest()

def version(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=12)
        return p.stdout.strip().splitlines()[0] if p.stdout.strip() else "no output"
    except Exception as e:
        return str(e)

def get_commit(root):
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
    except Exception:
        return "unknown"

def change_config(lines: list[str], changes: dict[str, str]):
    found = set()
    new_lines = []
    for line in lines:
        m = re.match(r"^\s*([^#=]+?)\s*=", line)
        key = m.group(1).strip() if m else None
        if key in changes:
            new_lines.append(f"{key} = {changes[key]}")
            found.add(key)
        else:
            new_lines.append(line)
    for key in sorted(changes.keys() - found):
        new_lines.append(f"{key} = {changes[key]}")
    return "\n".join(new_lines) + "\n"

def make_configs(root: Path, out: Path, include_extras: bool):
    base = (root / "pathwyse.set").read_text(encoding="utf-8").splitlines()
    target = out / "configs"
    target.mkdir(parents=True, exist_ok=True)
    profiles = {**CORE, **(EXTRAS if include_extras else {}), **({"stock": {}} if include_extras else {})}
    configs = {}
    for name, params in profiles.items():
        changes = {
            "verbosity": "1", "algo_names": "PWDefault", "algo_active": "1",
            "algo_parallel": "0", "algo/default/autoconfig": params.get("autoconfig", "0"),
            "algo/default/parallel": params.get("parallel", "0"),
            "algo/default/search": "bidirectional", "algo/default/candidate/type": "node",
            "algo/default/use_visited": "1", "algo/default/compare_unreachables": "1",
            "algo/default/dssr": params.get("dssr", "restricted"),
            "algo/default/ng": "off", "algo/two_cycle_elimination": "1",
            "algo/default/timelimit": "0", "algo/requested_solutions": "1",
            "data_collection/level": params.get("collection", "-1"), "output/write": "0",
        }
        if name == "stock":
            # Exact untouched source config: anchor ONLY, not for factorial attribution.
            content = (root / "pathwyse.set").read_text(encoding="utf-8")
        else:
            content = change_config(base, changes)
        path = target / (name + ".set")
        path.write_text(content, encoding="utf-8")
        configs[name] = path
        if name in CORE:
            # Full per-iteration diagnostic in isolated run, NOT benchmark timing.
            diag = dict(changes)
            diag.update({"verbosity": "3", "data_collection/level": "0", "output/write": "1"})
            (target / (name + "_diagnostic.set")).write_text(change_config(base, diag), encoding="utf-8")
    return configs

def parse_config(path: Path) -> dict[str, str]:
    """The PathWyse parser takes the last occurrence if a key is duplicated."""
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if "=" in line:
            name, val = line.split("=", 1)
            result[name.strip()] = val.strip()
    return result


def validate_config_invariants(configs: dict[str, Path]) -> dict:
    """Fail fast if an ablation control does not actually change its intended key."""
    parsed = {name: parse_config(path) for name, path in configs.items()}

    def only_diff(first: str, second: str, expected_key: str, first_value: str, second_value: str):
        a, b = parsed[first], parsed[second]
        differences = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        if differences != {expected_key} or a.get(expected_key) != first_value or b.get(expected_key) != second_value:
            raise ValueError(
                f"INVALID ABLATION {first} vs {second}: "
                f"expected only {expected_key}: {first_value} -> {second_value}, "
                f"actual differences={sorted(differences)}, values={a.get(expected_key)!r}/{b.get(expected_key)!r}"
            )
        return {"first": first, "second": second, "changed_only": expected_key, "values": [first_value, second_value], "verified": True}

    checks = [
        only_diff("p0_r", "p1_r", "algo/default/parallel", "0", "1"),
        only_diff("p0_s", "p1_s", "algo/default/parallel", "0", "1"),
        only_diff("p0_r", "p0_s", "algo/default/dssr", "restricted", "standard"),
        only_diff("p1_r", "p1_s", "algo/default/dssr", "restricted", "standard"),
    ]
    if "p1_s_auto" in parsed:
        checks.append(only_diff("p1_s", "p1_s_auto", "algo/default/autoconfig", "0", "1"))
        if checksum(configs["p1_s"]) == checksum(configs["p1_s_auto"]):
            raise ValueError("AUTO ABLATION INVALID: p1_s and p1_s_auto have identical SHA256")
    if "p0_r_collect" in parsed:
        checks.append(only_diff("p0_r", "p0_r_collect", "data_collection/level", "-1", "0"))
    return {"all_valid": True, "checks": checks}


def run_command(cmd: list[str], cwd: Path, logfile: Path, timeout: int | float,
                env: dict, cpus: list[int] | None):
    """High-resolution process wall time: blocking waitpid + independent watchdog.

    subprocess.run(timeout=...) and Popen.wait(timeout=...) both use busy polling
    on POSIX; the backoff can produce ~50ms time quantization. This version
    blocks inside Popen.wait() WITHOUT timeout. A separate watchdog thread
    kills the entire spawned process group if the deadline is reached.
    Stopwatch starts just before spawn, stops immediately after wait().
    """
    logfile.parent.mkdir(parents=True, exist_ok=True)
    preexec = (lambda: os.sched_setaffinity(0, set(cpus))) if cpus is not None else None
    timed_out = threading.Event()
    with logfile.open("w", encoding="utf-8", errors="replace") as f:
        f.write("# command: " + " ".join(cmd) + "\n# cwd: " + str(cwd) + "\n")
        f.flush()
        start_ns = time.perf_counter_ns()
        process = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT,
            env=env, preexec_fn=preexec, start_new_session=True,
        )

        def stop_after_deadline():
            if process.poll() is not None:
                return
            timed_out.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        watchdog = threading.Timer(float(timeout), stop_after_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            return_code = process.wait()  # POSIX blocking waitpid; no 50ms polling
            elapsed_ns = time.perf_counter_ns() - start_ns
        finally:
            watchdog.cancel()
            watchdog.join()
        if timed_out.is_set():
            f.write(f"\n[E05] TIMEOUT after {timeout}s; process group killed\n")
            return_code = 124
        return return_code, elapsed_ns / 1_000_000_000, timed_out.is_set()

def extract(output: str):
    output = ANSI.sub("", output)
    def field(label):
        matches = re.findall(r"^\s*" + re.escape(label) + r"\s*:\s*(.*?)\s*$", output, re.M)
        return matches[-1] if matches else None
    obj = field("Obj")
    alg_time = field("PWDefault Time")
    return {
        "status": field("Solution Status"),
        "objective": float(obj) if obj and re.fullmatch(NUMBER, obj) else None,
        "solver_s": float(alg_time) if alg_time and re.fullmatch(NUMBER, alg_time) else None,
        "tour": field("Tour"), "consumption": field("Consumption"),
    }

def get_stats_csv(run_cwd: Path):
    candidates = list((run_cwd / "output").rglob("*.csv"))
    # Look for PWDefault, not LM, and prioritize exact basename.
    alg = [p for p in candidates if p.stem == "PWDefault"]
    if not alg: alg = [p for p in candidates if "PWDefault" in p.name and not p.stem.endswith("_LM") and "_sol" not in p.stem]
    if not alg: return None, []
    p = max(alg, key=lambda x: x.stat().st_mtime)
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    return p, rows

def parse_positive_int_list(arg: str, allowed: set[int]):
    vals = [int(x.strip()) for x in arg.split(",") if x.strip()]
    if len(vals) < 2 or len(set(vals)) != len(vals) or not set(vals).issubset(allowed):
        raise ValueError(f"--cpus must name >=2 distinct available CPU IDs (allowed={sorted(allowed)[:48]})")
    return vals

def main():
    pa=argparse.ArgumentParser(description="Offline PathWyse E0.5 factorial benchmark: source code never modified")
    pa.add_argument("--repo",type=Path,default=Path.home()/"code/dass/pathwyse")
    pa.add_argument("--out",type=Path,default=Path.home()/"code/dass/e05_results")
    pa.add_argument("--instance",action="append",default=[],help="Absolute or relative-to-PathWyse-root; repeat to add")
    pa.add_argument("--repeats",type=int,default=10)
    pa.add_argument("--warmups",type=int,default=1)
    pa.add_argument("--timeout",type=int,default=300)
    pa.add_argument("--build-timeout",type=int,default=600)
    pa.add_argument("--jobs",type=int,default=8)
    pa.add_argument("--seed",type=int,default=20261115)
    pa.add_argument("--cpus",type=str,default="",help="OPTIONAL: 2+ physical core IDs, e.g. 2,4; same affinity for all configs")
    pa.add_argument("--skip-build",action="store_true")
    pa.add_argument("--no-extras",action="store_true",help="Only four factorial cells, no controls or stock anchor")
    pa.add_argument("--profiles",default="all",help="Comma-separated profiles or 'all'; e.g. p1_s,p1_s_auto for auto-only rerun")
    pa.add_argument("--validate-only",action="store_true",help="Write configs and validate ablations, then exit before build/benchmark")
    pa.add_argument("--no-diagnostics",action="store_true",help="Skip one per-cell CSV-producing diagnostic run")
    pa.add_argument("--force",action="store_true",help="Allow replacing existing E0.5 measurements in output directory")
    args=pa.parse_args()
    if min(args.repeats,args.timeout,args.jobs,args.build_timeout) < 1 or args.warmups < 0:
        pa.error("repeats/timeout/jobs/build-timeout must be positive and warmups nonnegative")
    root=args.repo.expanduser().resolve(); out=args.out.expanduser().resolve()
    if not (root/"CMakeLists.txt").is_file() and (root/"pathwyse-main"/"CMakeLists.txt").is_file():
        root=root/"pathwyse-main"
    for p in [root/"pathwyse.set",root/"CMakeLists.txt",root/"src"/"CMakeLists.txt"]:
        if not p.is_file(): pa.error(f"PathWyse source file not found: {p}")
    if (out/"measurements.csv").exists() and not args.force and not args.validate_only:
        pa.error("Output already contains measurements.csv; use a new --out directory or explicit --force")
    out.mkdir(parents=True,exist_ok=True)
    allowed=set(os.sched_getaffinity(0)) if hasattr(os,"sched_getaffinity") else set(range(os.cpu_count() or 1))
    try: cpus=parse_positive_int_list(args.cpus,allowed) if args.cpus else None
    except ValueError as e:pa.error(str(e))
    instances=[(Path(p) if Path(p).is_absolute() else root/p).resolve() for p in args.instance] if args.instance else [(root/DEFAULT_SPP).resolve()]
    for p in instances:
        if not p.is_file():pa.error(f"Instance not found: {p}")
    env=os.environ.copy();env.update({"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","LC_ALL":"C"})
    meta={"experiment":"VOC-LS/E0.5-fixed-v2","timing_method":"blocking_Popen_wait_with_thread_watchdog_perf_counter_ns","started_utc":now(),"repo":str(root),"git_commit_local_only":get_commit(root),"source_tree_sha256":source_hash(root),"source_settings_sha256":checksum(root/"pathwyse.set"),"cmake":version(["cmake","--version"]),"compiler":version(["g++","--version"]),"python":platform.python_version(),"platform":platform.platform(),"cpu_affinity":cpus,"allowed_cpus_count":len(allowed),"repeats":args.repeats,"warmups":args.warmups,"seed":args.seed,"requested_profiles":args.profiles,"timeout_s":args.timeout,"instances":{str(x):checksum(x) for x in instances},"completed":False}
    (out/"metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n")
    try:
        configs=make_configs(root,out,include_extras=not args.no_extras)
        validation=validate_config_invariants(configs)
        meta["config_validation"] = validation
        meta["configs_sha256"]={key:checksum(v) for key,v in configs.items()}
        if args.profiles == "all":
            profiles=list(configs)
        else:
            profiles=[p.strip() for p in args.profiles.split(",") if p.strip()]
            if not profiles or len(set(profiles)) != len(profiles) or any(p not in configs for p in profiles):
                raise ValueError(f"Invalid --profiles {args.profiles!r}; allowed={sorted(configs)}")
        meta["selected_profiles"] = profiles
        if args.validate_only:
            meta["validation_only"] = True
            meta["completed_utc"] = now()
            print("CONFIG VALIDATION PASSED:", ", ".join(profiles), flush=True)
            for item in validation["checks"]:
                print(f"  {item['first']} -> {item['second']}: ONLY {item['changed_only']} {item['values']}", flush=True)
            return 0
        if not args.skip_build:
            for phase,cmd,tm in [("cmake_configure",["cmake","-S",str(root),"-B",str(root/"build"),"-DCMAKE_BUILD_TYPE=Release"],args.build_timeout),("cmake_build",["cmake","--build",str(root/"build"),"-j",str(args.jobs)],args.build_timeout)]:
                code,_,timed=run_command(cmd,root,out/"logs"/(phase+".log"),tm,env,None)
                if code or timed:raise RuntimeError(f"{phase} failed (exit={code}); inspect {out/'logs'/(phase+'.log')}")
        exe=root/"bin"/"pathwyse"
        if not exe.is_file():raise RuntimeError(f"Binary missing: {exe}")
        meta["binary_sha256"]=checksum(exe)
        # The program only uses absolute instance & config paths. No stale pathwyse.set picked up.
        work=out/"execution_cwd";work.mkdir(parents=True,exist_ok=True)
        rnd=random.Random(args.seed)
        rows=[]
        def save_rows():
            with (out/"measurements.csv").open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
        def one(kind,inst,profile,round_number,order):
            path=out/"logs"/f"{inst.stem[:22]}__{kind}__round{round_number:03d}__{order:02d}__{profile}.log"
            cmd=[str(exe),str(inst),"-param",str(configs[profile])]
            rc,wall,timed=run_command(cmd,work,path,args.timeout,env,cpus)
            result=extract(path.read_text(encoding="utf-8",errors="replace"))
            row={"run_type":kind,"instance":str(inst.relative_to(root)) if inst.is_relative_to(root) else str(inst),"round":round_number,"order":order,"profile":profile,**result,"wall_s":round(wall,9),"wall_ns":round(wall*1_000_000_000),"exit_code":rc,"timeout":timed,"log":str(path.relative_to(out))}
            good=rc==0 and not timed and row["status"]=="Optimal" and row["objective"] is not None
            if inst.name=="toy.txt" and good: good=math.isclose(row["objective"],TOY_EXPECTED,rel_tol=0,abs_tol=1e-6)
            if not good: raise RuntimeError(f"Invalid/failed result ({profile}, {inst.name}): {row}; inspect {path}")
            if kind=="measure":
                rows.append(row);save_rows()
                print(f"round={round_number:02d} {profile:<14} {inst.stem[:25]:<25} wall={wall:.4f}s solver={result['solver_s']} obj={result['objective']}",flush=True)
            return row
        for inst in instances:
            # One off warmups per cell: keep them off the timing analysis.
            for w in range(args.warmups):
                order=profiles.copy();rnd.shuffle(order)
                for j,name in enumerate(order,1):one("warmup",inst,name,-w-1,j)
            for round_number in range(1,args.repeats+1):
                order=profiles.copy();rnd.shuffle(order)
                for j,name in enumerate(order,1):one("measure",inst,name,round_number,j)
        if not args.no_diagnostics:
            summaries=[]
            for inst in instances:
                for p in CORE:
                    if p not in profiles: continue
                    diag_cwd=out/"diagnostic_cwd"/(inst.stem+"_"+p)
                    diag_cwd.mkdir(parents=True,exist_ok=True)
                    logfile=out/"logs"/f"{inst.stem[:22]}__diagnostic__{p}.log"
                    cmd=[str(exe),str(inst),"-param",str(out/"configs"/(p+"_diagnostic.set")),"-tag",p]
                    rc,wall,timed=run_command(cmd,diag_cwd,logfile,args.timeout,env,cpus)
                    row=extract(logfile.read_text(encoding="utf-8",errors="replace"))
                    csv_path,records=get_stats_csv(diag_cwd)
                    if rc != 0 or timed or row["status"] != "Optimal":
                        raise RuntimeError(f"Diagnostic solver failed: {p}, {inst}, exit={rc}, status={row['status']}, log={logfile}")
                    if not csv_path or not records:
                        raise RuntimeError(f"Diagnostic PWDefault.csv not found or empty: {diag_cwd / 'output'}")
                    lm_candidates = list((diag_cwd / "output").rglob("PWDefault_LM.csv"))
                    lm_path = lm_candidates[0] if lm_candidates else None
                    lm_records=[]
                    if lm_path:
                        with lm_path.open(newline="",encoding="utf-8",errors="replace") as f: lm_records=list(csv.DictReader(f))
                    def numeric(x,k):
                        try:return float(x.get(k, "0") or "0")
                        except (ValueError,TypeError):return 0.0
                    total_extensions=sum(numeric(x,"it_ext_fw")+numeric(x,"it_ext_bw") for x in records)
                    summ={"instance":str(inst),"profile":p,"status":row["status"],"objective":row["objective"],
                          "wall_s_DONT_USE_AS_BENCHMARK":wall,"exit_code":rc,"timeout":timed,
                          "per_iteration_records":len(records),"iteration_csv":str(csv_path.relative_to(out)),
                          "total_extensions_across_iterations":int(total_extensions),
                          "last_iteration_extensions":int(numeric(records[-1],"it_ext_fw")+numeric(records[-1],"it_ext_bw")),
                          "lm_csv":str(lm_path.relative_to(out)) if lm_path else None,
                          "lm_records":lm_records,"iterations":records}
                    summaries.append(summ)
                    print(f"diagnostic {p} {inst.stem}: iterations={len(records)} total_ext={int(total_extensions)} (NOT benchmark timing)",flush=True)
            (out/"diagnostics.json").write_text(json.dumps(summaries,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        # Hard checks: compare every profile objective against all peers for each instance.
        check={}
        for inst in instances:
            name=str(inst.relative_to(root)) if inst.is_relative_to(root) else str(inst)
            vals=[r["objective"] for r in rows if r["instance"]==name]
            ok=bool(vals) and all(math.isclose(v,vals[0],abs_tol=1e-6,rel_tol=0) for v in vals)
            check[name] = {"all_objectives_equal":ok,"objective":vals[0] if vals else None}
        passed=all(x["all_objectives_equal"] for x in check.values())
        meta.update({"completed":True,"completed_utc":now(),"all_passed":passed,"objective_checks":check,"measurement_count":len(rows),"timer_precision_note":"wall_s uses monotonic perf_counter_ns and blocking wait; OS scheduling noise remains"})
        print(f"\nE0.5 {'PASS' if passed else 'FAIL'}: {len(rows)} measurement rows -> {out/'measurements.csv'}")
        return 0 if passed else 1
    except Exception as e:
        meta.update({"error":str(e),"completed_utc":now()})
        print(f"E0.5 blocked/failed: {e}",file=sys.stderr)
        return 2
    finally:
        (out/"metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

if __name__=="__main__":sys.exit(main())
