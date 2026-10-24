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
from datetime import datetime, timezone
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


# Current article fields surfaced alongside the comment thread. These come from
# the latest history revision because the base article endpoint does not
# reliably return ``reason`` (critical closure/lifecycle context).
ARTICLE_META_FIELDS = [
    "id",
    "title",
    "status",
    "reason",
    "owner",
    "updated_by",
    "updated_date",
    "rev",
]


def _extract_current_meta(revisions: list) -> dict:
    """Pick current article fields from the latest (highest-rev) revision.

    HSD history returns one record per revision; the one with the largest
    numeric ``rev`` reflects the current state of the article.
    """
    if not revisions:
        return {}

    def _rev_num(record: dict) -> int:
        try:
            return int(record.get("rev") or 0)
        except (TypeError, ValueError):
            return 0

    latest = max(revisions, key=_rev_num)
    return {field: latest.get(field, "") for field in ARTICLE_META_FIELDS}
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


def _days_since(date_str: str) -> Optional[int]:
    """Whole days between an HSD date string and now (UTC).

    Accepts common HSD formats (ISO 8601 with/without 'T' and timezone).
    Returns None when the value is empty or cannot be parsed.
    """
    if not date_str:
        return None
    raw = str(date_str).strip()
    # Normalize a few common variants before attempting ISO parsing.
    candidate = raw.replace("Z", "+00:00")
    if " " in candidate and "T" not in candidate:
        candidate = candidate.replace(" ", "T", 1)
    parsed = None
    for value in (candidate, raw[:19], raw[:10]):
        try:
            parsed = datetime.fromisoformat(value)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    return max(delta.days, 0)


# In-process cache of saved-query member rows. Keyed by query_id ->
# (fetch_timestamp, max_results, rows, total). A short TTL keeps repeated tool
# calls within one request from re-downloading the whole (often 900+ row)
# member list each time.
_SAVED_QUERY_CACHE: dict = {}
_SAVED_QUERY_CACHE_TTL = float(os.getenv("SAVED_QUERY_CACHE_TTL", "120"))

# Team roster used to decide whether a ticket's last update was made by the
# support team or by a customer. An update by anyone NOT in this set counts as
# a customer update (a signal the ticket is "unattended"). NOTE: "Shetty,
# Nitin" is intentionally excluded — he is on the team but files/updates on
# behalf of customers, so his updates are treated as customer updates.
TEAM_MEMBERS = [
    "Lopez Carrasco, Diana",
    "Sarveswaran, Nilavan",
    "Binder, Andrew",
    "Chen, Guojun",
    "Jumrani, Akash Chetanprakash",
    "Nguyen, Thao",
    "Reyes Ramirez, Jesus",
    "Robertson, Marc",
    "Gonzalez Solalinde, Luis",
    "Valenzuela Gonzalez, Oscar",
    "Ricks, Trenston",
    "Dashora, Mahim",
    "Paige, Dale",
]


def _is_team_member(name: str) -> bool:
    """True when the given updated_by/owner name matches a team member.

    Matching compares the first two tokens (surname + first name) of each
    roster entry against the name, case-insensitively, so a middle initial or
    suffix in either string does not break the match.
    """
    if not name:
        return False
    haystack = str(name).lower()
    for member in TEAM_MEMBERS:
        tokens = [t for t in re.split(r"[,\s]+", member.lower()) if t][:2]
        if tokens and all(tok in haystack for tok in tokens):
            return True
    return False


def _fetch_saved_query_rows(query_id: str, max_results: int) -> tuple:
    """Fetch raw member rows for a saved query, with a short-TTL cache.

    Saved-query member lists are large (hundreds of rows) and several tools
    (filter, field_stats, count, triage) re-read the same list within a single
    request. To avoid re-downloading the whole query per tool call, the raw
    rows are cached in-process keyed by query_id for a short TTL. A cached
    snapshot with an equal-or-larger max_results satisfies smaller requests.

    Returns (rows, total_count, error_json). If error_json is not None the
    request failed and it should be returned to the caller as-is.
    """
    now = datetime.now(timezone.utc).timestamp()
    cached = _SAVED_QUERY_CACHE.get(query_id)
    if cached is not None:
        ts, cached_max, rows, total = cached
        if now - ts <= _SAVED_QUERY_CACHE_TTL and cached_max >= max_results:
            return rows, total, None

    rows, total, error = _fetch_saved_query_rows_uncached(query_id, max_results)
    if error is None:
        _SAVED_QUERY_CACHE[query_id] = (now, max_results, rows, total)
    return rows, total, error


