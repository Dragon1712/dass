#!/usr/bin/env bash
set -euo pipefail
# E0 from Dragon1712/dass. Can be run from any working directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${E0_WORK_DIR:-$PWD/voc_ls_e0_work}"
mkdir -p "$WORK"
python3 "$HERE/e0_run.py" \
  --repo "$WORK/dass" --out "$WORK/results/e0" \
  --profiles both --repeats "${E0_REPEATS:-3}" \
  --timeout "${E0_TIMEOUT:-120}" --jobs "${E0_JOBS:-8}" "$@"
