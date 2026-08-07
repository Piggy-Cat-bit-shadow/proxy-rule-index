"""Metadata snapshot: per-source state enabling incremental scans.

The snapshot is the ONLY durable source of truth about what was previously
scanned. It lives in .snapshot/snapshot.json (git-ignored) and is also
committed to the repo's .snapshot/ so CI can recover it across runs even when
the Actions cache is cold.

Layout:
  {
    "schemaVersion": 1,
    "updated_at": "<iso>",
    "sources": {
      "<source_id>": {
        "source_sha": "...",           # git commit/tree sha at last scan
        "config_hash": "...",          # urlresolver.source_config_hash
        "last_success_at": "<iso>",
        "files": {
          "<canonical_key>": {
            "blob_sha": "...",
            "rule_count": 327,
            "rule_types": {...},
            "status": "available",
            "last_checked_at": "...",
            "last_success_at": "...",
            "failure_count": 0
          }
        }
      }
    }
  }
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / ".snapshot" / "snapshot.json"
GENERATED_DIR = Path(__file__).resolve().parent.parent / "generated"


class Snapshot:
    def __init__(self, data: dict | None = None):
        self.data = data or {"schemaVersion": SCHEMA_VERSION, "updated_at": None, "sources": {}}

    @classmethod
    def load(cls, path: Path = SNAPSHOT_PATH) -> "Snapshot":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("schemaVersion") == SCHEMA_VERSION:
                    return cls(data)
            except Exception:
                pass
        # Recover from committed generated shards when the working snapshot is
        # missing (e.g. cold CI cache). Shards carry source_sha/config_hash/
        # last_success_at + per-file records, so no data is lost.
        recovered = cls._from_shards()
        if recovered:
            return recovered
        return cls()

    @classmethod
    def _from_shards(cls) -> "Snapshot":
        import json as _json

        snap = cls()
        if not GENERATED_DIR.exists():
            return snap
        for p in sorted(GENERATED_DIR.glob("*.json")):
            try:
                shard = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = shard.get("source_id")
            if not sid:
                continue
            s = snap.source(sid)
            s["source_sha"] = shard.get("source_sha")
            s["config_hash"] = shard.get("config_hash")
            s["last_success_at"] = shard.get("last_success_at")
            s["files"] = shard.get("records", {})
        return snap

    def save(self, path: Path = SNAPSHOT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ---- source-level ----
    def source(self, sid: str) -> dict:
        return self.data["sources"].setdefault(sid, {"files": {}})

    def source_sha(self, sid: str) -> str | None:
        return self.source(sid).get("source_sha")

    def set_source_sha(self, sid: str, sha: str | None, config_hash: str, last_success: str | None = None) -> None:
        s = self.source(sid)
        s["source_sha"] = sha
        s["config_hash"] = config_hash
        if last_success:
            s["last_success_at"] = last_success

    def source_config_hash(self, sid: str) -> str | None:
        return self.source(sid).get("config_hash")

    def source_last_success(self, sid: str) -> str | None:
        return self.source(sid).get("last_success_at")

    # ---- file-level ----
    def file(self, sid: str, key: str) -> dict | None:
        return self.source(sid).get("files", {}).get(key)

    def set_file(self, sid: str, key: str, meta: dict) -> None:
        self.source(sid).setdefault("files", {})[key] = meta

    def set_source_files(self, sid: str, files: dict) -> None:
        self.source(sid)["files"] = files

    def source_files(self, sid: str) -> dict:
        return self.source(sid).get("files", {})

    def file_keys(self, sid: str) -> set[str]:
        return set(self.source_files(sid).keys())

    def update_updated_at(self, now: str) -> None:
        self.data["updated_at"] = now
