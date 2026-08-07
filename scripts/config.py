"""Configuration loader for sources.yaml / aliases.yaml / overrides.yaml."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


class Config:
    def __init__(self, root: Path):
        self.root = root
        data_dir = root / "data"
        self.sources = yaml.safe_load((data_dir / "sources.yaml").read_text(encoding="utf-8"))
        self.aliases = yaml.safe_load((data_dir / "aliases.yaml").read_text(encoding="utf-8"))
        self.overrides = yaml.safe_load((data_dir / "overrides.yaml").read_text(encoding="utf-8"))
        self.services: list[dict] = self.aliases.get("services", [])
        self.provider_file_mapping: dict[str, dict[str, str]] = self.aliases.get("provider_file_mapping", {})

        self._service_by_id: dict[str, dict] = {}
        self._service_by_name_lower: dict[str, str] = {}
        self._alias_map: dict[str, str] = {}
        for svc in self.services:
            sid = svc["id"]
            self._service_by_id[sid] = svc
            self._service_by_name_lower[svc["name"].lower()] = sid
            for a in svc.get("aliases", []):
                key = a.strip().lower()
                if key:
                    self._alias_map[key] = sid

    # ---- services ----
    def has_service(self, sid: str) -> bool:
        return sid in self._service_by_id

    def get_service(self, sid: str) -> dict | None:
        return self._service_by_id.get(sid)

    def resolve_service_id(self, raw_name: str) -> str | None:
        """Resolve a raw name (file stem, alias) to a canonical service id."""
        n = raw_name.strip().lower()
        if n in self._service_by_name_lower:
            return self._service_by_name_lower[n]
        if n in self._alias_map:
            return self._alias_map[n]
        # normalized word-match: e.g. "Open AI" -> "openai", "YoutubeMusic" -> "youtubemusic"
        norm = "".join(ch for ch in n if ch.isalnum())
        for svc in self.services:
            if svc["name"].lower().replace(" ", "").replace("-", "") == norm:
                return svc["id"]
        if norm in self._alias_map:
            return self._alias_map[norm]
        return None

    def service_aliases(self, sid: str) -> list[str]:
        svc = self._service_by_id.get(sid)
        if not svc:
            return []
        return [a for a in svc.get("aliases", []) if a]

    def all_services(self) -> list[dict]:
        return list(self.services)

    # ---- provider file mapping ----
    def map_provider_file(self, provider: str, file_stem: str) -> str | None:
        """Map a provider-specific filename to a canonical service id."""
        mapping = self.provider_file_mapping.get(provider, {})
        return mapping.get(file_stem)

    # ---- overrides ----
    def is_excluded(self, provider: str, file_stem: str) -> bool:
        for e in self.overrides.get("service_overrides", {}).get("exclude", []):
            if e.get("provider") == provider and e.get("file") == file_stem:
                return True
        return False

    def get_granularity_override(self, provider: str, file_stem: str) -> str | None:
        for g in self.overrides.get("service_overrides", {}).get("granularity", []):
            if g.get("match", {}).get("provider") == provider and g["match"].get("file") == file_stem:
                return g.get("granularity")
        return None

    def is_client_excluded(self, provider: str, client: str) -> bool:
        for e in self.overrides.get("client_excludes", []):
            if e.get("provider") == provider and e.get("client") == client:
                return True
        return False

    def get_format_override(self, provider: str, client: str, ext: str) -> str | None:
        for o in self.overrides.get("client_format_overrides", []):
            if (
                o.get("provider") == provider
                and o.get("client") == client
                and o.get("ext") == ext
            ):
                return o.get("format")
        return None

    def get_name_override(self, provider: str, file_stem: str) -> str | None:
        for o in self.overrides.get("name_overrides", []):
            if o.get("provider") == provider and o.get("file") == file_stem:
                return o.get("name")
        return None


def load_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    return Config(root)


def main() -> None:
    cfg = load_config()
    print(f"sources: {len(cfg.sources)}")
    print(f"services: {len(cfg.services)}")
    # sanity: unique service ids
    ids = [s["id"] for s in cfg.services]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        print(f"WARNING duplicate service ids: {dupes}")
        sys.exit(1)
    print("config ok")


if __name__ == "__main__":
    main()
