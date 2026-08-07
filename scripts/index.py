"""Shared helpers for the incremental scanner (resolve/dedupe/format/granularity).

Kept as a light module — build orchestration lives in build.py, per-source scan
in incremental.py, aggregation in aggregate.py.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from config import load_config

# preferred extension per client for dedupe when a repo ships multiple formats
CLIENT_PREFERRED_EXT = {
    "surge": ["list", "conf", "txt"],
    "loon": ["list", "lsr", "conf"],
    "shadowrocket": ["list", "conf"],
    "quantumultx": ["list", "conf"],
    "mihomo": ["yaml", "list", "txt"],
    "sing-box": ["json", "srs", "list"],
    "adguard": ["txt", "list"],
    "egern": ["list"],
    "stash": ["list"],
    "surfboard": ["list", "conf"],
}


def get_format_for(cand: dict, cfg) -> str:
    """Determine rule format from extension + overrides."""
    ext = cand.get("ext", "")
    override = cfg.get_format_override(cand["provider"], cand.get("client", ""), ext)
    if override:
        return override
    m = {
        "json": "ruleset_json",
        "mrs": "mrs",
        "srs": "singbox_srs",
        "mmdb": "geoip_mmdb",
        "dat": "geoip_dat",
        "db": "geoip_db",
        "lsr": "loon_ruleset",
        "conf": "plain_ruleset",
        "txt": "plain_ruleset",
        "list": "plain_ruleset",
        "yaml": "clash_yaml",
    }
    fmt = cand.get("format")
    if fmt:
        return fmt
    return m.get(ext, "plain_ruleset")


def resolve_service_id(cand: dict, cfg) -> tuple[str | None, str]:
    """Return (service_id, status) where status in {'known','mapped','dynamic','skip'}."""
    stem = cand["file_stem"]
    provider = cand["provider"]
    source = next((s for s in cfg.sources if s["id"] == provider), {})
    allow_dynamic = source.get("dynamic_services", False)
    # manual records carry explicit service_id
    if cand.get("service_id"):
        if cfg.has_service(cand["service_id"]):
            return cand["service_id"], "known"
        return cand["service_id"], "dynamic"

    # provider file mapping first
    mapped = cfg.map_provider_file(provider, stem)
    if mapped:
        if cfg.has_service(mapped):
            return mapped, "mapped"
        return mapped, "dynamic"

    # direct name resolution
    sid = cfg.resolve_service_id(stem)
    if sid:
        return sid, "known"

    # provider allows dynamic services -> create one
    if allow_dynamic:
        return provider + "-" + stem.lower().replace("_", "-"), "dynamic"

    return None, "skip"


def dedupe_candidates(cands: list[dict], cfg) -> list[dict]:
    """Remove same (provider, service, client, url) duplicates.

    For each (provider, service, client) group we keep a single canonical
    record: prefer native/primary extension for the client, prefer non-resolve
    variants over resolve/no-resolve variants, drop duplicate URLs.
    """
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for c in cands:
        by_key[(c["provider"], c.get("service_id"), c.get("client"))].append(c)

    kept: list[dict] = []
    for key, group in by_key.items():
        kept.append(_pick_canonical(group, cfg))
    return kept


def _pick_canonical(group: list[dict], cfg) -> dict:
    """Pick the single best record from a (provider, service, client) group."""
    if len(group) == 1:
        return group[0]
    client = group[0].get("client", "")
    pref = CLIENT_PREFERRED_EXT.get(client, [])
    # 1) prefer native/primary extension for the client
    for ext in pref:
        pool = [c for c in group if c.get("ext") == ext]
        if pool:
            group = pool
            break
    # 2) prefer non-resolve variants
    non_resolve = [c for c in group if not c.get("resolve_variant")]
    if non_resolve:
        group = non_resolve
    # 3) if still multiple distinct urls, prefer non-dynamic canonical path
    if len(group) == 1:
        return group[0]
    # 4) dedupe by url keeping first, preferring the shortest/cleanest path
    by_url: dict[str, list[dict]] = defaultdict(list)
    for c in group:
        by_url[c["url"]].append(c)
    group = [by_url[u][0] for u in by_url]
    # 5) last resort: prefer the smallest path depth
    group.sort(key=lambda c: (c.get("path", "").count("/"), len(c.get("url", ""))))
    return group[0]


def infer_granularity(c: dict, cfg) -> str:
    """Guess rule granularity from service + filename."""
    sid = c.get("service_id", "")
    svc = cfg.get_service(sid)
    stem = c["file_stem"].lower()
    # geo / ads / global catch-alls by name pattern
    if stem.startswith("china") or stem.startswith("cn") or stem in ("geolocation", "geolocation-cn", "geoip"):
        return "geo"
    if stem in ("global", "proxy", "gfw", "direct", "reject", "proxy-gfw", "proxymedia", "adguard"):
        return "global"
    if stem in ("ads", "reject", "adguard") or "ad" == stem or stem.startswith("easyprivacy") or stem.startswith("easylist") or stem.endswith("ads"):
        return "ads"
    if not svc:
        # dynamic service from a specific app-named file -> app
        return "app"
    cat = svc.get("category", "")
    if cat in ("Geo", "Ads"):
        return cat.lower()
    if cat in ("AI", "Social", "Streaming", "Google", "Apple", "Microsoft", "Cloud", "Academic", "Finance", "Games"):
        # umbrella brand names -> category (multiple products); everything else app
        umbrella_brands = {"Google", "Apple", "Microsoft", "Meta", "Amazon"}
        if svc["name"] in umbrella_brands:
            return "category"
        return "app"
    return "app"

