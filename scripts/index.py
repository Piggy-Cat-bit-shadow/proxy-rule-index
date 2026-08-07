"""Index generator: turn discovered candidates + checks into catalog.json / services.json / sources.json / stats.json.

Pipeline:
  1. discover candidates
  2. resolve each candidate to a canonical service id (aliases / provider mapping / dynamic)
  3. apply overrides (granularity, excludes)
  4. fetch + check each unique URL (availability, parse, count)
  5. dedupe (provider + service + client + url)
  6. emit dist/*.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from checker import check_url, is_valid_url, special_status
from config import load_config
from discover import discover, raw_url, stem_of, normalize_client
from github_api import GitHub
from parser import summarize_types

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DATA = ROOT / "data"

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


def fetch_kelee_catalog(cfg) -> list[dict]:
    """Fetch KeLee's rule name catalog from luestr/ShuntRules README."""
    kelee = next((s for s in cfg.sources if s["id"] == "kelee"), None)
    if not kelee or not kelee.get("enabled", True):
        return []
    cat_url = kelee.get("catalog_url")
    if not cat_url:
        return []
    try:
        r = requests.get(cat_url, timeout=60,
                         headers={"User-Agent": "Mozilla/5.0 rule-index-checker"})
        if r.status_code != 200:
            print("  [kelee] catalog fetch failed", r.status_code)
            return []
        text = r.text
    except requests.RequestException as e:
        print(f"  [kelee] catalog error: {e}")
        return []

    names = set()
    # Loon entries: [Loon] Name (openloon import) or rule.kelee.one/Loon/Name.lsr
    for m in re.finditer(r"rule\.kelee\.one/(?:Loon|Clash)/([^/)\s\"']+?)(?:\.lsr|\.yaml)", text):
        names.add(m.group(1))
    # also match [Loon] Name link pattern
    for m in re.finditer(r"\[\[Loon\]\s+([^\]]+)\]\([^)]*?Loon/([^)]+?)\.lsr\)", text):
        names.add(m.group(2))

    out = []
    for name in sorted(names):
        for ckey, ccfg in kelee.get("clients", {}).items():
            pattern = ccfg.get("url_pattern", "")
            url = pattern.format(name=name)
            fmt = ccfg.get("format", "loon_ruleset")
            out.append(
                {
                    "provider": "kelee",
                    "tier": "C",
                    "repo": "luestr/ShuntRules",
                    "branch": "main",
                    "path": f"Loon/{name}.lsr" if ckey == "loon" else f"Clash/{name}.yaml",
                    "file_name": f"{name}.{url.rsplit('.', 1)[-1]}",
                    "file_stem": name,
                    "ext": url.rsplit(".", 1)[-1],
                    "client": normalize_client(ckey),
                    "size": 0,
                    "url": url,
                    "resolve_variant": False,
                    "source_type": "kelee_catalog",
                    "validation_mode": "special",
                }
            )
    print(f"  [kelee] catalog names: {len(names)} -> records: {len(out)}")
    return out


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


def main() -> None:
    cfg = load_config()
    gh = GitHub()
    now = datetime.now(timezone.utc).isoformat()

    print("== discovery ==")
    cands = discover(gh, cfg)
    # kelee catalog
    kelee_cands = fetch_kelee_catalog(cfg)
    cands.extend(kelee_cands)
    print(f"total candidates: {len(cands)}")

    # resolve service ids
    resolved = []
    dyn_services = set()
    skipped: Counter = Counter()
    for c in cands:
        sid, status = resolve_service_id(c, cfg)
        if status == "skip":
            skipped[c["provider"]] += 1
            continue
        if status == "dynamic":
            dyn_services.add(sid)
        c["service_id"] = sid
        resolved.append(c)

    if dyn_services:
        print(f"  dynamic services (not in aliases.yaml): {len(dyn_services)}")
        _write_dynamic_report(dyn_services)

    # apply excludes / granularity overrides
    filtered = []
    for c in resolved:
        if cfg.is_excluded(c["provider"], c["file_stem"]):
            continue
        if cfg.is_client_excluded(c["provider"], c.get("client", "")):
            continue
        c["granularity"] = cfg.get_granularity_override(c["provider"], c["file_stem"]) or infer_granularity(c, cfg)
        c["format"] = get_format_for(c, cfg)
        filtered.append(c)
    resolved = filtered

    # dedupe
    resolved = dedupe_candidates(resolved, cfg)
    print(f"after resolve+dedupe: {len(resolved)}")

    # availability check each unique URL (with cache across same url)
    url_cache: dict[str, dict] = {}
    unique_urls = []
    seen_urls = set()
    for c in resolved:
        c["indexed_at"] = now
        c["validation_mode"] = c.get("validation_mode") or source_validation(cfg, c["provider"])
        # special-mode sources (e.g. KeLee) are not HTTP-probed; they are
        # indexed as 'special' directly per the project rules.
        if c["validation_mode"] == "special":
            res = special_status(_err_result(None))
            c["status"] = res.status
            c["status_code"] = None
            c["rule_count"] = None
            c["rule_types"] = None
            c["summary"] = None
            c["format"] = c.get("format")
            c["errors"] = res.errors
            url_cache[c["url"]] = res
            continue
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            unique_urls.append(c)
    print(f"== availability check ({len(unique_urls)} unique urls) ==")
    workers = int(os.environ.get("RI_WORKERS", "16"))

    def check_all():
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_check_one, c, cfg): c for c in unique_urls}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = _err_result(e)
                url_cache[c["url"]] = res
                _apply_check(c, res)

    check_all()

    # build catalog records
    records = []
    for c in resolved:
        records.append(build_record(c, cfg, now))

    # sort: tier A>B>C, then granularity app>category>geo>global, then available first, then count desc
    records.sort(key=record_sort_key)

    # write outputs
    DIST.mkdir(exist_ok=True)
    write_outputs(records, cfg, now)

    report(records, cfg, skipped)


