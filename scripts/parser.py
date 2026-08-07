"""Rule parser: count and classify rules from various ruleset formats.

Handles:
  - Surge-style plain rules   (DOMAIN,example.com / IP-CIDR,1.2.3.4/24,no-resolve)
  - Clash YAML payload:       (payload: - DOMAIN,example.com)
  - Mihomo rule-set JSON      ({version, rules: [{domain: [...]}]})
  - sing-box headless JSON    ({version, rules: [...]})
  - GeoSite YAML              (payload: - +.example.com)
  - MRS / SRS (binary)        -> rule_count from header; types unknown (None)
  - plain domain list         (one domain per line)

Ignores blank lines, comments, metadata headers, and prose.
Returns a rule_count and a rule_types breakdown.
"""
from __future__ import annotations

import json
import re
import struct
from collections import Counter

try:
    import zstandard
except ImportError:
    zstandard = None

# rule-type -> canonical key
RULE_TYPE_KEY = {
    "DOMAIN": "domain",
    "DOMAIN-SUFFIX": "domain_suffix",
    "DOMAIN-KEYWORD": "domain_keyword",
    "DOMAIN-WILDCARD": "domain_wildcard",
    "DOMAIN-REGEX": "domain_regex",
    "IP-CIDR": "ip_cidr",
    "IP-CIDR6": "ip_cidr6",
    "IP-ASN": "ip_asn",
    "GEOIP": "geoip",
    "GEOSITE": "geosite",
    "ASN": "asn",
    "PROCESS-NAME": "process_name",
    "PROCESS-PATH": "process_path",
    "SRC-IP": "src_ip",
    "SRC-PORT": "src_port",
    "DST-PORT": "dst_port",
    "USER-AGENT": "user_agent",
    "URL-REGEX": "url_regex",
    "RULE-SET": "rule_set",
    "IP-ASN": "ip_asn",
    "MATCH": "match",
    "FINAL": "final",
}

IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")
IPV6_RE = re.compile(r"^[0-9a-fA-F:]+(/[0-9]{1,3})?$")


class ParseResult:
    __slots__ = ("rule_count", "types", "is_rule_file", "errors", "format")

    def __init__(self, rule_count=0, types=None, is_rule_file=False, errors=None, format_="unknown"):
        self.rule_count = rule_count
        self.types: Counter = types or Counter()
        self.is_rule_file = is_rule_file
        self.errors = errors or []
        self.format = format_


def _classify_surge_token(token: str) -> str | None:
    """Given 'DOMAIN,example.com' or 'DOMAIN-SUFFIX,x.com,no-resolve', return type key."""
    if "," in token:
        head = token.split(",", 1)[0].strip().upper()
    else:
        head = token.strip().upper()
    return RULE_TYPE_KEY.get(head)


def parse_plain_lines(lines: list[str], base_format: str = "surge") -> ParseResult:
    """Parse a list of text lines in Surge/Clash-style or plain-domain format."""
    result = ParseResult(format_=base_format)
    types: Counter = Counter()
    count = 0
    domain_only = 0
    ip_only = 0
    seen_rule = False
    payload_block = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if base_format in ("clash_yaml", "geosite_yaml") or payload_block:
            # strip leading '- ' for YAML payload items
            if payload_block and line.startswith("- "):
                line = line[2:].strip()
            else:
                if line == "payload:":
                    payload_block = True
                    continue
                if not payload_block:
                    continue
        # metadata headers like 'DOMAIN: 2' or 'TOTAL: 46' in blackmatrix7 files
        if re.match(r"^[A-Z][A-Z0-9-]+:\s*\d+$", line):
            continue
        if re.match(r"^UPDATED:\s", line) or re.match(r"^rule_count:\s", line):
            continue

        upper = line.upper()
        key = _classify_surge_token(line)

        if key:
            seen_rule = True
            count += 1
            types[key] += 1
            if key in ("domain", "domain_suffix", "domain_keyword", "domain_wildcard", "domain_regex"):
                domain_only += 1
            elif key in ("ip_cidr", "ip_cidr6", "geoip", "asn", "ip_asn"):
                ip_only += 1
            continue

        # might be a bare domain (plain domain list / domainset)
        if "." in line and not line.startswith(".") and not line.startswith("-"):
            # not an IP and not 'process' etc.
            if not IPV4_RE.match(line) and not IPV6_RE.match(line):
                lower = line.lower()
                if not any(k in lower for k in ("http", "/", " ", "\t")):
                    seen_rule = True
                    count += 1
                    types["domain"] += 1
                    domain_only += 1
                    continue

    result.rule_count = count
    result.types = types
    result.is_rule_file = seen_rule
    return result


def parse_clash_yaml(text: str) -> ParseResult:
    """Parse Clash rule-set YAML with a `payload:` list of 'TYPE,value' items."""
    lines = text.splitlines()
    return parse_plain_lines(lines, base_format="clash_yaml")


def parse_geosite_yaml(text: str) -> ParseResult:
    """Parse GeoSite YAML (payload: - +.example.com)."""
    lines = text.splitlines()
    return parse_plain_lines(lines, base_format="geosite_yaml")


