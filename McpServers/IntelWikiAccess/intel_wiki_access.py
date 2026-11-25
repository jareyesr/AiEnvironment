#!/usr/bin/env python3
"""
IntelWikiAccess MCP Server (author: jareyesr)

Read-only access to the Intel Confluence wiki (wiki.ith.intel.com) using a
Confluence Personal Access Token (PAT).

Tools:
  - wiki_get_page      : read a page (by ID or URL) as plain text
  - wiki_search        : run a CQL search
  - wiki_release_stats : count Bug Fixes / Enhancements / other items across
                         release pages "<product> 1.<yy>.<WW><d> Release"

Configuration via environment variables:
  - CONFLUENCE_PAT : Personal Access Token. If not in the process environment,
                     it is read from the Windows user environment
                     (HKCU\\Environment). The token is never logged or returned.
  - WIKI_BASE_URL  : Default https://wiki.ith.intel.com

Usage:
  python intel_wiki_access.py
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: mcp package not installed. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)


WIKI_BASE_URL = os.getenv("WIKI_BASE_URL", "https://wiki.ith.intel.com").rstrip("/")
WIKI_HOST = urllib.parse.urlparse(WIKI_BASE_URL).hostname

mcp = FastMCP("IntelWikiAccess")


# ============================================================================
# HTTP / auth
# ============================================================================
def _get_token() -> str:
    token = os.environ.get("CONFLUENCE_PAT")
    if token:
        return token
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, "CONFLUENCE_PAT")[0]
        except FileNotFoundError:
            pass
    raise RuntimeError(
        "CONFLUENCE_PAT is not set. Create a Confluence Personal Access Token and store it "
        "as a user environment variable named CONFLUENCE_PAT."
    )


def _api_get(path: str, params: dict | None = None) -> dict:
    # Only relative API paths are accepted, so the token is never sent to another host.
    url = WIKI_BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + _get_token(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        hints = {401: "token invalid or expired", 403: "no permission for this content",
                 404: "content not found"}
        raise RuntimeError(f"Wiki API returned HTTP {e.code} ({hints.get(e.code, e.reason)})") from None


def _search(cql: str, max_results: int = 500) -> list[dict]:
    results, start, page = [], 0, 100
    while len(results) < max_results:
        data = _api_get("/rest/api/content/search", {
            "cql": cql, "limit": page, "start": start, "expand": "history",
        })
        batch = data.get("results", [])
        results.extend(batch)
        if len(batch) < page:
            break
        start += page
    return results[:max_results]


def _get_body(page_id: str) -> dict:
    return _api_get(f"/rest/api/content/{page_id}", {"expand": "body.storage,version,space"})


def _resolve_page_id(page: str) -> str:
    page = page.strip()
    if page.isdigit():
        return page
    parsed = urllib.parse.urlparse(page)
    if parsed.hostname and parsed.hostname != WIKI_HOST:
        raise ValueError(f"Only {WIKI_HOST} URLs are supported")
    m = re.search(r"/pages/(\d+)", parsed.path)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(parsed.query)
    if "pageId" in qs and qs["pageId"][0].isdigit():
        return qs["pageId"][0]
    m = re.search(r"/display/([^/]+)/([^/?#]+)", parsed.path)
    if m:
        space = urllib.parse.unquote(m.group(1))
        title = urllib.parse.unquote_plus(m.group(2))
        data = _api_get("/rest/api/content", {"spaceKey": space, "title": title, "limit": 1})
        if data.get("results"):
            return data["results"][0]["id"]
    raise ValueError("Could not determine page ID from input; pass a numeric ID or a page URL")


# ============================================================================
# HTML helpers
# ============================================================================
def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _storage_to_text(storage: str) -> str:
    t = re.sub(r"<(/p|br ?/?|/tr|/li|/h\d)>", "\n", storage)
    t = re.sub(r"<(td|th)[^>]*>", " | ", t)
    t = re.sub(r"<h(\d)[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _parse_tables(storage: str) -> list[dict]:
    """Return tables with the section heading (h1-h6 or bold paragraph) above them."""
    tables, section = [], None
    pattern = r"<h\d[^>]*>(.*?)</h\d>|<p[^>]*>\s*<strong>(.*?)</strong>\s*</p>|<table.*?</table>"
    for m in re.finditer(pattern, storage, re.S):
        heading = m.group(1) if m.group(1) is not None else m.group(2)
        if heading is not None:
            if _text(heading):
                section = _text(heading)
            continue
        tbl = m.group(0)
        header = re.search(r"<th[^>]*>(.*?)</th>", tbl, re.S)
        rows = [_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tbl, re.S)]
        tables.append({
            "section": section,
            "header": _text(header.group(1)) if header else None,
            "rows": [r for r in rows if r],
        })
    return tables


def _category(header: str | None) -> str | None:
    h = (header or "").strip().lower()
    if h.startswith("bug fix"):
        return "bug"
    if h.startswith("enhancement"):
        return "enh"
    if h in ("", "errata", "notes"):
        return None
    return header.strip()


# ============================================================================
# Tools
# ============================================================================
@mcp.tool()
def wiki_get_page(page: str, max_chars: int = 20000) -> str:
    """Read a wiki page as plain text.

    Args:
        page: Numeric page ID or a wiki.ith.intel.com page URL
              (/spaces/<S>/pages/<id>/..., viewpage.action?pageId=<id>, /display/<S>/<title>).
        max_chars: Truncate the text body to this many characters.
    """
    page_id = _resolve_page_id(page)
    d = _get_body(page_id)
    body = _storage_to_text(d["body"]["storage"]["value"])
    truncated = len(body) > max_chars
    header = (f"Title: {d['title']}\nID: {d['id']} | Space: {d.get('space', {}).get('key', '')}"
              f" | Version: {d.get('version', {}).get('number', '')}\n"
              f"URL: {WIKI_BASE_URL}/pages/viewpage.action?pageId={d['id']}\n\n")
    return header + body[:max_chars] + ("\n\n[truncated]" if truncated else "")


@mcp.tool()
def wiki_search(cql: str, max_results: int = 50) -> str:
    """Search the wiki with CQL. Returns one line per result: id | title | created date.

    Example CQL: space = "Galaxy" AND type = page AND title ~ "Release" AND created >= "2026-05-01"
    """
    results = _search(cql, max_results)
    if not results:
        return "No results."
    lines = [f"{r['id']} | {r['title']} | {r.get('history', {}).get('createdDate', '')[:10]}"
             for r in results]
    return f"{len(lines)} result(s):\n" + "\n".join(lines)


@mcp.tool()
def wiki_release_stats(product: str = "Galaxy", space: str = "Galaxy", months: int = 6,
                       sections: str = "", include_per_release: bool = True) -> str:
    """Min/max/avg of Bug Fixes, Enhancements and combined items per release.

    Release pages must be titled "<product> 1.<yy>.<WW><d> Release" (e.g. 1.26.476 = 2026 WW47 day 6).
    Releases are selected by version number: from the ISO week `months` months ago to today.
    Errata and Notes tables are excluded; any other table category (e.g. Features) is
    reported separately and counted in Combined.

    Args:
        product: Title prefix, e.g. "Galaxy".
        space: Confluence space key.
        months: Look-back window in months.
        sections: Comma-separated page sections to count (e.g. "Galaxy" or
                  "Galaxy,Power Management,Helios"). Empty = all sections.
        include_per_release: Include the per-release breakdown table.
    """
    today = date.today()
    cutoff = today - timedelta(days=round(months * 30.44))
    cut_year, cut_week, _ = cutoff.isocalendar()
    min_key = (cut_year % 100, cut_week * 10)
    wanted = {s.strip().lower() for s in sections.split(",") if s.strip()}

    cql = (f'space = "{space}" AND type = page AND title ~ "{product}" AND title ~ "Release" '
           f'AND created >= "{(cutoff - timedelta(days=30)).isoformat()}"')
    title_re = re.compile(rf"{re.escape(product)} 1\.(\d{{2}})\.(\d+) Release")

    releases = []
    for r in _search(cql):
        m = title_re.fullmatch(r["title"].strip())
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if key < min_key:
            continue
        storage = _get_body(r["id"])["body"]["storage"]["value"]
        releases.append((key, f"1.{m.group(1)}.{m.group(2)}", r["id"], _parse_tables(storage)))
    if not releases:
        return f"No '{product} 1.<yy>.<WW><d> Release' pages found since {cutoff} in space {space}."
    releases.sort()

    rows, other_cats, hsd_seen = [], Counter(), defaultdict(list)
    for _, version, _, tables in releases:
        c = Counter()
        for t in tables:
            cat = _category(t["header"])
            if cat is None or t["section"] is None:
                continue
            if wanted and t["section"].lower() not in wanted:
                continue
            key = cat if cat in ("bug", "enh") else "other"
            c[key] += len(t["rows"])
            if key == "other":
                other_cats[f"{t['section']} / {cat}"] += len(t["rows"])
            for row in t["rows"]:
                for hsd in re.findall(r"\b\d{11}\b", row):
                    hsd_seen[hsd].append(version)
        rows.append((version, c["bug"], c["enh"], c["other"]))

    def stats(idx: int, label: str) -> str:
        vals = [(r[0], r[idx] if idx < 4 else r[1] + r[2] + r[3]) for r in rows]
        lo = min(n for _, n in vals)
        hi = max(n for _, n in vals)
        avg = sum(n for _, n in vals) / len(vals)
        return (f"### {label}\n| Metric | Value | Release(s) |\n|---|---|---|\n"
                f"| Min | {lo} | {', '.join(v for v, n in vals if n == lo)} |\n"
                f"| Max | {hi} | {', '.join(v for v, n in vals if n == hi)} |\n"
                f"| Avg | {avg:.2f} | — |\n"
                f"| Total | {sum(n for _, n in vals)} | — |\n")

    out = [f"{len(rows)} releases ({rows[0][0]} .. {rows[-1][0]}), window since {cutoff} (WW{cut_week}). "
           f"Sections: {sections or 'all'}.\n",
           stats(1, "Bug Fixes"), stats(2, "Enhancements"),
           stats(4, "Combined (Bug Fixes + Enhancements + other categories)")]
    out.append("Other categories: " + (", ".join(f"{k}: {v}" for k, v in other_cats.items()) or "none"))
    repeats = {k: v for k, v in hsd_seen.items() if len(v) > 1}
    out.append(f"HSDs listed in more than one release: {len(repeats)}"
               + ("".join(f"\n- {k}: {', '.join(v)}" for k, v in repeats.items())))
    if include_per_release:
        out.append("\n### Per release\n| Release | Bug Fixes | Enhancements | Other | Combined |\n|---|---|---|---|---|\n"
                   + "\n".join(f"| {v} | {b} | {e} | {o} | {b + e + o} |" for v, b, e, o in rows))
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