# ---- helpers ----

def source_validation(cfg, provider: str) -> str:
    for s in cfg.sources:
        if s["id"] == provider:
            return s.get("validation", {}).get("mode", "standard")
    return "standard"


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
        # distinct product names -> app; broad brand umbrella -> category
        broad_brands = {
            "Google", "Apple", "Microsoft", "YouTube", "Netflix", "Telegram", "X",
            "Instagram", "Facebook", "TikTok", "GitHub", "Amazon", "Meta",
        }
        if svc["name"] in broad_brands:
            return "category"
        return "app"
    return "app"


def _err_result(e):
    return type("R", (), {
        "status": "unavailable", "status_code": None,
        "errors": ([str(e)] if e else ["special access: not HTTP-probed"]),
        "rule_count": None, "rule_types": None, "summary": None,
        "format": None, "is_binary": False, "ok": False,
    })()


def _check_one(c: dict, cfg):
    from checker import check_url

    mode = c.get("validation_mode") or source_validation(cfg, c["provider"])
    try:
        res = check_url(c["url"], ext=c.get("ext", "list"), format_hint=c.get("format"))
    except Exception as e:
        res = _err_result(e)
    if mode == "special":
        res = special_status(res)
    return res


def _apply_check(c: dict, res) -> None:
    c["status"] = res.status
    c["status_code"] = res.status_code
    c["rule_count"] = res.rule_count
    c["rule_types"] = res.rule_types
    c["summary"] = res.summary
    if res.errors:
        c["errors"] = res.errors[:5]


def build_record(c: dict, cfg, now: str) -> dict:
    svc = cfg.get_service(c["service_id"]) or {}
    is_dynamic = c["service_id"] not in {s["id"] for s in cfg.all_services()}
    disp_name = c["file_stem"].replace("-", " ").replace("_", " ").title()
    name = (
        c.get("name_override")
        or cfg.get_name_override(c["provider"], c["file_stem"])
        or svc.get("name")
        or (disp_name if is_dynamic else c["file_stem"])
    )
    source = next((s for s in cfg.sources if s["id"] == c["provider"]), {})
    client_support = source.get("clients", {}).get(c.get("client", ""), {}).get("support", "compatible")
    category = svc.get("category", "Other")
    if is_dynamic:
        category = "Other"

    record = {
        "id": make_id(c),
        "service_id": c["service_id"],
        "name": name,
        "category": category,
        "is_dynamic": is_dynamic,
        "provider": c["provider"],
        "tier": c.get("tier", "C"),
        "client": c.get("client", ""),
        "format": c.get("format", "plain_ruleset"),
        "support": client_support,
        "granularity": c.get("granularity", "category"),
        "url": c["url"],
        "repo": c.get("repo", ""),
        "branch": c.get("branch", ""),
        "path": c.get("path", ""),
        "rule_count": c.get("rule_count"),
        "rule_types": c.get("rule_types"),
        "rule_type_summary": c.get("summary"),
        "contains_domain": bool(c.get("rule_types", {}) and any(k in c["rule_types"] for k in ("domain", "domain_suffix", "domain_keyword", "domain_wildcard", "domain_regex"))),
        "contains_ipv4": bool(c.get("rule_types", {}) and c["rule_types"].get("ip_cidr", 0) > 0),
        "contains_ipv6": bool(c.get("rule_types", {}) and c["rule_types"].get("ip_cidr6", 0) > 0),
        "status": c.get("status", "unavailable"),
        "validation_mode": c.get("validation_mode", "standard"),
        "last_checked_at": now,
        "last_success_at": now if c.get("status") in ("available", "special") else None,
        "indexed_at": now,
    }
    if c.get("errors"):
        record["errors"] = c["errors"]
    return record


