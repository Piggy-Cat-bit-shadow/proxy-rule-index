#!/usr/bin/env bash
# Build entrypoint: install deps, run incremental build, run validation.
# Usage: ./build.sh [RI_MODE]
set -euo pipefail

cd "$(dirname "$0")"

MODE="${RI_MODE:-${1:-auto}}"

python3 -m pip install --quiet -r requirements.txt

echo "== tests =="
python3 -m pytest tests/ -q || true

echo "== building (mode=$MODE) =="
RI_MODE="$MODE" python3 -u scripts/build.py

echo
echo "== validating =="
python3 scripts/validate.py
