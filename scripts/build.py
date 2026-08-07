"""Main build orchestration.

Modes:
  auto          (default) incremental scan changed sources, reuse unchanged
  full-refresh  re-scan every source (ignore snapshot reuse for source SHA)
  source=<id>   scan only one source, keep all other shards from snapshot
  site-only     skip data scan entirely; reuse generated/*.json and rebuild site
  audit-only    scan + aggregate locally, write reports, do not commit/deploy

Flow:
  resolve (source SHAs, config hashes, decide changed)
  → per-source incremental scan (reused/refreshed/removed)
  → write generated/<source_id>.json shards
  → aggregate → dist/data/*
  → compute data_hash → decide if site needs rebuilding
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from github_api import GitHub
from snapshot import Snapshot, SNAPSHOT_PATH
from urlresolver import source_config_hash

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "generated"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    mode = os.environ.get("RI_MODE", "auto")
    cfg = load_config()
    snap = Snapshot.load()
    now = datetime.now(timezone.utc).isoformat()
    gh = GitHub()

    start = time.time()

    if mode == "site-only":
        # reuse generated shards; no GitHub API calls for data
        log("== site-only: reusing generated shards ==")
        _aggregate_and_report(cfg, now)
        return

    log("== resolve ==")
    # decide which sources need scanning
    from incremental import incremental_scan

    enabled = [s for s in cfg.sources if s.get("enabled", True)]
    selected = enabled
    if mode.startswith("source="):
        sid_filter = mode.split("=", 1)[1]
        selected = [s for s in enabled if s["id"] == sid_filter]
        log(f"  mode=source: scanning only {sid_filter}")

    shards = []
    metrics = {}
    for source in selected:
        sid = source["id"]
        cfg_hash = source_config_hash(source)
        prev_cfg = snap.source_config_hash(sid)
        prev_sha = snap.source_sha(sid)
        changed = mode == "full-refresh" or prev_cfg != cfg_hash or _source_changed(source, gh, snap)

        t0 = time.time()
        if changed:
            log(f"  scan {sid} ...")
            shard = incremental_scan(source, cfg, gh, snap, now)
            shards.append(shard)
            metrics[sid] = {
                "changed": True,
                "reused": shard.get("reused", 0),
                "refreshed": shard.get("refreshed", 0),
                "removed": shard.get("removed", 0),
                "failed": shard.get("failed", 0),
                "seconds": round(time.time() - t0, 2),
            }
        else:
            # REUSE whole source snapshot
            log(f"  reuse {sid} (sha unchanged)")
            prev_files = snap.source_files(sid)
            shard = {
                "source_id": sid,
                "tier": source.get("tier", "C"),
                "validation_mode": source.get("validation", {}).get("mode", "standard"),
                "config_hash": cfg_hash,
                "source_sha": prev_sha,
                "last_success_at": snap.source_last_success(sid),
                "reused": len(prev_files),
                "refreshed": 0,
                "removed": 0,
                "failed": 0,
                "records": prev_files,
            }
            shards.append(shard)
            metrics[sid] = {"changed": False, "reused": len(prev_files), "refreshed": 0,
                            "removed": 0, "failed": 0, "seconds": round(time.time() - t0, 2)}

    # sources not selected (mode=source) -> reuse snapshot shards
    selected_ids = {s["id"] for s in selected}
    for s in enabled:
        if s["id"] not in selected_ids:
            prev_files = snap.source_files(s["id"])
            if prev_files:
                shards.append({
                    "source_id": s["id"],
                    "tier": s.get("tier", "C"),
                    "validation_mode": s.get("validation", {}).get("mode", "standard"),
                    "config_hash": snap.source_config_hash(s["id"]),
                    "source_sha": snap.source_sha(s["id"]),
                    "last_success_at": snap.source_last_success(s["id"]),
                    "reused": len(prev_files),
                    "refreshed": 0, "removed": 0, "failed": 0,
                    "records": prev_files,
                })

    # write shards
    GENERATED.mkdir(exist_ok=True)
    for shard in shards:
        (GENERATED / f"{shard['source_id']}.json").write_text(
            json.dumps(shard, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )

    snap.update_updated_at(now)
    snap.save()

    _write_metrics(metrics, cfg)
    _aggregate_and_report(cfg, now, metrics)

    log(f"total time: {round(time.time() - start, 1)}s")


def _source_changed(source: dict, gh: GitHub, snap) -> bool:
    """Does the source-level fingerprint differ from the snapshot?"""
    from incremental import _current_source_sha

    sid = source["id"]
    cur = _current_source_sha(source, gh)
    prev = snap.source_sha(sid)
    if cur is None:
        return False  # can't verify -> rely on config hash
    return cur != prev


def _aggregate_and_report(cfg, now: str, metrics: dict | None = None) -> None:
    from aggregate import aggregate, load_shards, sha256_hex

    shards = load_shards()
    log(f"== aggregate ({len(shards)} shards) ==")
    res = aggregate(cfg, shards, now)
    log(f"  data_hash: {res['data_hash']}")

    # summary
    total_records = 0
    reused = refreshed = removed = failed = 0
    for shard in shards:
        total_records += len(shard.get("records", {}))
        reused += shard.get("reused", 0)
        refreshed += shard.get("refreshed", 0)
        removed += shard.get("removed", 0)
        failed += shard.get("failed", 0)

    rate = (reused / max(total_records, 1)) * 100
    log("")
    log("== build summary ==")
    log(f"  sources: {len(shards)}")
    log(f"  records: {total_records}  (reused {reused} / refreshed {refreshed} / removed {removed} / failed {failed})")
    log(f"  reuse rate: {rate:.2f}%")
    log(f"  data_hash: {res['data_hash']}")
    if metrics:
        for sid, m in sorted(metrics.items()):
            if m["changed"]:
                log(f"    {sid}: +{m['refreshed']} ~{m['reused']} -{m['removed']} !{m['failed']} {m['seconds']}s")


def _write_metrics(metrics: dict, cfg) -> None:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "build-metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
