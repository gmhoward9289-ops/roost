# Cursor on-disk layout (COOPER recon)

Parent: [#48](https://github.com/gmhoward9289-ops/roost/issues/48) /
[#49](https://github.com/gmhoward9289-ops/roost/issues/49).

Measured on COOPER (Windows) with Cursor running, August 2026. Read-only.
Re-run `python scripts/cursor_recon.py` after a Cursor upgrade.

## Recommendation for roost

**Headers-first, transcripts as fallback.**

| source | use for |
| --- | --- |
| `%APPDATA%/Cursor/User/globalStorage/state.vscdb` → `composerHeaders` | live list, name, `contextUsagePercent`, idle (`lastUpdatedAt`), subagent flag |
| `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` | task text (`<user_query>`), Task tool_use model names |
| `cursorDiskKV` `composerData:{id}` | `promptTokenBreakdown.maxTokens` (=256k measured); richer modelConfig — optional next |
| `~/.cursor/chats/**/store.db` | **absent on COOPER** (0 files) — do not require |
| `ai-tracking/ai-code-tracking.db` | titles/models possible; empty `conversation_summaries` when probed |

Transcript-only collection is wrong for CTX%: **71 live JSONL files had zero `usage` keys**. Models appear only inside `Task` `tool_use.input.model`, not on the parent assistant message.

## Paths

```
~/.cursor/projects/<slug>/agent-transcripts/<composerId>/<composerId>.jsonl
~/.cursor/ai-tracking/ai-code-tracking.db
%APPDATA%/Cursor/User/globalStorage/state.vscdb   (Windows)
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb  (macOS)
~/.config/Cursor/User/globalStorage/state.vscdb   (Linux)
```

`CURSOR_AGENT_HOME` remaps `~/.cursor`. The global DB is **not** under that tree —
override with `ROOST_CURSOR_STATE_DB` for fixtures.

Slug encoding: absolute path with drive letter lowercased and separators turned
into `-` (e.g. `C:\Users\gmhow\dev\roost` → `c-Users-gmhow-dev-roost`).

## Field map (roost worker row)

| roost field | Cursor source |
| --- | --- |
| `session_id` | `composerId` |
| `name` | newest `trackedGitRepos[].branches[].branchName` by `lastInteractionAt`; else `cursor/<first-8-of-id>` |
| `task` | `composerHeaders.value.name`, else transcript `<user_query>`, else `subtitle` |
| `model` | transcript `message.model` (rare); else newest `Task` tool_use `input.model`; else `-` |
| `ctx_pct` | `composerHeaders.value.contextUsagePercent` (Cursor’s own meter) |
| `ctx_tokens` | reverse from pct × known window when headers present; transcript usage if ever present |
| `idle_secs` | now − `lastUpdatedAt` (ms) |
| `pid` | **always None** — composers are not OS processes |
| `cwd` / `project` | `workspaceStorage/<workspaceId>/workspace.json` `folder` URI |
| subagents | `composerHeaders.isSubagent=1` + `subagentInfo.parentComposerId` on the SUBAGENTS panel |

## Freshness

Cheapest “actively working” signal: `composerHeaders.lastUpdatedAt` within
`ROOST_CURSOR_MAX_IDLE_SECS` (default 24h). Transcript mtime is the fallback
when the DB is missing. `WORKING NOW` still uses idle &lt; 60s like Claude.

## Cursor 3.0 note

`composerHeaders` table + `ItemTable['composer.composerHeaders']` are present —
the centralized index migration has landed on COOPER. roost reads the table,
not the ItemTable blob.

## Fixtures to check in

- `tests/fixtures/cursor/sample.jsonl` — synthetic usage + user_query (transcript path)
- `tests/fixtures/cursor/headers.sqlite` — tiny `composerHeaders` DB (headers path)

Scrub rules: no prompts longer than a title, no API keys, no absolute home paths
beyond the slug form.

## Non-goals still

- Holding Cursor credentials
- Writing to `state.vscdb`
- Stopping a composer with `x` (no pid)