def _fetch_saved_query_rows_uncached(query_id: str, max_results: int) -> tuple:
    """Issue the REST GET that downloads a saved query's member rows.

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
    """Fetch the comment thread and current metadata for a single article.

    The dedicated ``/comments`` endpoint is unreliable for these articles, so
    comments are reconstructed from the article history: the highest revision
    that carries a non-empty ``comments`` field holds the full accumulated
    thread, which is then parsed into individual comments. The same history
    also exposes the current article fields (``status``, ``reason``, etc.);
    the latest revision is surfaced as metadata because the base article
    endpoint does not reliably return ``reason``.

    Returns (comments, meta, error_json). If error_json is not None the request
    failed and it should be returned to the caller as-is. ``meta`` is a dict
    of current article fields taken from the latest revision.
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
        return None, None, json.dumps(
            {
                "error": f"History request failed (HTTP {status}) for "
                f"article_id={article_id}",
                "detail": detail,
                "url": url,
            },
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        return None, None, json.dumps(
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

    meta = _extract_current_meta(revisions)
    return _parse_comment_thread(best_html), meta, None


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
    filter_field2: Optional[str] = None,
    filter_value2: Optional[str] = None,
) -> str:
    """
    Tally the values of a single field across a saved query's member articles.

    Useful for questions like "who is the main submitted_by?" or "which status
    is most common?" without shipping every row back to the caller. Up to two
    optional filters narrow the scan to records where each filter_field matches
    its filter_value (both must match — logical AND), enabling questions like
    "how many reason=new tickets are in release Galaxy?" in a single call.

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
        filter_field2: Optional second filter field, ANDed with the first
            (e.g., 'reason'). Requires filter_value2.
        filter_value2: Optional value that filter_field2 must match (same
            case-insensitive substring matching).

    Returns:
        JSON string with query_id, field, applied filters, total_count,
        scanned_count (records after filtering), distinct_count, and a ranked
        'top_values' list of {value, count}. On failure, an 'error'.
    """
    rows, total, error = _fetch_saved_query_rows(query_id, max_results)
    if error is not None:
        return error

    formatted_rows = [_format_row(item) for item in rows]

    applied_filters = []
    for f_field, f_value in ((filter_field, filter_value),
                             (filter_field2, filter_value2)):
        if f_field and f_value is not None:
            needle = str(f_value).lower()
            formatted_rows = [
                r
                for r in formatted_rows
                if needle in str(r.get(f_field, "")).lower()
            ]
            applied_filters.append({"field": f_field, "value": f_value})
    applied_filter = applied_filters or None

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


# Compact field set surfaced by hsd_filter_saved_query. Kept small so the whole
# result stays parseable inline (no oversized single-line resource file).
COMPACT_FIELDS = [
    "id",
    "title",
    "status",
    "reason",
    "release",
    "component",
    "issue_type",
    "record_type",
    "owner",
    "updated_by",
    "priority",
    "open_date",
    "submitted_date",
    "updated_date",
    "url",
]


@mcp.tool()
def hsd_filter_saved_query(
    query_id: str,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
    filter_field2: Optional[str] = None,
    filter_value2: Optional[str] = None,
    fields: Optional[str] = None,
    sort_by: str = "open_date",
    descending: bool = True,
    limit: int = 50,
    max_results: int = 5000,
) -> str:
    """
    Filter a saved query and return a small, inline-parseable set of rows.

    This is the fast path for triage questions like "list the new / unattended
    Galaxy HSDs with titles and how many days they have been open". It filters
    the saved query by up to two ANDed field=value pairs, sorts (default by
    open_date, newest first), caps the output to a small `limit`, and returns
    only a compact set of fields per row plus computed `days_open` and
    `days_since_submitted`. Because the payload is small and sorted, the caller
    can read titles/dates/reasons/updated_by directly from the result WITHOUT
    running a full dump or fetching articles one ID at a time.

    Prefer this over hsd_run_saved_query when you need actual rows (titles,
    dates) for a filtered subset. Use hsd_run_saved_query only when you truly
    need the full unfiltered member list.

    Args:
        query_id: The saved query's queryId.
        filter_field: First field to filter on (e.g., 'reason'). Requires
            filter_value.
        filter_value: Value filter_field must match (case-insensitive
            substring, e.g. 'new').
        filter_field2: Optional second filter field, ANDed with the first
            (e.g., 'release'). Requires filter_value2.
        filter_value2: Value filter_field2 must match (e.g., 'Galaxy').
        fields: Optional comma-separated list of fields to return per row.
            Defaults to a compact set (id, title, status, reason, release,
            component, owner, updated_by, priority, open_date, submitted_date,
            updated_date, url).
        sort_by: Field to sort rows by (default: 'open_date').
        descending: Sort newest/highest first when True (default: True).
        limit: Maximum rows to return after sorting (default: 50). Keep this
            small so the result stays inline-parseable.
        max_results: Maximum member records to scan server-side (default:
            5000).

    Returns:
        JSON string with query_id, applied filters, total_count,
        matched_count (rows after filtering), returned_count, and a 'results'
        list of compact rows. Each row includes the requested fields plus
        'days_open' (whole days since open_date, or null) and
        'days_since_submitted' (whole days since submitted_date, or null).
        On failure, a JSON object with an 'error' field.
    """
    rows, total, error = _fetch_saved_query_rows(query_id, max_results)
    if error is not None:
        return error

    formatted_rows = [_format_row(item) for item in rows]

    applied_filters = []
    for f_field, f_value in ((filter_field, filter_value),
                             (filter_field2, filter_value2)):
        if f_field and f_value is not None:
            needle = str(f_value).lower()
            formatted_rows = [
                r
                for r in formatted_rows
                if needle in str(r.get(f_field, "")).lower()
            ]
            applied_filters.append({"field": f_field, "value": f_value})

    matched_count = len(formatted_rows)

    # Sort newest-first by default so recently-filed tickets are at the head.
    formatted_rows.sort(key=lambda r: str(r.get(sort_by, "")), reverse=descending)
    if limit and limit > 0:
        formatted_rows = formatted_rows[:limit]

    selected = (
        [f.strip() for f in fields.split(",") if f.strip()]
        if fields
        else COMPACT_FIELDS
    )

    results = []
    for r in formatted_rows:
        row = {field: r.get(field, "") for field in selected}
        row["days_open"] = _days_since(r.get("open_date", ""))
        row["days_since_submitted"] = _days_since(r.get("submitted_date", ""))
        results.append(row)

    return json.dumps(
        {
            "query_id": query_id,
            "filters": applied_filters or None,
            "sort_by": sort_by,
            "descending": descending,
            "total_count": total,
            "matched_count": matched_count,
            "returned_count": len(results),
            "results": results,
        },
        indent=2,
        default=str,
    )


def _classify_category(row: dict) -> str:
    """Map a row's issue_type/record_type to Bug/Enhancement/Consultation/Sighting."""
    blob = " ".join(
        str(row.get(f, "")).lower()
        for f in ("issue_type", "record_type", "service_sub_type")
    )
    if "enhanc" in blob or "feature" in blob:
        return "Enhancement"
    if "consult" in blob or "question" in blob or "how" in blob:
        return "Consultation"
    if "sighting" in blob:
        return "Sighting"
    if "bug" in blob or "defect" in blob:
        return "Bug"
    return "Sighting"


@mcp.tool()
def hsd_triage_new_unattended(
    query_id: str,
    release: Optional[str] = None,
    component: Optional[str] = None,
    limit_per_group: int = 25,
    max_results: int = 5000,
) -> str:
    """
    One-shot triage of a saved query: new/clone, unattended, and category mix.

    This is the fast path for the common "list the new / unattended HSDs"
    request. In a SINGLE server-side pass over the (cached) member list it
    returns everything the analyst needs, so the caller does not have to issue
    several filter/field_stats/count calls and cross-reference the team roster
    by hand:

      * new_or_clone: rows whose reason is 'new' or 'clone' (just-filed).
      * unattended: rows that are new/clone OR whose last update (updated_by)
        was made by a customer (i.e. NOT a team member). Each row carries an
        'unattended_reason' of 'new'/'clone' or 'customer_last_update'.
      * category_breakdown: counts of Bug / Enhancement / Consultation /
        Sighting across the (optionally release/component-scoped) set.

    Every returned row is compact and inline-parseable and includes a
    'category' tag plus 'days_open', 'updated_by', and 'is_team_last_update'.

    The team roster is baked into the server (env-independent); 'Shetty, Nitin'
    is treated as a customer. Because the underlying member list is cached with
    a short TTL, calling this right after other saved-query tools reuses the
    same download.

    Args:
        query_id: The saved query's queryId.
        release: Optional release scope (e.g. 'Galaxy' or 'Solar'); matched
            case-insensitively as a substring of the row's release field.
        component: Optional component scope (e.g. 'tool.Galaxy'); matched
            case-insensitively as a substring of the row's component field.
        limit_per_group: Max rows returned in each of new_or_clone and
            unattended, sorted by open_date newest-first (default: 25).
        max_results: Maximum member records to scan (default: 5000).

    Returns:
        JSON string with query_id, applied scope, total_count, scanned_count,
        counts, category_breakdown, and the 'new_or_clone' and 'unattended'
        row lists. On failure, a JSON object with an 'error' field.
    """
    rows, total, error = _fetch_saved_query_rows(query_id, max_results)
    if error is not None:
        return error

    formatted_rows = [_format_row(item) for item in rows]

    scope = {}
    if release:
        needle = release.lower()
        formatted_rows = [
            r for r in formatted_rows if needle in str(r.get("release", "")).lower()
        ]
        scope["release"] = release
    if component:
        needle = component.lower()
        formatted_rows = [
            r for r in formatted_rows if needle in str(r.get("component", "")).lower()
        ]
        scope["component"] = component

    scanned = len(formatted_rows)

    def _compact(r: dict, unattended_reason: Optional[str] = None) -> dict:
        team_last = _is_team_member(r.get("updated_by", ""))
        out = {
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "category": _classify_category(r),
            "reason": r.get("reason", ""),
            "status": r.get("status", ""),
            "release": r.get("release", ""),
            "component": r.get("component", ""),
            "owner": r.get("owner", ""),
            "updated_by": r.get("updated_by", ""),
            "is_team_last_update": team_last,
            "days_open": _days_since(r.get("open_date", "")),
            "url": r.get("url", ""),
        }
        if unattended_reason:
            out["unattended_reason"] = unattended_reason
        return out

    def _newest(items: list) -> list:
        items.sort(key=lambda r: str(r.get("open_date", "")), reverse=True)
        return items

    new_or_clone_rows = _newest(
        [r for r in formatted_rows if str(r.get("reason", "")).lower() in ("new", "clone")]
    )

    unattended_rows = []
    for r in formatted_rows:
        reason = str(r.get("reason", "")).lower()
        if reason in ("new", "clone"):
            unattended_rows.append((r, reason))
        elif not _is_team_member(r.get("updated_by", "")):
            unattended_rows.append((r, "customer_last_update"))
    unattended_rows.sort(
        key=lambda t: str(t[0].get("open_date", "")), reverse=True
    )

    category_breakdown = Counter(_classify_category(r) for r in formatted_rows)

    return json.dumps(
        {
            "query_id": query_id,
            "scope": scope or None,
            "total_count": total,
            "scanned_count": scanned,
            "counts": {
                "new_or_clone": len(new_or_clone_rows),
                "unattended": len(unattended_rows),
            },
            "category_breakdown": {k: category_breakdown[k] for k in
                                   ("Bug", "Enhancement", "Consultation", "Sighting")},
            "new_or_clone": [_compact(r) for r in new_or_clone_rows[:limit_per_group]],
            "unattended": [
                _compact(r, ur) for r, ur in unattended_rows[:limit_per_group]
            ],
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
        JSON string with article_id, current article metadata (status, reason,
        owner, title, updated_by, updated_date, rev), comment_count,
        returned_count, url, and a 'comments' list (oldest first). The
        'status' and 'reason' fields reflect the article's current state
        (reason carries critical closure/lifecycle context that the base
        article endpoint does not reliably return). Use the last comment for
        the most recent one. On failure, a JSON object with an 'error' field.
    """
    # Accept a full article URL and extract the trailing numeric id.
    cleaned = str(article_id).strip().rstrip("/")
    if "/" in cleaned:
        cleaned = cleaned.split("/")[-1]

    comments, meta, error = _fetch_article_comments(cleaned)
    if error is not None:
        return error

    total = len(comments)
    if max_results and max_results > 0:
        comments = comments[:max_results]

    meta = meta or {}
    return json.dumps(
        {
            "article_id": cleaned,
            "status": meta.get("status", ""),
            "reason": meta.get("reason", ""),
            "owner": meta.get("owner", ""),
            "title": meta.get("title", ""),
            "updated_by": meta.get("updated_by", ""),
            "updated_date": meta.get("updated_date", ""),
            "rev": meta.get("rev", ""),
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
