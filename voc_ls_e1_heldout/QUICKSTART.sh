#!/usr/bin/env bash
set -euo pipefail
# Quick start: prepare data split only; deliberately does not auto-run expensive experiments.
cd "$(dirname "$0")"
python3 selftest_heldout.py
python3 e1_heldout.py prepare --work "${WORK:-$HOME/code/dass/e1_heldout_v1}"
echo "Frozen dataset. Read README.md to collect/train/eval. No tests were used or run."
