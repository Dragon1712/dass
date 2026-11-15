#!/usr/bin/env bash
set -euo pipefail
ROOT="${ROOT:-$HOME/code/dass/pathwyse}"
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/selftest_e1.py"
python3 "$HERE/e1_apply.py" --repo "$ROOT" --check
printf '\nSources compatible. To modify local PathWyse and build:\n'
printf 'python3 "%s/e1_apply.py" --repo "%s" --apply\n' "$HERE" "$ROOT"
printf 'cmake -S "%s" -B "%s/build" -DCMAKE_BUILD_TYPE=Release && cmake --build "%s/build" -j 8\n' "$ROOT" "$ROOT" "$ROOT"
