"""Rule-file discovery: turn sources.yaml + GitHub API into candidate rule entries.

Each candidate is a dict like:
    {
      "provider": "blackmatrix7",
      "tier": "A",
      "repo": "blackmatrix7/ios_rule_script",
      "branch": "master",
      "path": "rule/Surge/Telegram/Telegram.list",
      "client": "surge",
      "file_stem": "Telegram",
      "resolve_variant": false,
      "ext": "list",
      "format": "plain_ruleset",          # later refined by parser overrides
      "url": "https://raw.githubusercontent.com/...",
      "source_type": "repo_dir" | "url_list" | "manual" | "release",
    }
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from config import load_config
from github_api import GitHub, raw_url

VALID_EXTS = {"list", "yaml", "txt", "json", "srs", "mrs", "conf", "lsr", "srmodule", "mmdb", "dat", "db", "md"}


def normalize_client(client_key: str) -> str:
    m = {
        "surge": "surge",
        "clash": "mihomo",
        "loon": "loon",
        "shadowrocket": "shadowrocket",
        "quantumultx": "quantumultx",
        "adguard": "adguard",
        "mihomo": "mihomo",
        "sing-box": "sing-box",
        "singbox": "sing-box",
        "stash": "stash",
        "surfboard": "surfboard",
        "egern": "egern",
        "meta": "mihomo",
    }
    return m.get(client_key.lower(), client_key.lower())


def stem_of(name: str) -> tuple[str, str]:
    if "." in name:
        return name[: name.rindex(".")], name[name.rindex(".") + 1 :].lower()
    return name, ""


def norm_exts(exts: list[str]) -> set[str]:
    return {e.lstrip(".").lower() for e in exts}


def strip_variant(stem: str, variants: list[str]) -> tuple[str, bool]:
    # longest variant first so "_All_No_Resolve" wins over "_Resolve"
    for v in sorted(variants, key=len, reverse=True):
        if stem.endswith(v):
            return stem[: -len(v)], True
    return stem, False


class SubtreeWalker:
    """Walk a GitHub repo's directory tree.

    If the full recursive tree fits (not truncated) it is cached and all
    lookups are prefix-matches over that in-memory index — no per-dir API
    calls. Otherwise it walks sub-trees via the git trees API for the specific
    directory (handles dirs larger than the contents-API 1000-item cap).
    """

    def __init__(self, client: GitHub, repo: str):
        self.client = client
        self.repo = repo
        self._cache: dict[str, list[dict]] = {}
        self._full_tree: dict[str, dict] | None = None
        self._branch_seen: set[str] = set()

    def _ensure_full_tree(self, branch: str) -> dict[str, dict] | None:
        if branch in self._branch_seen:
            return self._full_tree
        self._branch_seen.add(branch)
        t = self.client.tree(self.repo, branch)
        if not t or t.get("truncated"):
            self._full_tree = None
            return None
        idx: dict[str, dict] = {}
        for e in t.get("tree", []):
            p = e.get("path", "")
            if p:
                e2 = dict(e)
                e2["type"] = "dir" if e2.get("type") == "tree" else "file"
                idx[p] = e2
        self._full_tree = idx
        return idx

    def list_dir(self, path: str, branch: str) -> list[dict]:
        """Return entries (dir or file dicts) directly under `path`."""
        key = f"{branch}:{path}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        prefix = f"{path.rstrip('/')}/"
        full = self._ensure_full_tree(branch)
        if full:
            entries = []
            for p, e in full.items():
                if p.startswith(prefix) and "/" not in p[len(prefix):]:
                    e2 = dict(e)
                    e2["name"] = p.rsplit("/", 1)[-1]
                    entries.append(e2)
            entries.sort(key=lambda e: e.get("path", ""))
            self._cache[key] = entries
            return entries

        # full tree unavailable/truncated -> use contents API, fall back to git subtree
        entries = self.client.list_dir(self.repo, path, branch)
        if not entries:
            entries = self._git_subtree(path, branch)
        self._cache[key] = entries
        return entries

    def _git_subtree(self, path: str, branch: str) -> list[dict]:
        try:
            node = self.client.git_subtree(self.repo, branch, path)
        except Exception:
            return []
        if not node:
            return []
        out = []
        for e in node.get("tree", []):
            out.append(
                {
                    "type": "tree" if e.get("type") == "tree" else "blob",
                    "name": e.get("path", "").rsplit("/", 1)[-1],
                    "path": f"{path}/{e['path']}" if path else e["path"],
                    "size": e.get("size", 0),
                    "sha": e.get("sha"),
                }
            )
        return out


def discover(client: GitHub, cfg) -> list[dict]:
    candidates: list[dict] = []
    enabled = [s for s in cfg.sources if s.get("enabled", True)]
    for source in enabled:
        candidates.extend(discover_source(client, source))
    return candidates


def discover_source(client: GitHub, source: dict) -> list[dict]:
    """Discover candidates for a single source (used by incremental scans)."""
    provider = source["id"]
    tier = source.get("tier", "C")
    repo = source["repo"]
    scan = source.get("scan", {})
    mode = scan.get("mode", "repo_dirs")
    branch = scan.get("branch", "master")
    try:
        if mode == "kelee_catalog":
            return _discover_kelee_catalog(source, tier)
        return _discover_one(client, source, provider, tier, repo, scan, mode, branch)
    except Exception as exc:  # noqa: BLE001
        print(f"  [discover] ERROR {provider}: {exc}")
        return []


def _discover_kelee_catalog(source: dict, tier: str) -> list[dict]:
    """Fetch KeLee's rule name catalog from the catalog README.

    Produces candidates that are NOT content-probed (validation_mode: special).
    """
    import re

    import requests

    cat_url = source.get("catalog_url")
    if not cat_url:
        return []
    try:
        r = requests.get(cat_url, timeout=60,
                         headers={"User-Agent": "Mozilla/5.0 rule-index-checker"})
        if r.status_code != 200:
            return []
        text = r.text
    except requests.RequestException:
        return []

    names = set()
    for m in re.finditer(r"rule\.kelee\.one/(?:Loon|Clash)/([^/)\s\"']+?)(?:\.lsr|\.yaml)", text):
        names.add(m.group(1))
    for m in re.finditer(r"\[\[Loon\]\s+([^\]]+)\]\([^)]*?Loon/([^)]+?)\.lsr\)", text):
        names.add(m.group(2))

    out = []
    for name in sorted(names):
        for ckey, ccfg in source.get("clients", {}).items():
            pattern = ccfg.get("url_pattern", "")
            url = pattern.format(name=name)
            fmt = ccfg.get("format", "loon_ruleset")
            ext = url.rsplit(".", 1)[-1]
            path = f"Loon/{name}.lsr" if ckey == "loon" else f"Clash/{name}.yaml"
            out.append(
                {
                    "provider": source["id"],
                    "tier": tier,
                    "repo": source["repo"],
                    "branch": "main",
                    "path": path,
                    "file_name": f"{name}.{ext}",
                    "file_stem": name,
                    "slug": name,
                    "ext": ext,
                    "client": normalize_client(ckey),
                    "size": 0,
                    "url": url,
                    "resolve_variant": False,
                    "source_type": "kelee_catalog",
                    "validation_mode": "special",
                }
            )
    print(f"  [discover] kelee catalog names: {len(names)} -> candidates: {len(out)}")
    return out


def is_dir(e: dict) -> bool:
    return e.get("type") in ("dir", "tree")


def is_file(e: dict) -> bool:
    return e.get("type") == "blob" or e.get("type") == "file"


def _discover_one(client, source, provider, tier, repo, scan, mode, branch) -> list[dict]:
    out: list[dict] = []
    root = scan.get("root", "").strip("/")
    nested = scan.get("nested", False)
    ignore_dirs = set(scan.get("ignore_dirs", []))
    resolve_variants = scan.get("resolve_variants", [])
    rule_exts = norm_exts(scan.get("rule_files", [".list"]))
    walker = SubtreeWalker(client, repo)

    if mode == "repo_dirs":
        for client_key, cdir in scan.get("client_dirs", {}).items():
            cclient = normalize_client(client_key)
            prefix = f"{root}/{cdir}" if root else cdir
            if nested:
                # layout: <prefix>/<Service>/<file>
                for d in walker.list_dir(prefix, branch):
                    if not is_dir(d):
                        continue
                    svc_dir = d.get("name", "")
                    if not svc_dir or svc_dir in ignore_dirs:
                        continue
                    for f in walker.list_dir(d["path"], branch):
                        if not is_file(f):
                            continue
                        name = f.get("name", "")
                        if name.lower() in {"readme.md", "readme"}:
                            continue
                        ext = stem_of(name)[1]
                        if ext not in rule_exts:
                            continue
                        stem, rv = strip_variant(stem_of(name)[0], resolve_variants)
                        if not stem:
                            continue
                        out.append(
                            {
                                "provider": provider,
                                "tier": tier,
                                "repo": repo,
                                "branch": branch,
                                "path": f["path"],
                                "file_name": name,
                                "file_stem": stem,
                                "ext": ext,
                                "client": cclient,
                                "size": f.get("size", 0),
                                "blob_sha": f.get("sha"),
                                "url": raw_url(repo, branch, f["path"]),
                                "resolve_variant": rv,
                                "source_type": "repo_dir",
                            }
                        )
            else:
                # flat layout: <prefix>/<file>
                for f in walker.list_dir(prefix, branch):
                    if not is_file(f):
                        continue
                    name = f.get("name", "")
                    if name.lower() in {"readme.md", "readme"}:
                        continue
                    ext = stem_of(name)[1]
                    if ext not in rule_exts:
                        continue
                    stem, rv = strip_variant(stem_of(name)[0], resolve_variants)
                    if not stem:
                        continue
                    out.append(
                        {
                            "provider": provider,
                            "tier": tier,
                            "repo": repo,
                            "branch": branch,
                            "path": f["path"],
                            "file_name": name,
                            "file_stem": stem,
                            "ext": ext,
                            "client": cclient,
                            "size": f.get("size", 0),
                            "blob_sha": f.get("sha"),
                            "url": raw_url(repo, branch, f["path"]),
                            "resolve_variant": rv,
                            "source_type": "repo_dir",
                        }
                    )

    elif mode == "root_files":
        ignore_files = set(scan.get("ignore_files", []))
        for f in walker.list_dir(root, branch):
            if not is_file(f):
                continue
            name = f.get("name", "")
            if name.lower() in {"readme.md", "readme"} or name in ignore_files:
                continue
            ext = stem_of(name)[1]
            if ext not in rule_exts:
                continue
            stem = stem_of(name)[0]
            path = f"{root}/{name}" if root else name
            out.append(
                {
                    "provider": provider,
                    "tier": tier,
                    "repo": repo,
                    "branch": branch,
                    "path": path,
                    "file_name": name,
                    "file_stem": stem,
                    "ext": ext,
                    "client": None,
                    "size": f.get("size", 0),
                    "url": raw_url(repo, branch, path),
                    "resolve_variant": False,
                    "source_type": "root_files",
                }
            )

    elif mode == "git_subtree":
        for bc in scan.get("branches", []):
            bname = bc.get("branch", "master")
            bclient = normalize_client(bc.get("client", "mihomo"))
            for d in bc.get("dirs", []):
                for f in walker.list_dir(d, bname):
                    if not is_file(f):
                        continue
                    name = f.get("name", "")
                    ext = stem_of(name)[1]
                    if ext not in norm_exts(bc.get("rule_files", [".list"])):
                        continue
                    stem = stem_of(name)[0]
                    if not stem or stem.lower().startswith("as") or "/" in stem:
                        continue
                    out.append(
                        {
                            "provider": provider,
                            "tier": tier,
                            "repo": repo,
                            "branch": bname,
                            "path": f["path"],
                            "file_name": name,
                            "file_stem": stem,
                            "ext": ext,
                            "client": bclient,
                            "size": f.get("size", 0),
                            "blob_sha": f.get("sha"),
                            "url": raw_url(repo, bname, f["path"]),
                            "resolve_variant": False,
                            "source_type": "repo_dir",
                            "base_name": bc.get("base", ""),
                        }
                    )

    elif mode == "url_list":
        base_url = scan.get("base_url", "").rstrip("/")
        for name in scan.get("names", []):
            for pat in scan.get("url_patterns", []):
                url = f"{base_url}/{pat['path'].format(name=name)}"
                ext = pat["path"].rsplit(".", 1)[-1]
                out.append(
                    {
                        "provider": provider,
                        "tier": tier,
                        "repo": repo,
                        "branch": branch,
                        "path": pat["path"].format(name=name),
                        "file_name": f"{name}.{ext}",
                        "file_stem": name,
                        "ext": ext,
                        "client": normalize_client(pat.get("client", "")),
                        "size": 0,
                        "url": url,
                        "resolve_variant": False,
                        "source_type": "url_list",
                        "format": pat.get("format"),
                    }
                )

    elif mode == "manual":
        for rec in scan.get("records", []):
            out.append(
                {
                    "provider": provider,
                    "tier": tier,
                    "repo": repo,
                    "branch": scan.get("branch", "main"),
                    "path": "",
                    "file_name": rec.get("name", ""),
                    "file_stem": rec.get("id", ""),
                    "ext": "",
                    "client": normalize_client(rec.get("client", "")),
                    "size": 0,
                    "url": rec["url"],
                    "resolve_variant": False,
                    "source_type": "manual",
                    "record_id": rec.get("id"),
                    "service_id": rec.get("service_id"),
                    "name_override": rec.get("name"),
                }
            )

    elif mode == "release_assets":
        release_repo = scan.get("release_repo", repo)
        rel = client.latest_release(release_repo)
        if not rel:
            print(f"  [discover] no release for {release_repo}")
            return out
        for asset_name in scan.get("release_assets", []):
            url = rel.get("assets_urls", {}).get(asset_name)
            if not url:
                continue
            for ckey in scan.get("clients", {}):
                out.append(
                    {
                        "provider": provider,
                        "tier": tier,
                        "repo": release_repo,
                        "branch": "release",
                        "path": f"release/{asset_name}",
                        "file_name": asset_name,
                        "file_stem": Path(asset_name).stem,
                        "ext": asset_name.rsplit(".", 1)[-1].lower() if "." in asset_name else "",
                        "client": normalize_client(ckey),
                        "size": 0,
                        "url": url,
                        "resolve_variant": False,
                        "source_type": "release",
                        "tag_name": rel.get("tag_name"),
                        "published_at": rel.get("published_at"),
                    }
                )

    else:
        print(f"  [discover] unknown scan mode {mode!r} for {provider}")

    return out


def main() -> None:
    cfg = load_config()
    client = GitHub()
    cands = discover(client, cfg)
    print(f"candidates: {len(cands)}")
    by_provider = Counter(c["provider"] for c in cands)
    for p, n in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {p}: {n}")


if __name__ == "__main__":
    main()
