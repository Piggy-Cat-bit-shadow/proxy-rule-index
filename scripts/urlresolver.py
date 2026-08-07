"""URL resolution: derive the final download URL for a candidate from source config.

Priority (per project rule):
  1. url_override           -> used verbatim
  2. path_override          -> template/base + path_override
  3. template + slug        -> derived from the source's url.template

Rules of thumb:
  - The raw.githubusercontent.com URL for repo-dir sources can be derived from
    repo + branch + path, so it is never stored per-record.
  - For url_list / kelee / release sources the URL is derived from a template.
  - Only truly irregular records carry a url_override.
"""
from __future__ import annotations

import hashlib
import json

RAW_BASE = "https://raw.githubusercontent.com"


def source_url_cfg(source: dict) -> dict:
    return source.get("url", {}) or {}


def source_config_hash(source: dict) -> str:
    """Deterministic fingerprint of the fields that affect scanning a source.

    Used to decide whether a source's scan config changed between runs
    (in which case a full re-scan is warranted even if the source SHA is same).
    """
    keep = {}
    for key in ("id", "repo", "branch", "tier", "type"):
        keep[key] = source.get(key)
    scan = source.get("scan", {})
    scan_keep = {}
    for key in ("branch", "root", "mode", "nested", "base_url", "rule_files", "ignore_dirs", "ignore_files", "resolve_variants", "client_dirs", "names", "url_patterns"):
        if key in scan:
            scan_keep[key] = scan[key]
    keep["scan"] = scan_keep
    keep["clients"] = source.get("clients", {})
    keep["validation"] = source.get("validation", {})
    keep["url"] = source.get("url", {})
    blob = json.dumps(keep, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def resolve_url(cand: dict, source: dict, cfg) -> str | None:
    """Return the final download URL for a candidate (or None if unresolvable).

    - url_override on the candidate wins.
    - If the source has an explicit url config (mode base/template), derive from it.
    - Otherwise (no url config) keep the candidate's discovered url as-is.
    """
    # 1. url_override
    uo = cand.get("url_override")
    if uo:
        return uo
    url_cfg = source_url_cfg(source)
    mode = url_cfg.get("mode", "repo")
    slug = cand.get("slug") or cand.get("file_stem") or ""
    path = cand.get("path", "")

    if mode == "repo":
        # derive from base + branch + path when we have a path (raw.githubusercontent)
        if path:
            base = url_cfg.get("base", f"{RAW_BASE}/{source['repo']}").rstrip("/")
            branch = cand.get("branch") or source.get("scan", {}).get("branch", "master")
            return f"{base}/{branch}/{path}"
        # no path -> template or fall back to discovered url
        tmpl = url_cfg.get("template")
        if tmpl:
            base = url_cfg.get("base", f"{RAW_BASE}/{source['repo']}").rstrip("/")
            branch = cand.get("branch") or source.get("scan", {}).get("branch", "master")
            return f"{base}/{branch}/{tmpl.format(slug=slug)}"
        return cand.get("url")

    if mode == "base":
        base = url_cfg.get("base", "").rstrip("/")
        if not base:
            return cand.get("url")
        po = cand.get("path_override")
        if po:
            return f"{base}/{po.lstrip('/')}"
        tmpl = url_cfg.get("template")
        if tmpl:
            return tmpl.format(slug=slug)
        if path:
            return f"{base}/{path.lstrip('/')}"
        return cand.get("url")

    # unknown mode
    return cand.get("url")


def url_plan_for(cand: dict) -> dict:
    """Compact description of how the URL is derived, for the catalog record.

    The frontend uses this (plus manifest.source_urls) to reconstruct URLs.
    """
    if cand.get("url_override"):
        return {"override": cand["url_override"]}
    if cand.get("path_override"):
        return {"path": cand["path_override"]}
    return {"slug": cand.get("slug") or cand.get("file_stem", "")}
