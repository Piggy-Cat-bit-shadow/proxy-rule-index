"""Availability checker: test whether a rule URL actually works.

Standard mode:
  GET the URL -> must be 200 / redirect-to-200
  content must be non-empty
  content must not be an HTML error page
  content must parse as a rule file (or be a valid binary format)
  rule_count > 0 (for text formats)
  A single transient failure marks 'warning' (failure_count += 1); several
  consecutive failures escalate to 'unavailable' but the record is retained.

Special mode (e.g. KeLee / rule.kelee.one):
  The source requires a special access path. Ordinary HTTP probing anomalies
  (403 / redirects / special pages) are NOT treated as failures. status is
  recorded as 'special' and the record is never hidden or downranked.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import requests

from parser import parse_content, ParseResult


class CheckResult:
    __slots__ = (
        "ok",
        "status",
        "status_code",
        "content_type",
        "rule_count",
        "rule_types",
        "summary",
        "format",
        "errors",
        "last_checked",
        "is_binary",
    )

    def __init__(self, ok=False, status="unavailable", status_code=None, content_type=None,
                 rule_count=None, rule_types=None, summary=None, format_=None, errors=None,
                 last_checked=None, is_binary=False):
        self.ok = ok
        self.status = status
        self.status_code = status_code
        self.content_type = content_type
        self.rule_count = rule_count
        self.rule_types = rule_types
        self.summary = summary
        self.format = format_
        self.errors = errors or []
        self.last_checked = last_checked
        self.is_binary = is_binary


_HTML_RE = re.compile(r"<(!doctype|html|head|body|title|h1|div)\b", re.IGNORECASE)
_ERR_TITLE_RE = re.compile(r"(404|403|forbidden|not found|error|bad gateway)", re.IGNORECASE)


def _looks_like_html(text: str) -> bool:
    sample = text[:2000]
    if "<html" in sample.lower() or "<!doctype" in sample.lower() or "<body" in sample.lower():
        return True
    if _HTML_RE.search(sample):
        return True
    # a 404 page from Cloudflare/Raw often starts with <!DOCTYPE html>
    return False


def _is_html_error_page(text: str) -> bool:
    if not _looks_like_html(text):
        return False
    return bool(_ERR_TITLE_RE.search(text))


BINARY_EXTS = {"mrs", "srs", "mmdb", "dat", "db"}


def check_url(
    url: str,
    ext: str = "list",
    format_hint: str | None = None,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> CheckResult:
    """Probe a rule URL and classify availability."""
    sess = session or requests.Session()
    result = CheckResult()
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 rule-index-checker"})
    except requests.RequestException as e:
        result.errors.append(str(e))
        return result

    result.status_code = r.status_code
    result.content_type = r.headers.get("Content-Type", "")
    if r.status_code >= 400:
        return result  # status stays unavailable

    body = r.content
    if not body:
        result.errors.append("empty body")
        return result

    result.is_binary = ext in BINARY_EXTS

    if result.is_binary:
        pres = parse_content(body, ext)
        if pres.is_rule_file:
            result.ok = True
            result.status = "available"
            result.rule_count = pres.rule_count
            result.rule_types = dict(pres.types) if pres.types else None
            result.summary = "binary"
            result.format = pres.format
        else:
            result.errors.append("binary content did not parse")
        return result

    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        result.errors.append("undecodable")
        return result

    if _is_html_error_page(text):
        result.errors.append("html error page")
        return result
    if _looks_like_html(text) and "text/html" in result.content_type:
        # an HTML page that is not an error page but also not a rule file
        result.errors.append("html page")
        return result

    pres: ParseResult = parse_content(text, ext, format_hint=format_hint)
    if not pres.is_rule_file:
        result.errors.append("no parseable rules")
        result.format = pres.format
        return result

    result.ok = True
    result.status = "available"
    result.rule_count = pres.rule_count if pres.rule_count is not None else None
    result.rule_types = dict(pres.types) if pres.types else None
    result.summary = summarize(pres)
    result.format = pres.format
    return result


def summarize(pres: ParseResult) -> str:
    from parser import summarize_types

    return summarize_types(pres.types)


def special_status(result: CheckResult) -> CheckResult:
    """For special sources: never mark unavailable based on HTTP anomalies."""
    if result.status == "available":
        return result
    result.status = "special"
    result.ok = True
    result.errors = result.errors or []
    if "special access" not in result.errors:
        result.errors.append("special access: HTTP probe not authoritative")
    return result


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False
