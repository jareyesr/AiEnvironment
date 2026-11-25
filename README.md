# AiEnvironment

Reusable AI infrastructure for C++ development, debug ownership, and HSD request handling.

## Structure

| Folder | Purpose |
|--------|---------|
| `McpServers/` | Reusable MCP servers (see below). |
| `Agents/` | Custom agent personas/modes (`*.agent.md`, see below). |
| `prompts/` | On-demand prompt files (`*.prompt.md`). |
| `.github/` | Always-on Copilot instructions. |

## MCP servers

| Server | Purpose |
|--------|---------|
| `HsdServer_SupportQuery` | Runs curated HSD saved support queries and reconstructs article comments/status/reason from the history endpoint (more reliable than the base `hsd` server for comments and the `reason` field). |
| `IntelWikiAccess` | Read-only access to the Intel Confluence wiki (`wiki.ith.intel.com`): read pages, CQL search, and release-page statistics (bug fixes / enhancements per release). |

### IntelWikiAccess

Server file: `McpServers/IntelWikiAccess/intel_wiki_access.py`

**Tools**

| Tool | Purpose |
|------|---------|
| `wiki_get_page(page, max_chars)` | Reads a page as plain text. Accepts a page ID or any wiki page URL. |
| `wiki_search(cql, max_results)` | Runs a CQL search; returns ID, title and created date per result. |
| `wiki_release_stats(product, space, months, sections, include_per_release)` | Min/max/avg/total of Bug Fixes, Enhancements and Combined items across `<product> 1.<yy>.<WW><d> Release` pages (e.g. `1.26.476` = 2026 WW47 day 6). Errata/Notes are excluded. Use `sections="Galaxy"` for Galaxy-only counts. |

**Requirements**

1. **Wiki token in `CONFLUENCE_PAT`**: create a Confluence Personal Access Token
   (wiki > profile picture > Settings > Personal Access Tokens) and store it as a Windows
   *user* environment variable named exactly `CONFLUENCE_PAT`. Type this in your own terminal,
   never in chat:
   ```powershell
   [Environment]::SetEnvironmentVariable("CONFLUENCE_PAT", "<token>", "User")
   ```
   If VS Code started before the variable existed, the server reads it from the Windows user
   environment (`HKCU\Environment`) directly. Renew the token before it expires.
2. **Python 3.10 or newer** (the code uses `str | None` type syntax). Currently runs on the
   hsdAgent venv (Python 3.13).
3. **`mcp` Python package** (FastMCP, version 1.0 or newer; tested with 1.12.0):
   `pip install "mcp>=1.0.0"`. Everything else is from the Python standard library.
4. **Intel network or VPN** to reach `wiki.ith.intel.com`.
5. **Wiki permissions**: the server only sees pages your wiki account can see. All calls are read-only.
6. **Optional `WIKI_BASE_URL`**: defaults to `https://wiki.ith.intel.com`. The token is only sent to this host.

**Registration** (user-scope `mcp.json`):
```json
"IntelWikiAccess": {
  "type": "stdio",
  "command": "<path-to-python-with-mcp>/python.exe",
  "args": ["C:/AiEnvironment/McpServers/IntelWikiAccess/intel_wiki_access.py"],
  "env": { "WIKI_BASE_URL": "https://wiki.ith.intel.com" }
}
```
To use it from an agent, add `IntelWikiAccess` to the agent's `tools:` frontmatter.

## Agents

| Agent | Purpose |
|-------|---------|
| `hsd-analyst` | HSD Support-Query Analyst — investigates HSD tickets/articles, runs saved support queries, and produces grounded root-cause analysis and triage for debug work. |
| `hsd-debugger` | HSD Debugger — investigates Galaxy log files (`GalaxyLog.log` / `GalaxyLogServerListen.log`) and crash dumps, correlates them with C++ source, and produces grounded debug findings. Read-only: no code edits. Asks which Galaxy repo to use before code traceback. |

Agents and MCP servers are published to the user workspace (VS Code `prompts/` folder and
user-scope `mcp.json`) so they are available across all workspaces.

Per-issue debug sessions live outside this repo under `C:\HSDs\<HSD-ID>_<slug>`.