def parse_rule_set_json(text: str) -> ParseResult:
    """Parse Mihomo rule-set JSON or sing-box headless JSON.

    Mihomo:  {"version":4, "rules":[{"domain":[...]},{"ip_cidr":[...]}]}
    sing-box: {"version":2, "rules":[{"domain_suffix":[...]}, ...]}  (singular keys)
    """
    result = ParseResult(format_="ruleset_json")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        result.errors.append(f"json: {e}")
        return result
    if not isinstance(data, dict) or "rules" not in data:
        return result
    rules = data.get("rules", [])
    types: Counter = Counter()
    count = 0
    key_map = {
        "domain": "domain",
        "domain_suffix": "domain_suffix",
        "domain_keyword": "domain_keyword",
        "domain_regex": "domain_regex",
        "domain_wildcard": "domain_wildcard",
        "ip_cidr": "ip_cidr",
        "ip_cidr6": "ip_cidr6",
        "ip_prefix": "ip_cidr",
        "source_ip_cidr": "src_ip",
        "geoip": "geoip",
        "geosite": "geosite",
        "ip_asn": "ip_asn",
        "asn": "asn",
        "process_name": "process_name",
        "process_path": "process_path",
        "network": "network",
        "port": "dst_port",
        "source_port": "src_port",
    }
    for r in rules:
        if not isinstance(r, dict):
            continue
        for k, vals in r.items():
            key = key_map.get(k.lower())
            if key is None:
                continue
            n = len(vals) if isinstance(vals, list) else 1
            if n:
                count += n
                types[key] += n
    if count:
        result.is_rule_file = True
    result.rule_count = count
    result.types = types
    return result


def parse_singbox_json(text: str) -> ParseResult:
    return parse_rule_set_json(text)


def _maybe_zstd(data: bytes) -> bytes:
    """MRS files are zstd-compressed streams; decompress if we detect the magic."""
    if data[:4] in (b"\x28\xb5\x2f\xfd", b"\x28\xb5\x2f\xfd\x01"):
        if zstandard is not None:
            try:
                return zstandard.ZstdDecompressor().decompress(data)
            except Exception:
                return data
    return data


def _read_mrs_header(data: bytes) -> dict:
    """Parse MRS binary header: 'MRS\x01' + version + count + checksum.

    MRS layout (after zstd decompression):
      magic  MRS\x01 (4 bytes)
      version uint16
      (padding)
      count   uint32  (at offset 12)
    """
    data = _maybe_zstd(data)
    if len(data) < 16 or data[:4] != b"MRS\x01":
        return {}
    version = struct.unpack("<H", data[4:6])[0]
    count = struct.unpack("<I", data[12:16])[0]
    return {"version": version, "rule_count": count, "binary": True}


def parse_binary(data: bytes, ext: str) -> ParseResult:
    result = ParseResult(format_="binary")
    if ext == "mrs":
        hdr = _read_mrs_header(data)
        if hdr.get("rule_count") is not None:
            result.rule_count = hdr["rule_count"]
            result.is_rule_file = True
    elif ext == "srs":
        # sing-box SRS: zstd-compressed protobuf. Decompress; if it looks like a
        # non-empty protobuf stream, treat as valid (exact count unavailable).
        dec = _maybe_zstd(data)
        if len(dec) > 4:
            result.is_rule_file = True
            result.rule_count = None
    return result


def parse_content(text: str | bytes, ext: str, format_hint: str | None = None) -> ParseResult:
    """Dispatch on extension and format hint."""
    if isinstance(text, bytes):
        if ext in ("mrs", "srs"):
            return parse_binary(text, ext)
        try:
            text = text.decode("utf-8", errors="replace")
        except Exception:
            return ParseResult(errors=["undecodable"])

    if format_hint in ("ruleset_json", "singbox_json"):
        return parse_rule_set_json(text)
    if format_hint == "clash_yaml":
        return parse_clash_yaml(text)
    if format_hint == "geosite_yaml":
        return parse_geosite_yaml(text)
    if format_hint == "plain_ruleset":
        return parse_plain_lines(text.splitlines(), base_format="surge")

    # heuristic by extension
    if ext == "json":
        return parse_rule_set_json(text)
    if ext == "yaml" or ext == "yml":
        if "payload:" in text:
            if "geosite" in text[:2000].lower() or text.startswith("payload:"):
                return parse_geosite_yaml(text)
            return parse_clash_yaml(text)
        return parse_plain_lines(text.splitlines(), base_format="surge")
    if ext in ("list", "txt", "conf", "lsr"):
        return parse_plain_lines(text.splitlines(), base_format="surge")
    # unknown text
    return parse_plain_lines(text.splitlines(), base_format="surge")


def summarize_types(types: Counter) -> str:
    """Produce a UI summary string like 'Domain + IPv4'."""
    has_domain = any(k in types for k in ("domain", "domain_suffix", "domain_keyword", "domain_wildcard", "domain_regex"))
    has_ipv4 = types.get("ip_cidr", 0) > 0
    has_ipv6 = types.get("ip_cidr6", 0) > 0
    has_other = any(
        types.get(k, 0) > 0
        for k in ("geoip", "geosite", "asn", "ip_asn", "process_name", "process_path", "match", "final", "src_ip", "dst_port", "src_port", "network", "rule_set")
    )
    if has_domain and has_ipv4 and has_ipv6:
        return "Domain + IPv4 + IPv6"
    if has_domain and has_ipv4:
        return "Domain + IPv4"
    if has_domain and has_ipv6:
        return "Domain + IPv6"
    if has_domain:
        return "Domain"
    if has_ipv4 and has_ipv6:
        return "IPv4 + IPv6"
    if has_ipv4:
        return "IPv4"
    if has_ipv6:
        return "IPv6"
    if has_other:
        return "Other"
    return "Unknown"


if __name__ == "__main__":
    import sys

    text = sys.stdin.read()
    res = parse_content(text, "list")
    print(f"count={res.rule_count} is_rule_file={res.is_rule_file} types={dict(res.types)} summary={summarize_types(res.types)}")
