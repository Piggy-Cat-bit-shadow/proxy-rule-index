#!/usr/bin/env bash
# Build entrypoint: install deps, run index, run validation.
# Usage: ./build.sh [--skip-deps]
set -euo pipefail

cd "$(dirname "$0")"

if [[ "${1:-}" != "--skip-deps" ]]; then
  python3 -m pip install --quiet -r requirements.txt
fi

echo "== building index =="
python3 -u scripts/index.py

echo
echo "== validating =="
python3 scripts/validate.py
