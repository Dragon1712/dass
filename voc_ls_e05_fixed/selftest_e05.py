#!/usr/bin/env python3
"""Local tests using tiny fake solver/subprocesses; NO PathWyse performance claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from e05_run import make_configs, validate_config_invariants, parse_config, run_command
from e05_analyze import evaluate


def main():
    with tempfile.TemporaryDirectory(prefix="voc_e05_test_") as temp:
        t = Path(temp)
        root = t / "repo"
        (root / "src").mkdir(parents=True)
        (root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
        (root / "src" / "CMakeLists.txt").write_text("project(fake)\n")
        (root / "src" / "stub.cpp").write_text("int dummy=0;\n")
        (root / "pathwyse.set").write_text("verbosity = 1\nalgo/default/autoconfig = 0\n")
        configs = make_configs(root, t / "result", include_extras=True)
        r = validate_config_invariants(configs)
        assert r["all_valid"]
        assert parse_config(configs["p1_s"])["algo/default/autoconfig"] == "0"
        assert parse_config(configs["p1_s_auto"])["algo/default/autoconfig"] == "1"
        assert configs["p1_s"].read_bytes() != configs["p1_s_auto"].read_bytes()

        # A deliberately broken ablation MUST fail before any benchmark runs.
        configs["p1_s_auto"].write_bytes(configs["p1_s"].read_bytes())
        try:
            validate_config_invariants(configs)
        except ValueError as e:
            assert "INVALID ABLATION" in str(e) or "AUTO ABLATION" in str(e)
        else:
            raise AssertionError("Identical auto configs were incorrectly accepted")

        log = t / "ok.log"
        rc, elapsed, timeout = run_command(
            [sys.executable, "-c", "import time; time.sleep(0.02); print('ok')"],
            t, log, timeout=10, env=dict(os.environ), cpus=None,
        )
        assert rc == 0 and not timeout and elapsed > 0.020, (rc, elapsed, timeout)
        assert "ok" in log.read_text()
        print(f"PASS: normal blocking wait, measured wall={elapsed:.6f}s")

        rc, elapsed, timeout = run_command(
            [sys.executable, "-c", "import time; time.sleep(4)"],
            t, t / "timeout.log", timeout=0.2, env=dict(os.environ), cpus=None,
        )
        assert rc == 124 and timeout and elapsed < 4, (rc, elapsed, timeout)
        assert "TIMEOUT" in (t / "timeout.log").read_text()
        print(f"PASS: watchdog cancels timed out process, measured wall={elapsed:.6f}s")
    print("ALL SELFTESTS PASSED: config guard + blocking wait + timeout")


if __name__ == "__main__":
    main()
