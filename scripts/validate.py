"""Build validation: check generated dist/data/*.json for consistency.

Checks:
  - all JSON parse
  - manifest references exist
  - every service_id in catalog resolves (or is a known dynamic)
  - every provider in catalog exists in sources.json
  - every client is a known client id
  - canonical keys unique across catalog
  - alias conflicts (one alias -> one service)
  - rule_count >= 0 when present
  - schemaVersion correct
  - frontend core static files present
Hard failures exit non-zero (no publish).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DATA = DIST / "data"

SCHEMA_VERSION = 2
KNOWN_CLIENTS = {"surge", "loon", "shadowrocket", "egern", "mihomo", "sing-box", "stash", "surfboard", "quantumultx", "adguard"}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_catalog_records() -> list[str]:
    errors = []
    manifest = load_json(DATA / "manifest.json")
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"manifest schemaVersion mismatch: {manifest.get('schemaVersion')}")
    services = load_json(DATA / "services.json")
    svc_ids = {s["id"] for s in services}
    svc_ids |= _mapped_service_ids()
    sources = load_json(DATA / "sources.json")
    provider_ids = {s["id"] for s in sources}

    seen_keys: dict[str, list] = defaultdict(list)
    for client, fname in manifest.get("clients", {}).items():
        p = DATA / fname.removeprefix("data/")
        if not p.exists():
            errors.append(f"manifest catalog missing: {fname}")
            continue
        records = load_json(p)
        for r in records:
            key = r.get("k")
            if key:
                seen_keys[key].append((client, r.get("service_id")))
            if not r.get("service_id"):
                errors.append(f"{client}: record missing service_id")
            sid = r.get("service_id")
            if sid and sid not in svc_ids and not _is_dynamic_service(sid, provider_ids):
                errors.append(f"{client}: unknown service_id {sid!r} ({key})")
            if not r.get("provider"):
                errors.append(f"{client}: record missing provider")
            elif r["provider"] not in provider_ids:
                errors.append(f"{client}: unknown provider {r['provider']!r}")
            if not r.get("client"):
                errors.append(f"{client}: record missing client")
            if r.get("client") and r["client"] not in KNOWN_CLIENTS:
                errors.append(f"{client}: unknown client {r['client']!r}")
            rc = r.get("rule_count")
            if rc is not None and (not isinstance(rc, int) or rc < 0):
                errors.append(f"{client}: invalid rule_count {rc!r}")
    # duplicate canonical keys across shards
    dupes = {k: v for k, v in seen_keys.items() if len(v) > 1}
    for k, locs in dupes.items():
        errors.append(f"duplicate canonical key {k!r} in {locs}")
    return errors


def _is_dynamic_service(sid: str, provider_ids: set[str]) -> bool:
    """Dynamic services are named '<provider>-<stem>'; allow them."""
    for p in provider_ids:
        if sid.startswith(p + "-"):
            return True
    return False


def _mapped_service_ids() -> set[str]:
    """Service ids referenced by provider_file_mapping (they are valid targets)."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import yaml

    aliases = yaml.safe_load((ROOT / "data" / "aliases.yaml").read_text(encoding="utf-8"))
    out = set()
    for mapping in (aliases.get("provider_file_mapping") or {}).values():
        out.update(mapping.values())
    return out


def check_services() -> list[str]:
    errors = []
    services = load_json(DATA / "services.json")
    ids = set()
    alias_to_svc: dict[str, str] = {}
    for s in services:
        if s["id"] in ids:
            errors.append(f"duplicate service id {s['id']!r}")
        ids.add(s["id"])
        if not s.get("name"):
            errors.append(f"service {s['id']} missing name")
        if not s.get("search_text"):
            errors.append(f"service {s['id']} missing search_text")
        for a in s.get("aliases", []):
            k = a.strip().lower()
            if not k:
                continue
            if k in alias_to_svc and alias_to_svc[k] != s["id"]:
                errors.append(f"alias conflict: {a!r} -> {alias_to_svc[k]} and {s['id']}")
            else:
                alias_to_svc[k] = s["id"]
    return errors


def check_sources() -> list[str]:
    errors = []
    sources = load_json(DATA / "sources.json")
    ids = set()
    for s in sources:
        if s["id"] in ids:
            errors.append(f"duplicate source id {s['id']!r}")
        ids.add(s["id"])
        if not s.get("repo"):
            errors.append(f"source {s['id']} missing repo")
    return errors


def check_health() -> list[str]:
    errors = []
    health = load_json(DATA / "health.json")
    if health.get("schemaVersion") != SCHEMA_VERSION:
        errors.append("health.json schemaVersion mismatch")
    # keys in health must be a subset of catalog keys
    manifest = load_json(DATA / "manifest.json")
    catalog_keys = set()
    for fname in manifest.get("clients", {}).values():
        p = DATA / fname.removeprefix("data/")
        if p.exists():
            catalog_keys.update(r["k"] for r in load_json(p))
    for h in health.get("records", []):
        if h.get("k") not in catalog_keys:
            errors.append(f"health record {h.get('k')} not in catalog")
    return errors


def check_core_files() -> list[str]:
    errors = []
    for f in ["index.html", "data/manifest.json", "data/services.json", "data/sources.json", "data/stats.json", "data/build-info.json", "data/health.json"]:
        if not (DIST / f).exists():
            errors.append(f"missing core file: {f}")
    if (DIST / "index.html").exists():
        html = (DIST / "index.html").read_text(encoding="utf-8")
        if "data/manifest.json" not in html:
            errors.append("index.html does not reference data/manifest.json")
    return errors


def main() -> None:
    errors: list[str] = []
    errors += check_catalog_records()
    errors += check_services()
    errors += check_sources()
    errors += check_health()
    errors += check_core_files()

    # cheap stats
    total = 0
    by_client = Counter()
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for client, fname in manifest.get("clients", {}).items():
        p = DATA / fname.removeprefix("data/")
        if p.exists():
            n = len(json.loads(p.read_text(encoding="utf-8")))
            total += n
            by_client[client] = n

    print("== validation report ==")
    print(f"total records: {total}")
    print(f"by client: {dict(by_client)}")
    print(f"errors: {len(errors)}")
    for e in errors[:40]:
        print("  -", e)

    if errors:
        print("VALIDATION FAILED")
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
