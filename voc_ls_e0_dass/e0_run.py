#!/usr/bin/env python3
"""VOC-LS E0: reproducible PathWyse exact-baseline runner (stdlib-only).

The script deliberately does not modify PathWyse algorithmic source files.
It supports official source cloning or reuse of a local checkout, and records
complete wall-clock costs, solver outputs, configuration hashes and correctness.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO_URL = "https://github.com/Dragon1712/dass.git"
EXPECTED_COMMIT = "d5a786dbbe2351d65564bf1245b54c72edfe2164"
NESTED_PROJECT = "pathwyse-main"
EXPECTED_TOY = -10435.0
SUMMARY_FIELDS = ["profile", "instance", "repeat", "status", "objective", "tour", "consumption", "wall_time_s", "exit_code", "optimal", "toy_reference_ok", "timed_out", "log_path"]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def which_version(tool: str, *args: str) -> str:
    try:
        p = subprocess.run([tool, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        return p.stdout.strip().splitlines()[0]
    except Exception as exc:
        return f"UNAVAILABLE: {exc}"


def run_logged(cmd: list[str], cwd: Path, log_path: Path, timeout: int, env=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8", errors="replace") as f:
        f.write("# command: " + " ".join(cmd) + "\n# cwd: " + str(cwd) + "\n")
        f.flush()
        try:
            p = subprocess.run(cmd, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT, timeout=timeout, env=env)
            return p.returncode, time.perf_counter() - start, False
        except subprocess.TimeoutExpired:
            f.write(f"\n[E0_RUNNER] TIMEOUT after {timeout} seconds\n")
            return 124, time.perf_counter() - start, True


def git_sha(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def is_project(path: Path) -> bool:
    return (path / "CMakeLists.txt").is_file() and (path / "pathwyse.set").is_file() and (path / "src" / "CMakeLists.txt").is_file()


def prepare_repo(checkout: Path, out: Path, clone_timeout: int):
    """Accept either an existing checkout root or the nested PathWyse project path.

    Return the verified PathWyse project root. Clone only if neither form exists.
    """
    if is_project(checkout):
        return checkout
    if is_project(checkout / NESTED_PROJECT):
        return checkout / NESTED_PROJECT
    if checkout.exists() and any(checkout.iterdir()):
        raise RuntimeError(f"Path exists, but neither it nor {NESTED_PROJECT}/ contains PathWyse: {checkout}")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    code, _, timed_out = run_logged(["git", "clone", "--depth", "1", REPO_URL, str(checkout)], checkout.parent, out / "logs" / "clone.log", clone_timeout)
    if code or timed_out:
        raise RuntimeError("Git clone failed; inspect logs/clone.log. If the repo is already downloaded, pass --repo /path/to/dass or --repo /path/to/dass/pathwyse-main.")
    project = checkout / NESTED_PROJECT
    if not is_project(project):
        raise RuntimeError(f"Cloned {REPO_URL}, but cannot find {NESTED_PROJECT}/CMakeLists.txt and settings")
    return project


def build_repo(repo: Path, out: Path, jobs: int, timeout: int):
    logs = out / "logs"
    code, _, _ = run_logged(["cmake", "-S", str(repo), "-B", str(repo / "build"), "-DCMAKE_BUILD_TYPE=Release"], repo, logs / "cmake_configure.log", timeout)
    if code:
        raise RuntimeError(f"cmake configure failed (exit {code}); see logs/cmake_configure.log")
    code, _, _ = run_logged(["cmake", "--build", str(repo / "build"), "-j", str(jobs)], repo, logs / "cmake_build.log", timeout)
    if code:
        raise RuntimeError(f"cmake build failed (exit {code}); see logs/cmake_build.log")
    exe = repo / "bin" / "pathwyse"
    if not exe.is_file():
        raise RuntimeError("build finished, but expected bin/pathwyse is missing")


def build_controlled_config(src: Path, dest: Path, diagnostic: bool = False):
    edits = {
        "verbosity": "3" if diagnostic else "1", # diagnostics show counters; timed runs minimize stdout
        "algo_names": "PWDefault",
        "algo_active": "1",
        "algo_parallel": "0",
        "algo/default/autoconfig": "0",  # avoid silent candidate-policy overrides
        "algo/default/parallel": "0",    # single-threaded bidirectional search
        "algo/default/search": "bidirectional",
        "algo/default/candidate/type": "node",
        "algo/default/dssr": "restricted", # DSSR-R exact only if solved to completion
        "algo/default/ng": "off",
        "algo/default/timelimit": "0",    # no solver-internal early termination
        "algo/requested_solutions": "1",
        "data_collection/level": "0",     # 0 = lightweight performance counters; -1 = off
        "output/write": "0",
    }
    lines = src.read_text(encoding="utf-8").splitlines()
    matched = set()
    result = []
    for line in lines:
        m = re.match(r"^(\s*)([^#\s][^=#]*?)\s*=", line)
        key = m.group(2).strip() if m else None
        if key in edits:
            result.append(f"{key} = {edits[key]}")
            matched.add(key)
        else:
            result.append(line)
    result.append("\n# E0 controlled-run overrides; copied without changing the solver source")
    for key in edits.keys() - matched:
        result.append(f"{key} = {edits[key]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(result) + "\n", encoding="utf-8")


def parse_solution(text: str):
    text = ANSI_RE.sub("", text)
    def match_str(k):
        m = re.findall(r"^\s*" + re.escape(k) + r"\s*:\s*(.*?)\s*$", text, re.M | re.I)
        return m[-1].strip() if m else None
    status = match_str("Solution Status")
    value = match_str("Obj")
    m = re.match(NUMBER, value) if value else None
    objective = float(m.group(0)) if m else None
    return {"status": status, "objective": objective, "tour": match_str("Tour"), "consumption": match_str("Consumption")}


def parse_counters(text: str):
    text = ANSI_RE.sub("", text)
    # Tags reflect the official DataCollector API, but may differ across versions.
    keys = ["Global_time", "iterations", "it_ext_fw", "it_ext_bw", "t_labeling", "t_join", "t_ext_fw", "t_ext_bw", "t_ins_fw", "t_ins_bw", "timeout", "relaxation", "ndominated", "ndominated_fw", "ndominated_bw", "split"]
    result = {}
    for key in keys:
        # choose last occurrence (the final algorithm summary)
        values = re.findall(r"^\s*" + re.escape(key) + r"\s*:\s*([^\r\n]+)", text, re.M)
        if values:
            value = values[-1].strip()
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
    return result


def discover_instances(repo: Path, selected: list[str], all_spprclib: bool, max_instances: int):
    if selected:
        found = [Path(p).resolve() for p in selected]
    else:
        found = [repo / "instances" / "toy.txt"]
        sp = sorted((repo / "instances" / "spprclib").glob("*.sppcc"))
        found.extend(sp if all_spprclib else sp[:1])
    missing = [str(p) for p in found if not p.is_file()]
    if missing:
        raise RuntimeError("Missing instance files: " + ", ".join(missing))
    return found[:max_instances]


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def aggregate(rows: list[dict]):
    result = []
    for key in sorted(set((x["profile"], x["instance"]) for x in rows)):
        subset = [x for x in rows if (x["profile"], x["instance"]) == key]
        times = [x["wall_time_s"] for x in subset if x["optimal"] and x["exit_code"] == 0 and x["toy_reference_ok"] is not False]
        objectives = [x["objective"] for x in subset if x["objective"] is not None]
        unique_obj = not objectives or all(math.isclose(x, objectives[0], rel_tol=0, abs_tol=1e-6) for x in objectives)
        result.append({"profile": key[0], "instance": key[1], "successful_runs": len(times), "total_runs": len(subset), "median_wall_s": statistics.median(times) if times else None, "mean_wall_s": statistics.mean(times) if times else None, "min_wall_s": min(times) if times else None, "max_wall_s": max(times) if times else None, "objectives_consistent": unique_obj, "objective": objectives[0] if objectives else None})
    by_instance = {}
    for x in rows:
        if x["objective"] is not None and x["optimal"] and x["toy_reference_ok"] is not False:
            by_instance.setdefault(x["instance"], []).append(x["objective"])
    cross_consistent = all(all(math.isclose(z, v[0], abs_tol=1e-6, rel_tol=0) for z in v) for v in by_instance.values())
    return result, cross_consistent


def main():
    ap = argparse.ArgumentParser(description="Run VOC-LS E0 baseline without altering PathWyse algorithm code")
    ap.add_argument("--repo", type=Path, default=Path("dass"), help="dass Git checkout root OR dass/pathwyse-main (auto-clones when absent)")
    ap.add_argument("--expected-commit", default=EXPECTED_COMMIT, help="Verify source commit SHA, empty string disables pin check")
    ap.add_argument("--out", type=Path, default=Path("results/e0"), help="Output directory")
    ap.add_argument("--profiles", choices=["stock", "controlled", "both"], default="both")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--instance", action="append", default=[], help="Explicit instance file; repeat flag for more")
    ap.add_argument("--all-spprclib", action="store_true", help="Include all available .sppcc instances")
    ap.add_argument("--max-instances", type=int, default=1000)
    ap.add_argument("--timeout", type=int, default=120, help="Seconds per solver run")
    ap.add_argument("--clone-timeout", type=int, default=180)
    ap.add_argument("--build-timeout", type=int, default=600)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--no-diagnostic", action="store_true", help="Disable one verbose statistics run per instance")
    args = ap.parse_args()
    if args.repeats < 1 or args.timeout < 1 or args.max_instances < 1:
        ap.error("repeats, timeout, and max-instances must be positive")
    checkout, out = args.repo.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    meta = {"experiment": "VOC-LS/E0", "started_utc": utc_now(), "repository_url": REPO_URL, "project_subdir": NESTED_PROJECT,
            "source_checkout_requested": str(checkout), "out": str(out), "expected_commit": args.expected_commit, "platform": platform.platform(),
            "python": sys.version, "cmake": which_version("cmake", "--version"),
            "compiler": which_version("g++", "--version"), "git": which_version("git", "--version"),
            "profiles": args.profiles, "repeats": args.repeats, "stock_is_correctness_only": True, "timeout_s": args.timeout,
            "run_completed": False}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        repo = prepare_repo(checkout, out, args.clone_timeout)
        meta["project_root"] = str(repo)
        meta["git_commit"] = git_sha(repo)
        if args.expected_commit and meta["git_commit"] != args.expected_commit:
            raise RuntimeError(f"Source commit changed: expected {args.expected_commit}, got {meta['git_commit']}. Use --expected-commit '' to deliberately accept a newer revision (and record it).")
        # Cross-check the official five-node toy independently of the solver.
        toy_path = repo / "instances" / "toy.txt"
        if toy_path.exists():
            from e0_toy_oracle import solve_toy
            meta["toy_independent_oracle"] = solve_toy(toy_path)
            if not meta["toy_independent_oracle"]["verified"]:
                raise RuntimeError("Independent toy verifier disagrees with the official -10435 reference")
        if not args.skip_build:
            build_repo(repo, out, args.jobs, args.build_timeout)
        elif not (repo / "bin" / "pathwyse").is_file():
            raise RuntimeError("--skip-build was given but bin/pathwyse does not exist")
        config_dir = out / "configs"
        config_dir.mkdir(exist_ok=True)
        shutil.copy2(repo / "pathwyse.set", config_dir / "upstream.set")
        build_controlled_config(repo / "pathwyse.set", config_dir / "controlled.set")
        build_controlled_config(repo / "pathwyse.set", config_dir / "controlled_diagnostic.set", diagnostic=True)
        meta["config_sha256"] = {"stock": sha256(config_dir / "upstream.set"), "controlled": sha256(config_dir / "controlled.set"), "controlled_diagnostic": sha256(config_dir / "controlled_diagnostic.set")}
        instances = discover_instances(repo, args.instance, args.all_spprclib, args.max_instances)
        meta["instances"] = [{"path": str(p), "sha256": sha256(p)} for p in instances]
        selected = ["stock", "controlled"] if args.profiles == "both" else [args.profiles]
        rows = []
        details = []
        env = dict(os.environ)
        env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "LC_ALL": "C"})
        if not args.no_diagnostic and "controlled" in selected:
            selected.append("controlled_diagnostic")
        for profile in selected:
            param = config_dir / ("upstream.set" if profile == "stock" else "controlled_diagnostic.set" if profile == "controlled_diagnostic" else "controlled.set")
            for instance in instances:
                for repeat in range(1, (1 if profile == "controlled_diagnostic" else args.repeats) + 1):
                    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", instance.stem) + "_" + sha256(instance)[:8]
                    logpath = out / "logs" / f"{profile}__{slug}__r{repeat:02d}.log"
                    cmd = [str(repo / "bin" / "pathwyse"), str(instance), "-param", str(param)]
                    exit_code, wall, timed_out = run_logged(cmd, repo, logpath, args.timeout, env=env)
                    output = logpath.read_text(encoding="utf-8", errors="replace")
                    sol = parse_solution(output)
                    toy = instance.name == "toy.txt"
                    toy_ok = math.isclose(sol["objective"], EXPECTED_TOY, abs_tol=1e-6, rel_tol=0) if toy and sol["objective"] is not None else (False if toy else None)
                    row = {"profile": profile, "instance": str(instance.relative_to(repo)) if instance.is_relative_to(repo) else str(instance), "repeat": repeat,
                           "status": sol["status"], "objective": sol["objective"], "tour": sol["tour"], "consumption": sol["consumption"],
                           "wall_time_s": round(wall, 6), "exit_code": exit_code, "optimal": sol["status"] == "Optimal", "toy_reference_ok": toy_ok,
                           "timed_out": timed_out, "log_path": str(logpath.relative_to(out))}
                    rows.append(row)
                    details.append({**row, "counters": parse_counters(output)})
                    print(f"[{profile}] {instance.name} repeat={repeat} status={sol['status']} obj={sol['objective']} wall={wall:.4f}s exit={exit_code} toy_check={toy_ok}", flush=True)
                    write_csv(out / "runs.csv", rows)
        aggr, consistent = aggregate(rows)
        pass_all = bool(rows) and all(x["exit_code"] == 0 and x["optimal"] and x["objective"] is not None and x["toy_reference_ok"] is not False and not x["timed_out"] for x in rows) and consistent
        meta.update({"completed_utc": utc_now(), "run_completed": True, "all_checks_passed": pass_all,
                     "objectives_consistent_across_profiles": consistent, "aggregates": aggr})
        (out / "details.json").write_text(json.dumps(details, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nE0 result: {'PASS' if pass_all else 'FAIL'}; CSV: {out / 'runs.csv'}")
        return 0 if pass_all else 1
    except Exception as exc:
        meta["error"] = str(exc)
        meta["completed_utc"] = utc_now()
        print(f"E0 blocked/failed: {exc}", file=sys.stderr)
        return 2
    finally:
        (out / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
