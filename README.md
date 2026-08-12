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

## Agents

| Agent | Purpose |
|-------|---------|
| `hsd-analyst` | HSD Support-Query Analyst — investigates HSD tickets/articles, runs saved support queries, and produces grounded root-cause analysis and triage for debug work. |
| `hsd-debugger` | HSD Debugger — investigates Galaxy log files (`GalaxyLog.log` / `GalaxyLogServerListen.log`) and crash dumps, correlates them with C++ source, and produces grounded debug findings. Read-only: no code edits. Asks which Galaxy repo to use before code traceback. |

Agents and MCP servers are published to the user workspace (VS Code `prompts/` folder and
user-scope `mcp.json`) so they are available across all workspaces.

Per-issue debug sessions live outside this repo under `C:\HSDs\<HSD-ID>_<slug>`.
