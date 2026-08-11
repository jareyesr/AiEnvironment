#!/usr/bin/env python3
"""
HsdServer_SupportQuery MCP Server (companion / extension server)

An MCP (Model Context Protocol) server that EXTENDS the capabilities of the
external HSD MCP server without modifying it. It adds tooling to work with
HSD-ES *saved queries* (by queryId) -- something the base server does not
expose -- while reusing the base server's authentication and HTTP plumbing.

Authorship:
  - Author of THIS folder (HsdServer_SupportQuery/): jareyesr
  - NOT authored by jareyesr: everything under the external dependency
    directory (see EXTERNAL_HSD_DIR below). Those modules
    (hsd_searcher.py, hsd_reader.py, hsdAgentPy/, etc.) are owned and
    maintained by their original authors. This server only IMPORTS and
    REUSES them; it does not modify them.

Design (Option A -- reuse external helper classes):
  This server imports `HsdSearcher` from the external HSD agent directory to
  inherit its Kerberos/NTLM/bearer authentication and request handling. It
  then issues the saved-query REST calls using that same authenticated
  session, so no auth logic is re-implemented here.

Configuration via environment variables:
  - EXTERNAL_HSD_DIR : Absolute path to the external HSD agent directory that
                       contains hsd_searcher.py. Defaults to the known Intel
                       AI agents location for this machine.
  - HSD_BASE_URL     : Base URL for the HSD instance.
                       Default: https://hsdes.intel.com
  - HSD_AUTH_METHOD  : 'kerberos', 'ntlm', or 'bearer'. Default: 'kerberos'
  - HSD_PAT          : Personal Access Token (only for HSD_AUTH_METHOD='bearer')
  - VERIFY_SSL       : 'true' / 'false'. Default: 'false'

Usage:
  python hsd_server_support_query.py
"""

