# HsdServer_SupportQuery MCP Server

A companion **MCP (Model Context Protocol) server** that extends the existing
external HSD MCP server with support for **HSD-ES saved queries** (by
`queryId`) — a capability the base server does not provide.

It follows **Option A**: instead of re-implementing authentication and HTTP
handling, it **imports and reuses** the external `HsdSearcher` class, inheriting
its Kerberos / NTLM / bearer auth for free.

---

## Authorship & ownership

- **Author of this folder (`HsdServer_SupportQuery/`):** jareyesr
- **Not authored by jareyesr:** all code outside this folder — in particular
  the external HSD agent directory and its modules
  (`hsd_searcher.py`, `hsd_reader.py`, `hsdAgentPy/`, etc.). That code is owned
  and maintained by its original authors. This server only **imports and reuses**
  it and **does not modify** it in any way.

---

## Dependency on external repos / folders

This server has a **hard runtime dependency** on the external HSD agent
directory, which is **not part of this folder** and is **not authored here**:

```
C:\Users\jareyesr\.copilot\applications.manufacturing.intel.ai.agents\hsdAgent
```

Specifically it imports:

| External file | Location | Used for | Authored here? |
|---------------|----------|----------|----------------|
| `hsd_searcher.py` (`HsdSearcher`) | external `hsdAgent/` dir | Auth (`.auth`, `.headers`), base URL, secrets loading | ❌ No |

If that external directory moves, override its location with the
`EXTERNAL_HSD_DIR` environment variable (see Configuration). The server will
fail fast with a clear message if `HsdSearcher` cannot be imported.

> Note: The external directory is read-only from this project's perspective.
> This server never writes to or edits any file outside `HsdServer_SupportQuery/`.

---

## What it adds

| Tool | Purpose |
|------|---------|
| `hsd_run_saved_query(query_id, max_results=200)` | Executes an HSD-ES saved query by `queryId` and returns member articles (`id`, `subject`, `status`, `owner`, `url`) plus counts. |
| `hsd_count_saved_query(query_id)` | Returns only the number of articles in a saved query. |
| `hsd_saved_query_field_stats(query_id, field='submitted_by', top=10, ...)` | Tallies the values of a single field across a saved query's members, with an optional filter. |
| `hsd_get_article_comments(article_id, max_results=0)` | Retrieves the comment thread for a single HSD article (`GET /rest/article/<id>/comments`). Accepts an article ID or full article URL. |

These resolve community/saved query URLs like:

```
https://hsdes.intel.com/appstore/generalapps/#/pages/community/<id>?queryId=<queryId>
```

---

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `EXTERNAL_HSD_DIR` | `C:\Users\jareyesr\.copilot\applications.manufacturing.intel.ai.agents\hsdAgent` | Path to the external dir containing `hsd_searcher.py`. |
| `HSD_BASE_URL` | `https://hsdes.intel.com` | HSD instance base URL. |
| `HSD_AUTH_METHOD` | `kerberos` | `kerberos`, `ntlm`, or `bearer`. |
| `HSD_PAT` | _(none)_ | Personal Access Token, only for `bearer` auth. |
| `VERIFY_SSL` | `false` | `true`/`false` SSL cert verification. |

---

## Requirements

See [requirements.txt](requirements.txt). Note that `requests-kerberos` /
`requests-ntlm` are pulled in transitively by the reused external `HsdSearcher`
depending on the selected auth method; they are listed here to mirror the
external server's runtime needs.

```
pip install -r requirements.txt
```

---

## Running

```
python hsd_server_support_query.py
```

### Register in MCP config

Add alongside the existing HSD server (e.g. in `.vscode/mcp.json` or user
`mcp.json`):

```jsonc
{
  "servers": {
    "HsdServer_SupportQuery": {
      "command": "python",
      "args": ["C:\\TestingPath\\McpServers\\HsdServer_SupportQuery\\hsd_server_support_query.py"],
      "env": {
        "HSD_AUTH_METHOD": "kerberos"
      }
    }
  }
}
```

Both servers' tools then become available together.

---

## Troubleshooting

### Agent keeps dropping the `HsdServer_SupportQuery` tools

**Symptom:** When using the `hsd-analyst` agent, the
`HsdServer_SupportQuery/*` tools (e.g. `hsd_get_article_comments`) disappear
from the agent's `tools:` list, and the comment tool is "not available" in a
chat session even though `mcp.json` is correct.

**Root cause:** VS Code validates each entry in an agent's `tools:` array
against the tools that are **currently registered and reachable**. If the
`HsdServer_SupportQuery` server is **stopped** when the agent file is loaded or
saved, VS Code treats the individually-named tools as unresolved and
**silently strips them** from the frontmatter. The base `hsd/*` tools survive
because that server stays running. A running server's tools are also only bound
to chat sessions that **start after** the server is up.

**Fix that was applied:**

1. **Verified the plumbing was correct** — `mcp.json` server key
   `HsdServer_SupportQuery`, script path, `EXTERNAL_HSD_DIR`, and auth env were
   all valid; the server script defines all 4 tools and uses
   `FastMCP("HsdServer_SupportQuery")`, matching the agent namespace.
2. **Confirmed the server actually starts** — via Command Palette →
   **MCP: List Servers** → **HsdServer_SupportQuery** → **Show Output**. A
   healthy start logs `Discovered 4 tools`. (A crash here is usually the
   `from hsd_searcher import HsdSearcher` import failing — check
   `EXTERNAL_HSD_DIR` and that the venv has `mcp` + `requests`.)
3. **Referenced the whole server instead of individual tools** in the
   `hsd-analyst` agent frontmatter — changed the four
   `hsdserver_supportquery/<tool>` entries to a single `hsdserver_supportquery`
   entry. Referencing the server as a unit stops VS Code from pruning
   individual tool names when the server is momentarily stopped.
4. **Keep the server running** — ensure `HsdServer_SupportQuery` stays
   "Running" (not flapping) so newly-started chat sessions bind its tools.

**Notes:**

- Editing the source file under `C:\AiEnvironment\McpServers\...` is enough —
  the server runs directly from that path, so no copy step is required.
- After editing the agent, republish it to the user prompts folder
  (`%APPDATA%\Code\User\prompts\hsd-analyst.agent.md`).
- Tools only bind to sessions started **after** the server is running; open a
  **new chat** if a tool was added mid-session.

---

## Notes / caveats

- The saved-query REST call is the **GET counterpart** to the base server's
  `POST /rest/query` (used for ad-hoc HQL). If your HSD-ES instance exposes
  saved-query execution under a different path, adjust `_saved_query_url()` in
  [hsd_server_support_query.py](hsd_server_support_query.py) accordingly.
- Article comments are fetched via `GET /rest/article/<id>/comments`. If your
  HSD-ES instance exposes comments under a different path, adjust
  `_article_comments_url()` in
  [hsd_server_support_query.py](hsd_server_support_query.py) accordingly.
- Response field names (`data`/`results`, `total`/`count`) are handled
  defensively to match the shapes the external `HsdSearcher` already tolerates.
