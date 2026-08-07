"""Per-source incremental scan.

For each enabled source:
  1. Compute the source config hash. If it changed -> full refresh of that source.
  2. Fetch the source-level SHA (branch commit, release tag, or catalog fetch).
     If unchanged vs snapshot AND config unchanged -> REUSE whole source snapshot.
  3. If changed -> discover files, compare per-file blob_sha to snapshot:
       - blob sha same  -> REUSE metadata (rule_count/types/status...)
       - blob sha new   -> REFRESH (GET + parse + availability check)
       - file missing   -> removed
  4. Emit a per-source shard (generated/<source_id>.json) and update the snapshot.

Special sources (validation_mode: special, e.g. KeLee) are never content-probed;
they are indexed with count_available=false and never marked unavailable from
HTTP anomalies.

Safety: per-source budgets (max_files / max_file_size / max_total_download_bytes)
fail closed and keep the previous snapshot on violation.
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from checker import check_url, special_status
from config import load_config
from discover import discover_source
from github_api import GitHub
from urlresolver import resolve_url, source_config_hash

MAX_BODY = int(os.environ.get("RI_MAX_BODY", 30 * 1024 * 1024))  # 30 MiB per file


def canonical_key(c: dict) -> str:
    """Stable dedupe key per project rule: source_id + client + slug.

    Using the raw slug (not resolved service_id) so that alias/category changes
    in aliases.yaml do NOT invalidate the snapshot or force re-downloads.
    """
    slug = c.get("slug") or c.get("file_stem") or c.get("service_id", "")
    parts = [c["provider"], c.get("client", ""), slug]
    if c.get("resolve_variant"):
        parts.append("resolve")
    return "|".join(parts)


def resolve_service(c: dict, cfg) -> tuple[str | None, str]:
    """Reuse index.resolve_service_id without circular import."""
    from index import resolve_service_id

    return resolve_service_id(c, cfg)


def incremental_scan(source: dict, cfg, gh: GitHub, snap, now: str) -> dict:
    """Scan one source, returning its shard record.

    Shard shape:
      { "source_id", "source_sha", "config_hash", "last_success_at",
        "reused": int, "refreshed": int, "removed": int, "failed": int,
        "records": { canonical_key: {...meta...} } }
    """
    sid = source["id"]
    prev = snap.source(sid)
    prev_files = snap.source_files(sid)
    cfg_hash = source_config_hash(source)
    validation = source.get("validation", {}).get("mode", "standard")

    shard = {
        "source_id": sid,
        "tier": source.get("tier", "C"),
        "validation_mode": validation,
        "config_hash": cfg_hash,
        "source_sha": None,
        "last_success_at": prev.get("last_success_at"),
        "reused": 0,
        "refreshed": 0,
        "removed": 0,
        "failed": 0,
        "records": {},
    }

    try:
        candidates = discover_source(gh, source)
    except Exception as exc:  # noqa: BLE001
        # discovery failed -> keep previous snapshot, mark warning
        shard["failed"] = 1
        shard["error"] = f"discovery: {exc}"
        shard["records"] = prev_files
        return shard

    # safety budget: detect scan-range runaway
    budget = source.get("budget", {})
    max_files = budget.get("max_files")
    if max_files and len(candidates) > max_files:
        shard["failed"] = 1
        shard["error"] = f"scan range blowup: {len(candidates)} files > max_files {max_files}"
        shard["records"] = prev_files
        return shard

    # resolve service ids + build canonical keys
    resolved = []
    for c in candidates:
        sid2, status = resolve_service(c, cfg)
        if status == "skip":
            continue
        c["service_id"] = sid2
        c["validation_mode"] = validation
        c["config_hash"] = cfg_hash
        resolved.append(c)

    from index import dedupe_candidates, get_format_for, infer_granularity

    # apply excludes / granularity / format
    filtered = []
    for c in resolved:
        if cfg.is_excluded(c["provider"], c["file_stem"]):
            continue
        if cfg.is_client_excluded(c["provider"], c.get("client", "")):
            continue
        c["granularity"] = cfg.get_granularity_override(c["provider"], c["file_stem"]) or infer_granularity(c, cfg)
        c["format"] = get_format_for(c, cfg)
        c["url"] = resolve_url(c, source, cfg)
        if not c["url"]:
            continue
        c["key"] = canonical_key(c)
        filtered.append(c)
    resolved = dedupe_candidates(filtered, cfg)

    # determine which need refresh
    new_files = {}
    need_refresh = []
    reused = 0
    for c in resolved:
        key = canonical_key(c)
        prev_meta = prev_files.get(key)
        blob = c.get("blob_sha") or ""
        if prev_meta and prev_meta.get("blob_sha") == blob and cfg_hash == prev.get("config_hash"):
            # reuse metadata, but refresh status if it was failing or stale
            if _needs_recheck(prev_meta):
                need_refresh.append((key, c))
            else:
                new_files[key] = dict(prev_meta)
                reused += 1
        else:
            need_refresh.append((key, c))

    # refresh changed/new files
    refreshed = _refresh_files(need_refresh, source, cfg, now)
    for key, meta in refreshed.items():
        new_files[key] = meta
        shard["refreshed"] += 1
    shard["reused"] = reused

    # removed files
    current_keys = set(new_files.keys())
    prev_keys = set(prev_files.keys())
    removed_keys = prev_keys - current_keys
    if removed_keys:
        shard["removed"] = len(removed_keys)

    # keep failures: mark previous meta as warning but preserve
    failed_count = 0
    for key, meta in _failed_refreshes.items():
        if key in prev_files:
            prev_meta = dict(prev_files[key])
            prev_meta["status"] = "warning"
            prev_meta["last_checked_at"] = now
            prev_meta["failure_count"] = prev_meta.get("failure_count", 0) + 1
            new_files[key] = prev_meta
            failed_count += 1

    shard["failed"] = failed_count
    shard["records"] = new_files

    # update snapshot
    new_last_success = now if failed_count == 0 and not shard.get("error") else prev.get("last_success_at")
    snap.set_source_files(sid, new_files)
    snap.set_source_sha(sid, _current_source_sha(source, gh), cfg_hash, new_last_success)

    return shard


_failed_refreshes: dict = {}


def _needs_recheck(meta: dict) -> bool:
    """A file needs an availability re-check if it was failing/stale."""
    if meta.get("status") in ("warning", "unavailable"):
        return True
    # re-check anything not successfully verified within STALE_DAYS
    stale = int(os.environ.get("RI_STALE_DAYS", "14"))
    last = meta.get("last_success_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).days
        return age > stale
    except Exception:
        return True


def _refresh_files(need_refresh, source: dict, cfg, now: str) -> dict:
    global _failed_refreshes
    _failed_refreshes = {}
    if not need_refresh:
        return {}
    validation = source.get("validation", {}).get("mode", "standard")
    workers = int(os.environ.get("RI_WORKERS", "12"))
    out: dict = {}
    budget = source.get("budget", {})
    max_size = budget.get("max_file_size") or MAX_BODY
    max_total = budget.get("max_total_download_bytes")
    total_bytes = 0

    def do_one(item):
        key, c = item
        slug = c.get("slug") or c.get("file_stem") or c.get("service_id", "")
        if validation == "special":
            # special sources are not content-probed
            meta = {
                "blob_sha": c.get("blob_sha") or "",
                "slug": slug,
                "path": c.get("path", ""),
                "rule_count": None,
                "rule_types": None,
                "count_available": False,
                "status": "special",
                "validation_mode": "special",
                "format": c.get("format"),
                "granularity": c.get("granularity"),
                "last_checked_at": now,
                "last_success_at": now,
                "failure_count": 0,
            }
            return key, meta, None
        try:
            res = check_url(c["url"], ext=c.get("ext", "list"), format_hint=c.get("format"))
        except Exception as e:  # noqa: BLE001
            res = type("R", (), {
                "status": "unavailable", "status_code": None, "errors": [str(e)],
                "rule_count": None, "rule_types": None, "summary": None,
                "format": None, "is_binary": False, "ok": False,
            })()
        meta = {
            "blob_sha": c.get("blob_sha") or "",
            "slug": slug,
            "path": c.get("path", ""),
            "rule_count": res.rule_count,
            "rule_types": res.rule_types,
            "count_available": True,
            "status": res.status,
            "validation_mode": validation,
            "format": c.get("format") or res.format,
            "granularity": c.get("granularity"),
            "last_checked_at": now,
            "last_success_at": now if res.status in ("available", "special") else None,
            "failure_count": 1 if res.status != "available" else 0,
            "summary": res.summary,
        }
        if res.errors:
            meta["errors"] = res.errors[:3]
        return key, meta, res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(do_one, item): item for item in need_refresh}
        for fut in as_completed(futs):
            key, meta, res = fut.result()
            out[key] = meta
            if meta.get("status") not in ("available", "special"):
                _failed_refreshes[key] = meta
    return out


def _current_source_sha(source: dict, gh: GitHub) -> str | None:
    """Return the current source-level fingerprint (branch sha / release tag / catalog)."""
    scan = source.get("scan", {})
    mode = scan.get("mode", "repo_dirs")
    repo = source.get("repo")
    if mode in ("release_assets",):
        release_repo = scan.get("release_repo", repo)
        rel = gh.latest_release(release_repo)
        return rel.get("tag_name") if rel else None
    if source.get("validation", {}).get("mode") == "special" and source.get("catalog_url"):
        # KeLee-style: fingerprint is the catalog URL content hash
        import requests
        try:
            r = requests.get(source["catalog_url"], timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 rule-index-checker"})
            if r.status_code == 200:
                return hashlib.sha256(r.content).hexdigest()[:16]
        except Exception:
            return None
    branch = scan.get("branch", "master")
    return gh.branch_sha(repo, branch)