import os
import sys
import json
import re
import html
from collections import Counter
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "Error: mcp package not installed. Install with: pip install mcp",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import requests
except ImportError:
    print(
        "Error: requests package not installed. Install with: pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)


# ============================================================================
# External dependency wiring (Option A)
#
# We reuse the external HSD helper classes for authentication + HTTP. This
# folder does NOT own that code. If the external path changes, override it via
# the EXTERNAL_HSD_DIR environment variable.
# ============================================================================
DEFAULT_EXTERNAL_HSD_DIR = (
    r"C:\Users\jareyesr\.copilot"
    r"\applications.manufacturing.intel.ai.agents\hsdAgent"
)
EXTERNAL_HSD_DIR = os.getenv("EXTERNAL_HSD_DIR", DEFAULT_EXTERNAL_HSD_DIR)

if EXTERNAL_HSD_DIR not in sys.path:
    sys.path.insert(0, EXTERNAL_HSD_DIR)

try:
    # NOTE: hsd_searcher.py is external, third-party code (not authored here).
    from hsd_searcher import HsdSearcher
except ImportError as exc:
    print(
        "Error: could not import 'HsdSearcher' from the external HSD agent "
        f"directory:\n    {EXTERNAL_HSD_DIR}\n"
        f"Import error: {exc}\n"
        "Set EXTERNAL_HSD_DIR to the folder that contains hsd_searcher.py.",
        file=sys.stderr,
    )
    sys.exit(1)


# ============================================================================
# Configuration
# ============================================================================
HSD_BASE_URL = os.getenv("HSD_BASE_URL", "https://hsdes.intel.com")
HSD_AUTH_METHOD = os.getenv("HSD_AUTH_METHOD", "kerberos")  # kerberos, ntlm, bearer
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() == "true"


mcp = FastMCP("HsdServer_SupportQuery")


def _create_searcher() -> HsdSearcher:
    """Create an external HsdSearcher to reuse its authentication + headers."""
    pat = os.getenv("HSD_PAT") if HSD_AUTH_METHOD == "bearer" else None
    if HSD_AUTH_METHOD == "bearer" and not pat:
        # Reuse the external secrets loader as a fallback.
        pat = HsdSearcher.load_pat_from_secrets()
    return HsdSearcher(pat, base_url=HSD_BASE_URL, auth_method=HSD_AUTH_METHOD)


def _saved_query_url(query_id: str) -> str:
    """Build the HSD-ES saved-query execution URL.

    This is the GET counterpart to the base server's POST /rest/query used for
    ad-hoc HQL. Executing a saved query returns its member records.
    """
    return f"{HSD_BASE_URL}/rest/query/{query_id}"


def _article_history_url(article_id: str) -> str:
    """Build the HSD-ES article history URL.

    GET on this endpoint returns the full revision changelog for a single
    article. Each revision record carries an accumulated ``comments`` field,
    so the comment thread is reconstructed from history rather than from the
    dedicated ``/comments`` endpoint (which is unreliable and frequently
    returns an error for these articles).
    """
    return f"{HSD_BASE_URL}/rest/article/{article_id}/history"


def _html_to_text(raw: str) -> str:
    """Convert an HSD comment HTML fragment to readable plain text."""
    if not raw:
        return ""
    text = re.sub(r"<br ?/?>", "\n", raw)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse excess blank lines / trailing whitespace.
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


def _parse_comment_thread(comments_html: str) -> list:
    """Parse the accumulated ``comments`` HTML into structured comments.

    HSD stores the whole comment thread in a single field where each comment
    is introduced by a marker of the form ``++++<commentId> <author>`` followed
    by the comment body. This splits on that marker and returns an ordered list
    (oldest first) of {comment_id, author, text} dicts.
    """
    if not comments_html:
        return []

    marker = re.compile(r"\+\+\+\+(\d+)\s+(\S+)")
    matches = list(marker.finditer(comments_html))
    if not matches:
        # No markers -- return the whole field as a single comment.
        body = _html_to_text(comments_html)
        return [{"comment_id": None, "author": None, "text": body}] if body else []

    parsed = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(comments_html)
        parsed.append(
            {
                "comment_id": match.group(1),
                "author": match.group(2),
                "text": _html_to_text(comments_html[start:end]),
            }
        )
    return parsed


# Fields requested from / surfaced for each saved-query member record.
#
# This set mirrors the columns exposed by the working saved query
#   https://hsdes.intel.com/appstore/generalapps/#/pages/community/14025907139?queryId=14025907745
# (discovered from the query's REST response). Adjust here if the working
# query changes and exposes a different column set.
SAVED_QUERY_FIELDS = [
    "id",
    "title",
    "subject",
    "status",
    "status_reason",
    "reason",
    "owner",
    "submitted_by",
    "updated_by",
    "priority",
    "component",
    "program",
    "release",
    "issue_type",
    "record_type",
    "service_sub_type",
    "tenant",
    "tag",
    "open_date",
    "open_date_ww",
    "closed_date",
    "closed_date_ww",
    "submitted_date",
    "updated_date",
    "intended_sprint_eta",
    "percent_done",
    "what_is_the_value",
    "parent_id",
    "hierarchy_id",
    "hierarchy_path",
    "is_current",
    "rev",
    "row_num",
]


def _format_row(item: dict) -> dict:
    """Extract the surfaced attributes from a raw saved-query record.

    Values are passed through for every field in SAVED_QUERY_FIELDS, plus a
    convenience 'url' to the article.
    """
    formatted = {field: item.get(field, "") for field in SAVED_QUERY_FIELDS}
    formatted["url"] = f"{HSD_BASE_URL}/appstore/article-one/#/{item.get('id')}"
    return formatted


def _fetch_saved_query_rows(query_id: str, max_results: int) -> tuple:
    """Fetch raw member rows for a saved query.

    Returns (rows, total_count, error_json). If error_json is not None the
    request failed and it should be returned to the caller as-is.
    """
    searcher = _create_searcher()
    url = _saved_query_url(query_id)

    try:
        response = requests.get(
            url,
            headers=searcher.headers,
            auth=searcher.auth,
            params={
                "start_at": 1,
                "max_results": max_results,
                "fields": ",".join(SAVED_QUERY_FIELDS),
            },
            timeout=60,
            verify=VERIFY_SSL,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = (
            exc.response.text[:300]
            if (exc.response is not None and exc.response.text)
            else "No details"
        )
        return None, None, json.dumps(
            {
                "error": f"Saved query request failed (HTTP {status}) for "
                f"queryId={query_id}",
                "detail": detail,
                "url": url,
            },
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        return None, None, json.dumps(
            {"error": f"Request error for queryId={query_id}: {exc}", "url": url},
            indent=2,
        )

    rows = data.get("data", data.get("results", []))
    total = data.get("total", len(rows))
    return rows, total, None


def _fetch_article_comments(article_id: str) -> tuple:
    """Fetch the comment thread for a single article via its history.

    The dedicated ``/comments`` endpoint is unreliable for these articles, so
    comments are reconstructed from the article history: the highest revision
    that carries a non-empty ``comments`` field holds the full accumulated
    thread, which is then parsed into individual comments.

    Returns (comments, error_json). If error_json is not None the request
    failed and it should be returned to the caller as-is.
    """
    searcher = _create_searcher()
    url = _article_history_url(article_id)

    try:
        response = requests.get(
            url,
            headers=searcher.headers,
            auth=searcher.auth,
            timeout=60,
            verify=VERIFY_SSL,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = (
            exc.response.text[:300]
            if (exc.response is not None and exc.response.text)
            else "No details"
        )
        return None, json.dumps(
            {
                "error": f"History request failed (HTTP {status}) for "
                f"article_id={article_id}",
                "detail": detail,
                "url": url,
            },
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        return None, json.dumps(
            {"error": f"Request error for article_id={article_id}: {exc}", "url": url},
            indent=2,
        )

    revisions = data.get("data", data.get("results", []))
    if isinstance(revisions, dict):
        revisions = [revisions]

    # Pick the revision with the most complete comment thread. HSD accumulates
    # the whole thread into each revision's ``comments`` field, so the longest
    # (typically highest-rev) non-empty value holds every comment.
    best_html = ""
    for rev in revisions:
        candidate = rev.get("comments") or ""
        if len(candidate) > len(best_html):
            best_html = candidate

    return _parse_comment_thread(best_html), None


# ============================================================================
# Tools
# ============================================================================
@mcp.tool()
def hsd_run_saved_query(query_id: str, max_results: int = 200) -> str:
    """
    Execute an HSD-ES saved query by its queryId and return the member articles.

    This complements the base HSD server (which can only run ad-hoc HQL) by
    resolving a saved query / community query URL of the form:
        https://hsdes.intel.com/appstore/.../#/pages/community/<id>?queryId=<queryId>

    Args:
        query_id: The saved query's queryId (e.g., '14028310220').
        max_results: Maximum number of member records to return (default: 200).

    Returns:
        JSON string with: query_id, total_count, returned_count, and results
        (id, subject, status, severity, owner, product, component,
        submitted_by, submit_date, last_update, url). On failure, a JSON
        object with an 'error' field.
    """
    rows, total, error = _fetch_saved_query_rows(query_id, max_results)
    if error is not None:
        return error

    formatted = {
        "query_id": query_id,
        "total_count": total,
        "returned_count": len(rows),
        "results": [_format_row(item) for item in rows],
    }
    return json.dumps(formatted, indent=2, default=str)


@mcp.tool()
def hsd_count_saved_query(query_id: str) -> str:
    """
    Return only the number of HSD articles contained in a saved query.

    A lightweight wrapper over hsd_run_saved_query for when only the count is
    needed (e.g., "how many tickets are under queryId 14028310220?").

    Args:
        query_id: The saved query's queryId.

    Returns:
        JSON string with query_id and total_count, or an 'error' field.
    """
    raw = hsd_run_saved_query(query_id, max_results=1)
    parsed = json.loads(raw)
    if "error" in parsed:
        return raw
    return json.dumps(
        {"query_id": query_id, "total_count": parsed.get("total_count", 0)},
        indent=2,
    )


@mcp.tool()
def hsd_saved_query_field_stats(
    query_id: str,
    field: str = "submitted_by",
    top: int = 10,
    max_results: int = 5000,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
) -> str:
    """
    Tally the values of a single field across a saved query's member articles.

    Useful for questions like "who is the main submitted_by?" or "which status
    is most common?" without shipping every row back to the caller. An optional
    filter narrows the scan to records where filter_field matches filter_value
    (e.g., only release 'Solar'), enabling questions like "who is the main
    owner for release Solar?".

    Args:
        query_id: The saved query's queryId.
        field: The field to tally (default: 'submitted_by'). Must be one of
            the surfaced fields (e.g., submitted_by, owner, status, priority,
            release, tenant, component).
        top: Number of top values to return (default: 10).
        max_results: Maximum member records to scan (default: 5000).
        filter_field: Optional field to filter on before tallying (e.g.,
            'release'). Requires filter_value.
        filter_value: Optional value that filter_field must match. Matching is
            case-insensitive and substring-based (e.g., 'solar' matches
            'Solar').

    Returns:
        JSON string with query_id, field, applied filter, total_count,
        scanned_count (records after filtering), distinct_count, and a ranked
        'top_values' list of {value, count}. On failure, an 'error'.
    """
    rows, total, error = _fetch_saved_query_rows(query_id, max_results)
    if error is not None:
        return error

    formatted_rows = [_format_row(item) for item in rows]

    applied_filter = None
    if filter_field and filter_value is not None:
        needle = str(filter_value).lower()
        formatted_rows = [
            r
            for r in formatted_rows
            if needle in str(r.get(filter_field, "")).lower()
        ]
        applied_filter = {"field": filter_field, "value": filter_value}

    counter = Counter((r.get(field) or "(empty)") for r in formatted_rows)
    ranked = counter.most_common(top)
    return json.dumps(
        {
            "query_id": query_id,
            "field": field,
            "filter": applied_filter,
            "total_count": total,
            "scanned_count": len(formatted_rows),
            "distinct_count": len(counter),
            "top_values": [{"value": v, "count": c} for v, c in ranked],
        },
        indent=2,
        default=str,
    )


@mcp.tool()
def hsd_get_article_comments(article_id: str, max_results: int = 0) -> str:
    """
    Retrieve the comment thread for a single HSD-ES article.

    Complements the saved-query tools by exposing the comments attached to an
    individual HSD article. Comments are always reconstructed from the article
    history (GET /rest/article/<id>/history), because the dedicated
    /rest/article/<id>/comments endpoint is unreliable and frequently fails for
    these articles. The highest revision carries the full accumulated thread,
    which is parsed into individual {comment_id, author, text} entries.

    Args:
        article_id: The HSD article ID (e.g., '14028310220'). A full article
            URL is also accepted; the trailing numeric ID is extracted.
        max_results: Maximum number of comments to return, ordered oldest
            first. Use 0 (default) to return all comments.

    Returns:
        JSON string with article_id, comment_count, returned_count, url, and a
        'comments' list (oldest first). Use the last element for the most
        recent comment. On failure, a JSON object with an 'error' field.
    """
    # Accept a full article URL and extract the trailing numeric id.
    cleaned = str(article_id).strip().rstrip("/")
    if "/" in cleaned:
        cleaned = cleaned.split("/")[-1]

    comments, error = _fetch_article_comments(cleaned)
    if error is not None:
        return error

    total = len(comments)
    if max_results and max_results > 0:
        comments = comments[:max_results]

    return json.dumps(
        {
            "article_id": cleaned,
            "comment_count": total,
            "returned_count": len(comments),
            "url": f"{HSD_BASE_URL}/appstore/article-one/#/{cleaned}",
            "comments": comments,
        },
        indent=2,
        default=str,
    )


if __name__ == "__main__":
    mcp.run()