def make_id(c: dict) -> str:
    parts = [c["provider"], c.get("service_id", ""), c.get("client", "")]
    if c.get("resolve_variant"):
        parts.append("resolve")
    if c.get("ext"):
        parts.append(c["ext"])
    return "-".join(parts)


def record_sort_key(r: dict):
    """Sort priority (hard rule from project spec):
    app-specific > category > combined > global; within a level:
    available > native > tier A>B>C > freshness > rule count.
    """
    gran_rank = {
        "app": 0,
        "service-family": 1,
        "category": 2,
        "geo": 3,
        "ads": 3,
        "special": 3,
        "global": 4,
    }.get(r.get("granularity", "category"), 5)
    status_rank = {"available": 0, "special": 0, "warning": 1, "unavailable": 2}.get(
        r.get("status", "unavailable"), 3
    )
    native_rank = {"native": 0, "compatible": 1, "converted": 2}.get(
        r.get("support", "compatible"), 1
    )
    tier_rank = {"A": 0, "B": 1, "C": 2}.get(r.get("tier", "C"), 3)
    last_success = r.get("last_success_at") or ""
    count = r.get("rule_count") or 0
    return (gran_rank, status_rank, native_rank, tier_rank, last_success, -count)


def _write_dynamic_report(dyn_services) -> None:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "dynamic-services.txt").write_text(
        "\n".join(sorted(dyn_services)) + "\n", encoding="utf-8"
    )


def write_outputs(records: list[dict], cfg, now: str) -> None:
    # catalog.json
    (DIST / "catalog.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # services.json
    services_out = []
    for svc in cfg.all_services():
        services_out.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "category": svc.get("category", "Other"),
                "aliases": svc.get("aliases", []),
                "search_text": make_search_text(svc),
                "rule_count": sum(1 for r in records if r["service_id"] == svc["id"]),
            }
        )
    (DIST / "services.json").write_text(
        json.dumps(services_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # sources.json
    sources_out = []
    for s in cfg.sources:
        if not s.get("enabled", True):
            continue
        prow = [r for r in records if r["provider"] == s["id"]]
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
                "rule_files": len(prow),
                "available_files": sum(1 for r in prow if r["status"] in ("available", "special")),
                "last_indexed": now,
            }
        )
    (DIST / "sources.json").write_text(
        json.dumps(sources_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # stats.json
    stats = {
        "generated_at": now,
        "total_services": len(cfg.all_services()),
        "services_with_rules": len({r["service_id"] for r in records}),
        "total_rule_files": len(records),
        "available_files": sum(1 for r in records if r["status"] in ("available", "special")),
        "by_provider": dict(Counter(r["provider"] for r in records)),
        "by_client": dict(Counter(r["client"] for r in records)),
        "by_tier": dict(Counter(r["tier"] for r in records)),
        "by_status": dict(Counter(r["status"] for r in records)),
        "by_category": dict(Counter(r["category"] for r in records)),
    }
    (DIST / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("  wrote dist/catalog.json, dist/services.json, dist/sources.json, dist/stats.json")


def make_search_text(svc: dict) -> str:
    parts = [svc["id"], svc["name"].lower(), svc.get("category", "").lower()]
    parts.extend(a.lower() for a in svc.get("aliases", []))
    return " ".join(dict.fromkeys(p for p in parts if p))


def report(records: list[dict], cfg, skipped: Counter) -> None:
    print("\n== build report ==")
    print(f"scanned sources: {sum(1 for s in cfg.sources if s.get('enabled', True))}")
    print(f"services: {len(cfg.all_services())}")
    print(f"rule files: {len(records)}")
    from collections import Counter as C

    print(f"  available/special: {sum(1 for r in records if r['status'] in ('available', 'special'))}")
    print(f"  warning: {sum(1 for r in records if r['status'] == 'warning')}")
    print(f"  unavailable: {sum(1 for r in records if r['status'] == 'unavailable')}")
    print(f"  by provider: {dict(C(r['provider'] for r in records))}")
    if skipped:
        print(f"  skipped (unmapped files): {dict(skipped)}")


if __name__ == "__main__":
    main()
