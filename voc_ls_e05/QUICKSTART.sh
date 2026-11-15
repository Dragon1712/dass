#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="${ROOT:-$HOME/code/dass/pathwyse}"
OUT="${OUT:-$HOME/code/dass/e05_results}"
python3 -u e05_run.py --repo "$ROOT" --out "$OUT" --repeats "${REPEATS:-10}" --warmups "${WARMUPS:-2}" --timeout "${TIMEOUT:-300}" --jobs "${JOBS:-8}"
python3 e05_analyze.py --out "$OUT"
