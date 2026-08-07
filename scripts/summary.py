#!/usr/bin/env python3
"""Print a markdown summary of build metrics for GitHub Actions step summary."""
import json
import os
import sys

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "build-metrics.json")
if not os.path.exists(path):
    print("no metrics file")
    sys.exit(0)

with open(path, encoding="utf-8") as f:
    m = json.load(f)

print("| Source | Changed | Reused | Refreshed | Removed | Failed | Time |")
print("|--------|---------|--------|-----------|---------|--------|------|")
for sid, x in sorted(m.items()):
    print(f"| {sid} | {x.get('changed')} | {x.get('reused')} | {x.get('refreshed')} | {x.get('removed')} | {x.get('failed')} | {x.get('seconds')}s |")
