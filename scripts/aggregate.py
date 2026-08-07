"""Aggregate: turn per-source shards into deterministic, production-ready JSON.

Output layout (dist/):
  index.html
  assets/                  (frontend assets)
  data/
    manifest.json          (client -> catalog shard filename, schemaVersion, source_urls)
    build-info.json        (builtAt, dataVersion, schemaVersion)
    services.json          (canonical services + precomputed search + rank)
    sources.json           (source metadata + URL resolver info)
    stats.json             (cheap aggregate counters)
    health.json            (record_id -> status/timestamps, separated from stable data)
    catalog/
      surge.json
      loon.json
      shadowrocket.json
      egern.json
      mihomo.json
      sing-box.json
      <other>.json

Design:
  - Deterministic: stable ordering, no per-record timestamps, sort_keys on JSON.
  - Stable data (catalog shards) is separated from volatile health data.
  - data_hash covers catalog+services+sources so no-change runs produce an
    identical hash and can skip deploy.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from urlresolver import resolve_url, source_url_cfg, url_plan_for

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "data"
GENERATED = ROOT / "generated"

SCHEMA_VERSION = 2

# client -> sort rank inside the specificity/availability/support/tier tiebreak
CLIENT_ORDER = [
    "surge", "loon", "shadowrocket", "egern", "mihomo", "sing-box",
    "stash", "surfboard", "quantumultx", "adguard",
]

SPECIFICITY_RANK = {"app": 0, "service-family": 1, "category": 2, "geo": 3, "ads": 3, "special": 3, "global": 4}
STATUS_RANK = {"available": 0, "special": 0, "warning": 1, "unavailable": 2}
SUPPORT_RANK = {"native": 0, "compatible": 1, "converted": 2}
TIER_RANK = {"A": 0, "B": 1, "C": 2}


def _stable_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(obj) -> str:
    return hashlib.sha256(_stable_json(obj).encode("utf-8")).hexdigest()


def load_shards() -> list[dict]:
    shards = []
    if GENERATED.exists():
        for p in sorted(GENERATED.glob("*.json")):
            shards.append(json.loads(p.read_text(encoding="utf-8")))
    return shards


def flatten_records(shards: list[dict], cfg) -> list[dict]:
    """Convert snapshot-style per-file metadata into catalog record dicts.

    The canonical key is source|client|slug (stable). service_id is resolved
    HERE from the slug against the current aliases.yaml, so alias changes don't
    invalidate the snapshot or force re-downloads.
    """
    records = []
    svc_cache = {s["id"]: s for s in cfg.all_services()}
    for shard in shards:
        sid = shard["source_id"]
        source = next((s for s in cfg.sources if s["id"] == sid), {})
        for key, meta in shard.get("records", {}).items():
            parts = key.split("|")
            provider = parts[0]
            client = parts[1]
            slug = parts[2]
            # resolve service_id from slug
            mapped = cfg.map_provider_file(provider, slug)
            resolved = mapped or cfg.resolve_service_id(slug)
            if not resolved:
                allow_dynamic = source.get("dynamic_services", False)
                resolved = provider + "-" + slug.lower().replace("_", "-") if allow_dynamic else None
            service_id = resolved or slug.lower()
            svc = svc_cache.get(service_id, {})
            is_dynamic = service_id not in svc_cache
            name = (
                cfg.get_name_override(provider, slug)
                or svc.get("name")
                or (slug.replace("-", " ").replace("_", " ").title() if is_dynamic else slug)
            )
            support = source.get("clients", {}).get(client, {}).get("support", "compatible")
            granularity = _compute_granularity(slug, service_id, svc, cfg, source)
            summary = meta.get("summary")
            if not summary and meta.get("rule_types"):
                from collections import Counter as _C

                from parser import summarize_types

                summary = summarize_types(_C(meta["rule_types"]))

            record = {
                "k": key,
                "service_id": service_id,
                "name": name,
                "category": svc.get("category", "Other") if not is_dynamic else "Other",
                "client": client,
                "provider": provider,
                "tier": shard.get("tier", source.get("tier", "C")),
                "support": support,
                "granularity": granularity,
                "format": meta.get("format"),
                "slug": slug,
                "path": meta.get("path", ""),
                "status": meta.get("status", "unavailable"),
                "last_checked_at": meta.get("last_checked_at"),
                "last_success_at": meta.get("last_success_at"),
                "failure_count": meta.get("failure_count", 0),
                "rule_count": meta.get("rule_count"),
                "rule_types": meta.get("rule_types"),
                "type_summary": summary,
                "has_domain": bool(meta.get("rule_types") and any(k in meta["rule_types"] for k in ("domain", "domain_suffix", "domain_keyword", "domain_wildcard", "domain_regex"))),
                "has_ipv4": bool(meta.get("rule_types", {}) and meta["rule_types"].get("ip_cidr", 0) > 0),
                "has_ipv6": bool(meta.get("rule_types", {}) and meta["rule_types"].get("ip_cidr6", 0) > 0),
                "count_available": meta.get("count_available", True),
            }
            records.append(record)
    return records


def _slug_of(parts: list[str]) -> str:
    return parts[2]


def _path_of(parts: list[str], source: dict) -> str:
    return ""


def _compute_granularity(slug: str, service_id: str, svc: dict, cfg, source: dict) -> str:
    """Compute search granularity at build time (search metadata, not rule content).

    Priority mirrors the scan-time inference but is re-derived here so that
    aliases/category/override changes don't require re-downloading rules.
    """
    # explicit override wins
    ov = cfg.get_granularity_override(source.get("id", ""), slug)
    if ov:
        return ov
    stem = slug.lower()
    # geo / ads / global catch-alls by name pattern
    if stem.startswith("china") or stem.startswith("cn") or stem in ("geolocation", "geolocation-cn", "geoip"):
        return "geo"
    if stem in ("global", "proxy", "gfw", "direct", "reject", "proxy-gfw", "proxymedia", "adguard"):
        return "global"
    if stem in ("ads", "reject", "adguard") or "ad" == stem or stem.startswith("easyprivacy") or stem.startswith("easylist") or stem.endswith("ads"):
        return "ads"
    if not svc:
        return "app"
    cat = svc.get("category", "")
    if cat in ("Geo", "Ads"):
        return cat.lower()
    if cat in ("AI", "Social", "Streaming", "Google", "Apple", "Microsoft", "Cloud", "Academic", "Finance", "Games"):
        umbrella_brands = {"Google", "Apple", "Microsoft", "Meta", "Amazon"}
        if svc["name"] in umbrella_brands:
            return "category"
        return "app"
    return "app"


def rank_tuple(r: dict) -> tuple:
    return (
        SPECIFICITY_RANK.get(r.get("granularity", "category"), 5),
        STATUS_RANK.get(r.get("status", "unavailable"), 3),
        SUPPORT_RANK.get(r.get("support", "compatible"), 1),
        TIER_RANK.get(r.get("tier", "C"), 3),
        r.get("last_success_at", "") or "",
        -(r.get("rule_count") or 0),
    )


def split_health(records: list[dict]) -> dict:
    """Extract volatile status fields into health.json, leaving catalog stable."""
    health = []
    for r in records:
        health.append(
            {
                "k": r["k"],
                "status": r.pop("status", "unavailable"),
                "last_checked_at": r.pop("last_checked_at", None),
                "last_success_at": r.pop("last_success_at", None),
                "failure_count": r.pop("failure_count", 0),
            }
        )
    health.sort(key=lambda h: h["k"])
    return {"schemaVersion": SCHEMA_VERSION, "records": health}


def aggregate(cfg, shards: list[dict], now: str) -> dict:
    """Build all production outputs. Returns {data_hash, changed}."""
    records = flatten_records(shards, cfg)

    # deterministic sort by rank tuple, then client order, then name
    def sort_key(r):
        client_idx = CLIENT_ORDER.index(r["client"]) if r["client"] in CLIENT_ORDER else len(CLIENT_ORDER)
        return (rank_tuple(r), client_idx, r["name"].lower())

    records.sort(key=sort_key)

    # health extraction
    health_data = split_health(records)

    # cheap stats BEFORE per-client pop
    stats = {
        "schemaVersion": SCHEMA_VERSION,
        "total_services": len(cfg.all_services()),
        "services_with_rules": len({r["service_id"] for r in records}),
        "total_rule_files": len(records),
        "by_provider": dict(Counter(r["provider"] for r in records)),
        "by_client": dict(Counter(r["client"] for r in records)),
        "by_tier": dict(Counter(r["tier"] for r in records)),
    }

    # per-client catalog shards
    by_client: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_client[r["client"]].append(r)

    catalog_dir = DIST / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    client_manifest: dict[str, str] = {}
    for client, recs in sorted(by_client.items()):
        # keep `client` in records: the frontend expects it and validation reads it.
        fname = f"{client}.json"
        (catalog_dir / fname).write_text(_stable_json(recs) + "\n", encoding="utf-8")
        client_manifest[client] = f"data/catalog/{fname}"

    # services.json (precomputed search + rank for the frontend)
    services_out = []
    svc_counts = Counter(r["service_id"] for r in records)
    for svc in cfg.all_services():
        sid = svc["id"]
        services_out.append(
            {
                "id": sid,
                "name": svc["name"],
                "category": svc.get("category", "Other"),
                "aliases": svc.get("aliases", []),
                "search_text": make_search_text(svc),
                "rule_count": svc_counts.get(sid, 0),
            }
        )
    services_out.sort(key=lambda s: s["name"].lower())

    # sources.json with URL resolver info (no per-record URLs)
    sources_out = []
    for s in cfg.sources:
        if not s.get("enabled", True):
            continue
        sources_out.append(
            {
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "repo": s.get("repo"),
                "homepage": s.get("homepage"),
                "author": s.get("author"),
                "license": s.get("license", "unknown"),
                "tier": s.get("tier", "C"),
                "type": s.get("type", "app-rules"),
                "validation_mode": s.get("validation", {}).get("mode", "standard"),
                "clients": {k: v.get("support", "compatible") for k, v in s.get("clients", {}).items()},
                "url": url_cfg_to_public(s),
            }
        )
    sources_out.sort(key=lambda s: s["id"])

    # manifest.json
    source_urls = {}
    for s in cfg.sources:
        if s.get("enabled", True):
            u = url_cfg_to_public(s)
            if u:
                source_urls[s["id"]] = u
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "clients": {k: f"data/catalog/{k}.json" for k in sorted(client_manifest.keys())},
        "source_urls": source_urls,
    }

    # build-info.json
    data_hash = sha256_hex({
        "catalog": {c: sorted(r["k"] for r in by_client[c]) for c in sorted(by_client)},
        "services": [s["id"] for s in services_out],
        "sources": [s["id"] for s in sources_out],
    })
    build_info = {
        "schemaVersion": SCHEMA_VERSION,
        "dataVersion": data_hash[:16],
        "builtAt": now,
        "sourceSnapshotVersion": data_hash[:16],
    }

    # write
    (DIST / "manifest.json").write_text(_stable_json(manifest) + "\n", encoding="utf-8")
    (DIST / "services.json").write_text(_stable_json(services_out) + "\n", encoding="utf-8")
    (DIST / "sources.json").write_text(_stable_json(sources_out) + "\n", encoding="utf-8")
    (DIST / "stats.json").write_text(_stable_json(stats) + "\n", encoding="utf-8")
    (DIST / "health.json").write_text(_stable_json(health_data) + "\n", encoding="utf-8")
    (DIST / "build-info.json").write_text(_stable_json(build_info) + "\n", encoding="utf-8")

    return {"data_hash": data_hash[:16], "client_manifest": client_manifest}


def url_cfg_to_public(source: dict) -> dict:
    """Expose a compact, non-secret URL resolver description for the frontend."""
    u = source_url_cfg(source)
    scan = source.get("scan", {})
    mode = u.get("mode", "repo")
    if mode == "repo":
        return {
            "mode": "repo",
            "base": f"https://raw.githubusercontent.com/{source['repo']}",
            "branch": scan.get("branch", "master"),
            "template": u.get("template"),
        }
    if mode == "base":
        return {
            "mode": "base",
            "base": u.get("base", ""),
            "template": u.get("template"),
        }
    return {"mode": mode}


def make_search_text(svc: dict) -> str:
    parts = [svc["id"], svc["name"].lower(), svc.get("category", "").lower()]
    parts.extend(a.lower() for a in svc.get("aliases", []))
    return " ".join(dict.fromkeys(p for p in parts if p))
