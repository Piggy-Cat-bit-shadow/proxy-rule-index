"""GitHub API client and rule-file discovery.

Reads sources.yaml and discovers candidate rule files (repo, branch, path,
client, format, raw URL) via the GitHub REST API. Uses the `gh` CLI when
available (avoids token plumbing) and falls back to unauthenticated requests.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import requests

# map our internal client ids -> the directory names used by each repo
CLIENT_HINT = {
    "surge": "Surge",
    "clash": "Clash",
    "loon": "Loon",
    "shadowrocket": "Shadowrocket",
    "quantumultx": "QuantumultX",
    "adguard": "AdGuard",
    "mihomo": "Mihomo",
    "sing-box": "sing-box",
    "stash": "Stash",
    "surfboard": "Surfboard",
    "egern": "Egern",
}

RAW_BASE = "https://raw.githubusercontent.com"


class GitHub:
    def __init__(self, token: str | None = None, use_gh_cli: bool | None = None):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        # Prefer the HTTP path when a token is available (deterministic in CI;
        # the gh CLI is only used as a convenience fallback on dev machines).
        if use_gh_cli is None:
            use_gh_cli = bool(self.token) is False
        self.use_gh_cli = use_gh_cli and not self.token
        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"
        self.session.headers["User-Agent"] = "rule-index-scanner"

    # ---- low-level ----
    def api(self, path: str, params: dict | None = None, paginate: bool = False) -> list | dict:
        """Call a GitHub REST endpoint. path like 'repos/owner/repo/contents/...'."""
        if self.use_gh_cli and self._gh_ok():
            return self._api_gh_cli(path, params, paginate)
        return self._api_http(path, params, paginate)

    def _gh_ok(self) -> bool:
        if not self.use_gh_cli:
            return False
        if getattr(self, "_gh_checked", False):
            return self._gh_works
        self._gh_checked = True
        try:
            r = subprocess.run(
                ["gh", "--version"], capture_output=True, text=True, timeout=10
            )
            self._gh_works = r.returncode == 0
        except Exception:
            self._gh_works = False
        return self._gh_works

    def _api_gh_cli(self, path: str, params: dict | None, paginate: bool) -> list | dict:
        # gh CLI: query params must be inlined as ?key=value in the path
        # (-f/-F append a JSON body for POST and break GET contents calls).
        if params:
            qs = "&".join(
                f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None
            )
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}{qs}"
        cmd = ["gh", "api", path]
        if paginate:
            cmd.append("--paginate")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            err = r.stderr.strip()
            # gh prints "gh: Not Found (HTTP 404)" on 404
            if "404" in err:
                raise requests.HTTPError(f"404 {path}")
            raise RuntimeError(f"gh api {path} failed: {err}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return []

    def _api_http(self, path: str, params: dict | None, paginate: bool) -> list | dict:
        url = f"https://api.github.com/{path}"
        results: list = []
        while url:
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 403 and "rate limit" in r.text.lower():
                # secondary rate limit — sleep and retry once
                time.sleep(60)
                r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 404:
                raise requests.HTTPError(f"404 {path}")
            if r.status_code >= 400:
                raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:200]}")
            data = r.json()
            if paginate and isinstance(data, list):
                results.extend(data)
                link = r.headers.get("Link", "")
                nxt = None
                for part in link.split(","):
                    if 'rel="next"' in part:
                        nxt = part[part.find("<") + 1: part.find(">")]
                url = nxt
                params = None
            else:
                return data
        return results

    # ---- higher-level ----
    def list_dir(self, repo: str, path: str, ref: str | None = None) -> list[dict]:
        q = f"repos/{repo}/contents/{quote(path)}" if path else f"repos/{repo}/contents"
        params = {"ref": ref} if ref else None
        try:
            return self.api(q, params=params, paginate=True)
        except requests.HTTPError:
            return []

    def tree(self, repo: str, branch: str) -> dict | None:
        """Get the recursive git tree for a branch (more efficient than walking contents API)."""
        try:
            return self.api(f"repos/{repo}/git/trees/{quote(branch)}?recursive=1", paginate=True)
        except Exception:
            return None

    def git_subtree(self, repo: str, branch: str, path: str) -> dict | None:
        """Get the git tree of a nested sub-directory.

        Walks the tree hierarchy down to `path` and returns that subtree.
        Useful when a full recursive tree is truncated (e.g. meta-rules-dat asn/).
        """
        try:
            node = self.api(f"repos/{repo}/git/trees/{quote(branch)}", params={})
            parts = [p for p in path.split("/") if p]
            for p in parts:
                nxt = None
                for e in node.get("tree", []):
                    if e.get("path") == p:
                        nxt = e
                        break
                if nxt is None:
                    return None
                node = self.api(f"repos/{repo}/git/trees/{nxt['sha']}", params={})
            return node
        except Exception:
            return None

    def repo_meta(self, repo: str) -> dict:
        try:
            meta = self.api(f"repos/{repo}")
            if isinstance(meta, dict):
                return {
                    "full_name": meta.get("full_name", repo),
                    "pushed_at": meta.get("pushed_at"),
                    "updated_at": meta.get("updated_at"),
                    "description": meta.get("description"),
                }
        except Exception:
            pass
        return {"full_name": repo}

    def branch_sha(self, repo: str, branch: str) -> str | None:
        """Get the latest commit sha for a branch (source-level fingerprint)."""
        try:
            data = self.api(f"repos/{repo}/branches/{quote(branch)}")
            if isinstance(data, dict):
                return data.get("commit", {}).get("sha")
        except Exception:
            pass
        return None

    def latest_release(self, repo: str) -> dict | None:
        try:
            rel = self.api(f"repos/{repo}/releases/latest")
            if isinstance(rel, dict) and rel.get("assets"):
                return {
                    "tag_name": rel.get("tag_name"),
                    "published_at": rel.get("published_at"),
                    "assets": [a["name"] for a in rel.get("assets", [])],
                    "assets_urls": {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])},
                }
        except Exception:
            pass
        return None

    def file_meta(self, repo: str, path: str, ref: str | None = None) -> dict | None:
        """Get metadata for a single file via contents API (size, sha, updated-ish)."""
        try:
            q = f"repos/{repo}/contents/{quote(path)}"
            params = {"ref": ref} if ref else None
            data = self.api(q, params=params)
            if isinstance(data, dict):
                return {
                    "path": data.get("path"),
                    "size": data.get("size"),
                    "sha": data.get("sha"),
                    "download_url": data.get("download_url"),
                }
        except Exception:
            return None
        return None


def raw_url(repo: str, branch: str, path: str) -> str:
    return f"{RAW_BASE}/{repo}/{branch}/{path}"
