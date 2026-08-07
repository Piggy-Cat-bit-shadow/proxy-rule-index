"""Unit tests for parser, URL resolver, snapshot, and aggregate.

Run:  python3 -m pytest tests/  (or python3 tests/test_unit.py)
No network required — uses local fixtures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parser import parse_content, summarize_types, parse_plain_lines, parse_clash_yaml, parse_rule_set_json
from urlresolver import resolve_url, source_config_hash
from snapshot import Snapshot


# ---------- parser ----------

def test_surge_ruleset_counts():
    text = """# NAME: Telegram
# TOTAL: 5
DOMAIN,api.example.com
DOMAIN-SUFFIX,t.me
IP-CIDR,1.2.3.4/24,no-resolve
IP-CIDR6,2001:db8::/32
PROCESS-NAME,Telegram
"""
    res = parse_content(text, "list")
    assert res.is_rule_file
    assert res.rule_count == 5
    assert res.types["domain"] == 1
    assert res.types["domain_suffix"] == 1
    assert res.types["ip_cidr"] == 1
    assert res.types["ip_cidr6"] == 1
    assert res.types["process_name"] == 1


def test_clash_yaml_payload():
    text = """# NAME: Test
payload:
  - DOMAIN-SUFFIX,example.com
  - IP-CIDR,10.0.0.0/8
  - DOMAIN-KEYWORD,test
"""
    res = parse_clash_yaml(text)
    assert res.rule_count == 3
    assert summarize_types(res.types) == "Domain + IPv4"


def test_ruleset_json():
    text = json.dumps({"version": 4, "rules": [
        {"domain": ["a.com", "b.com"]},
        {"ip_cidr": ["1.2.3.0/24"]},
    ]})
    res = parse_rule_set_json(text)
    assert res.rule_count == 3
    assert res.types["domain"] == 2
    assert res.types["ip_cidr"] == 1


def test_geosite_yaml():
    text = "payload:\n  - +.example.com\n  - +.foo.org\n"
    res = parse_content(text, "yaml")
    assert res.rule_count == 2
    assert res.types["domain"] == 2


def test_empty_file_not_rule():
    res = parse_content("", "list")
    assert not res.is_rule_file
    assert res.rule_count == 0


def test_html_404_not_rule():
    res = parse_content("<!DOCTYPE html><html><body><h1>404 Not Found</h1></body></html>", "list")
    assert not res.is_rule_file


def test_comments_and_blank_ignored():
    text = "# comment\n\n# ANOTHER\nDOMAIN,example.com\n  \n"
    res = parse_plain_lines(text.splitlines(), "surge")
    assert res.rule_count == 1


def test_metadata_header_ignored():
    text = "DOMAIN: 10\nTOTAL: 10\nUPDATED: 2025-01-01\nDOMAIN,example.com\n"
    res = parse_plain_lines(text.splitlines(), "surge")
    assert res.rule_count == 1


# ---------- URL resolver ----------

def test_url_override():
    cand = {"slug": "X", "path": "", "url_override": "https://example.com/custom"}
    source = {"repo": "a/b", "url": {}}
    assert resolve_url(cand, source, {}) == "https://example.com/custom"


def test_repo_url_from_path():
    cand = {"slug": "Telegram", "path": "rule/Surge/Telegram/Telegram.list", "branch": "master"}
    source = {"repo": "blackmatrix7/ios_rule_script", "url": {}}
    url = resolve_url(cand, source, {})
    assert url == "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Surge/Telegram/Telegram.list"


def test_repo_url_fallback_to_discovered():
    cand = {"slug": "X", "path": "", "url": "https://example.com/discovered"}
    source = {"repo": "a/b", "url": {}}
    assert resolve_url(cand, source, {}) == "https://example.com/discovered"


def test_base_url_template():
    cand = {"slug": "Telegram", "path": ""}
    source = {"repo": "a/b", "url": {"mode": "base", "base": "https://rule.kelee.one", "template": "https://rule.kelee.one/Loon/{slug}.lsr"}}
    assert resolve_url(cand, source, {}) == "https://rule.kelee.one/Loon/Telegram.lsr"


def test_base_url_with_path():
    cand = {"slug": "Telegram", "path": "Loon/Telegram.lsr"}
    source = {"repo": "a/b", "url": {"mode": "base", "base": "https://rule.kelee.one"}}
    assert resolve_url(cand, source, {}) == "https://rule.kelee.one/Loon/Telegram.lsr"


def test_config_hash_changes_with_url():
    s1 = {"id": "x", "repo": "a/b", "url": {}}
    s2 = {"id": "x", "repo": "a/b", "url": {"mode": "base", "base": "https://x"}}
    assert source_config_hash(s1) != source_config_hash(s2)


# ---------- snapshot ----------

def test_snapshot_roundtrip(tmp_path):
    snap = Snapshot()
    snap.set_source_sha("blackmatrix7", "abc123", "cfghash", "2026-01-01")
    snap.set_file("blackmatrix7", "blackmatrix7|surge|telegram", {"blob_sha": "bbb", "rule_count": 45, "status": "available"})
    p = tmp_path / "snap.json"
    snap.save(p)
    snap2 = Snapshot.load(p)
    assert snap2.source_sha("blackmatrix7") == "abc123"
    assert snap2.file("blackmatrix7", "blackmatrix7|surge|telegram")["rule_count"] == 45


def test_snapshot_missing_returns_empty(tmp_path):
    snap = Snapshot.load(tmp_path / "none.json")
    assert snap.source_sha("x") is None
    assert snap.source_files("x") == {}


# ---------- aggregate ----------

def test_aggregate_deterministic():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from aggregate import sha256_hex
    d1 = sha256_hex({"a": [1, 2], "b": {"x": "y"}})
    d2 = sha256_hex({"b": {"x": "y"}, "a": [1, 2]})
    assert d1 == d2  # dict order irrelevant


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(tmp_path=None)
            print(f"PASS {fn.__name__}")
        except TypeError:
            # tmp_path not supported in plain runner
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except Exception:
                traceback.print_exc()
                failed += 1
        except Exception:
            traceback.print_exc()
            failed += 1
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
