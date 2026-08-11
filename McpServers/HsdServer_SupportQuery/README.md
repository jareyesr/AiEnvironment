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

## Notes / caveats

- The saved-query REST call is the **GET counterpart** to the base server's
  `POST /rest/query` (used for ad-hoc HQL). If your HSD-ES instance exposes
  saved-query execution under a different path, adjust `_saved_query_url()` in
  [hsd_server_support_query.py](hsd_server_support_query.py) accordingly.
- Response field names (`data`/`results`, `total`/`count`) are handled
  defensively to match the shapes the external `HsdSearcher` already tolerates.
