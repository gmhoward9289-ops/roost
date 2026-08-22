# roost `--json` schema (`roost.snapshot.v1`)

`roost --json` prints one JSON object and exits. The REMOTE panel fetches this
same payload over ssh (`ssh <host> roost --json`). Field names below match the
object as emitted; if a test in `TestJsonContract` fails, this file is wrong.

Top-level keys:

| key | type | meaning |
| --- | --- | --- |
| `schema` | string | always `roost.snapshot.v1` |
| `version` | string | roost version that produced the snapshot |
| `workers` | array | live Claude Code sessions and Cursor composers |
| `subagents` | array | Claude sidechains plus Cursor Task composers |
| `infra` | array | ollama / litellm / openwebui probes |
| `usage_caps` | object or null | Anthropic scrape cache; null if never written |
| `local_models` | array | Ollama `/api/tags` merged with `/api/ps` |
| `gateway` | object | LiteLLM liveliness, `config.yaml` aliases, batch runs |

## workers[]

One object per live row. Units: token counts are integers, times are seconds
(floats). Cursor composers set `pid` to JSON `null`.

| field | notes |
| --- | --- |
| `source` | `claude` or `cursor` |
| `name` | session name; Cursor: last-touched branch name, or `cursor/<first-8>` |
| `pid` | OS pid, or `null` for Cursor |
| `session_id` | Claude sessionId or Cursor composerId |
| `cwd` / `project` | working directory and its basename |
| `model` | from the transcript / Task tool_use |
| `ctx_tokens` | last turn `input + cache_read + cache_creation` |
| `ctx_pct` | percent of the inferred (or known) window |
| `ctx_history` | last few context totals, oldest first |
| `window` | label such as `200k`, `1M`, or `~200k` when inferred |
| `task` / `task_src` | title or last prompt (`title` / `prompt` / `-`) |
| `idle_secs` | seconds since last transcript write |
| `age_secs` | seconds since session start, if known |
| `flow` | sparkline sampled while roost runs (spaces under `--json`) |
| `auto_compact` | Claude auto-compact effective setting |

## subagents[]

Distinguished from workers by `agent_id` + `parent_sid` (no `pid`).

| field | notes |
| --- | --- |
| `source` | `claude` or `cursor` |
| `agent_id` | Claude agent id or Cursor composerId |
| `agent_type` | type name once the parent records it; empty while running |
| `parent_sid` | parent sessionId / composerId |
| `task` | description or first prompt |
| `model` | resolved model if known |
| `ctx_tokens` / `ctx_pct` / `window` | same units as workers |
| `idle_secs` | seconds since last write |
| `state` | `working` / `idle` / `orphan` |
| `parent_live` | whether the parent is still on the board |

## infra[]

| field | notes |
| --- | --- |
| `name` | `ollama`, `litellm`, `openwebui` |
| `port` | probed localhost port |
| `up` | boolean |
| `unseen` | true when down, on an unconfigured default port, and never seen up this run — "no such service or wrong port", not an outage |
| `detail` | resident models, `not running`, or `never seen on this port` |

## gateway

| field | notes |
| --- | --- |
| `litellm_up` | liveliness probe only |
| `configured` | aliases from LiteLLM `model_list` (`alias`, `model`, `api_base`, `kind`, `ollama`) |
| `configured_gap` | string when `config.yaml` is missing/unreadable; otherwise null |
| `runs` | batch dirs: `name`, `model`, `done`, `total`, `failed`, `rate_hr`, `eta_secs`, `last_write_secs`, `active` |
| `jobs` | `{inbox, running, done, failed}` or null |
| `last_req_secs` / `req_per_min` | from `proxy.log`, or null |

`configured` is intent from disk, not whether a backing model will answer. Secrets
in `config.yaml` are not copied into this object.

## usage_caps / local_models

`usage_caps` is the scrape-cache document plus `age_secs`, or `null`.
`local_models[]` has `name`, `disk_gb`, `resident`, `vram_gb`, `expires_secs`.
