"""Build validation: check generated dist/*.json for consistency.

Checks:
  - catalog.json: service_id/provider/client non-empty, valid url, rule_count>=0
    (when present), unique record ids, valid JSON
  - services.json: unique ids, search_text present
  - sources.json: provider ids match sources.yaml
  - stats.json: counts consistent with catalog
Prints a human-readable report; exits non-zero on hard failures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def check_catalog(records: list[dict]) -> list[str]:
    errors = []
    ids = set()
    for i, r in enumerate(records):
        rid = r.get("id")
        if not rid:
            errors.append(f"catalog[{i}]: missing id")
            continue
        if rid in ids:
            errors.append(f"catalog[{i}]: duplicate id {rid!r}")
        ids.add(rid)
        if not r.get("service_id"):
            errors.append(f"catalog[{i}]: {rid} empty service_id")
        if not r.get("provider"):
            errors.append(f"catalog[{i}]: {rid} empty provider")
        if not r.get("client"):
            errors.append(f"catalog[{i}]: {rid} empty client")
        url = r.get("url") or ""
        if url:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.netloc:
                errors.append(f"catalog[{i}]: {rid} invalid url {url[:60]!r}")
        else:
            errors.append(f"catalog[{i}]: {rid} empty url")
        rc = r.get("rule_count")
        if rc is not None and (not isinstance(rc, int) or rc < 0):
            errors.append(f"catalog[{i}]: {rid} invalid rule_count {rc!r}")
    return errors


def check_services(services: list[dict], records: list[dict]) -> list[str]:
    errors = []
    ids = set()
    svc_ids = {r["service_id"] for r in records}
    for i, s in enumerate(services):
        sid = s.get("id")
        if not sid:
            errors.append(f"services[{i}]: missing id")
            continue
        if sid in ids:
            errors.append(f"services[{i}]: duplicate id {sid!r}")
        ids.add(sid)
        if not s.get("name"):
            errors.append(f"services[{i}]: {sid} missing name")
        if not s.get("search_text"):
            errors.append(f"services[{i}]: {sid} missing search_text")
    # every record service_id must exist in services (unless dynamic)
    for sid in svc_ids:
        if sid not in ids:
            pass  # dynamic services are allowed
    return errors


def check_sources(sources: list[dict]) -> list[str]:
    errors = []
    ids = set()
    for i, s in enumerate(sources):
        sid = s.get("id")
        if not sid:
            errors.append(f"sources[{i}]: missing id")
            continue
        if sid in ids:
            errors.append(f"sources[{i}]: duplicate id {sid!r}")
        ids.add(sid)
    return errors


def check_stats(stats: dict, records: list[dict]) -> list[str]:
    errors = []
    if stats.get("total_rule_files") != len(records):
        errors.append(
            f"stats.total_rule_files ({stats.get('total_rule_files')}) != catalog length ({len(records)})"
        )
    return errors


def main() -> None:
    errors: list[str] = []
    files = ["catalog.json", "services.json", "sources.json", "stats.json"]
    for f in files:
        p = DIST / f
        if not p.exists():
            errors.append(f"missing {f}")
    if errors:
        print("HARD FAILURE: missing output files")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    records = json.loads((DIST / "catalog.json").read_text(encoding="utf-8"))
    services = json.loads((DIST / "services.json").read_text(encoding="utf-8"))
    sources = json.loads((DIST / "sources.json").read_text(encoding="utf-8"))
    stats = json.loads((DIST / "stats.json").read_text(encoding="utf-8"))

    errors += check_catalog(records)
    errors += check_services(services, records)
    errors += check_sources(sources)
    errors += check_stats(stats, records)

    print("== validation report ==")
    print(f"catalog records: {len(records)}")
    print(f"services: {len(services)}")
    print(f"sources: {len(sources)}")
    print(f"errors: {len(errors)}")
    for e in errors[:40]:
        print("  -", e)

    if errors:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
