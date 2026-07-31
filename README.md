# roost

`top` for Claude Code. Every live session, the model it is on, how much context
it has burned — and, unlike anything else, **the subagents it spawned**.

One file, no dependencies, Python 3.9+. Runs on macOS, Linux and Windows.

```
  WORKER    MODEL    CTX  IDLE    TASK
NEAR LIMIT
  demo-a1   opus-5   85%  12s     refactor the parser
PARKED + COSTLY
  demo-b2   opus-5   61%  4h10m   audit the build scripts
WORKING NOW
  demo-c3   fable-5  22%  3s      add integration tests
STARTING
  demo-d4   -        -    -

QUIET (4)  demo-e5 . demo-f6 . demo-g7 . demo-h8

8 worker(s)  |  fable-5, opus-5

SUBAGENTS
  STATE    AGENT       MODEL     CTX  IDLE   TASK
  working  a812aca59f  opus-5    33%  2s     survey the config loaders
  idle     adaffaba4b  sonnet-5  67%  1h22m  draft the migration notes

  2 subagent(s), 1 working

INFRA  ollama:11434 up qwen2.5-coder:14b (9.2 GB)   litellm:4000 up   openwebui:8080 DOWN
```

Sessions are grouped by what it costs to ignore them, not by size: `NEAR LIMIT`
is about to stop working, `PARKED + COSTLY` bills its whole context on the next
turn, and everything quiet collapses to a single line.

## Install

```bash
curl -o roost https://raw.githubusercontent.com/OWNER/roost/main/roost.py
chmod +x roost && ./roost
```

Windows: save as `roost.py` and run it — `.PY` is in `PATHEXT`, so `roost.py`
works from anywhere on `PATH`.

## Use

```
roost              live, refreshing every second
roost -w 5         slower refresh
roost -1           one frame, then exit
roost --json       joined records, for piping
```

While running: `space` refresh now · `a` advice panel · `s` subagents panel · `q` quit

## Why subagents are the interesting part

Subagents have **no process of their own** — they run as sidechains inside the
parent's process. Every pid-based view is structurally blind to them.

They do each get a transcript, one directory deeper than the session transcripts:

```
~/.claude/projects/<slug>/<sessionId>/subagents/agent-<id>.jsonl
```

The short task description ("Scout source URLs") lives only in the *parent's*
`toolUseResult`, keyed by `agentId`. roost joins the two, and falls back to the
opening words of the subagent's own first message when the parent's record has
scrolled out of reach.

## Where the numbers come from

Three local, read-only sources. Nothing is sent anywhere; there is no network
call except a localhost probe of the infra ports.

| source | gives |
| --- | --- |
| `~/.claude/sessions/<pid>.json` | live sessions: pid, sessionId, launch cwd, name |
| `~/.claude/projects/*/<sid>.jsonl` | model in use, token usage |
| `127.0.0.1` ports | ollama / litellm / openwebui |

**Context** is the last assistant turn's `input_tokens + cache_read_input_tokens
+ cache_creation_input_tokens`. Cross-checked against an independent tool on the
same session: 77% vs 77.29%.

**The context window is inferred, not recorded.** Nothing on disk states which
window a session opened with, and a session on the 1M window will read 480k+
cache tokens in a single call — scoring that against 200k yields a nonsense
"242%". roost picks the smallest standard tier the usage fits and prints it in
the `WIN` column, so the assumption is visible rather than silent. If a new tier
ships, `WINDOW_TIERS` is the one line to edit.

## Caveats

- It reads an **undocumented on-disk format** that can change without warning.
  That is the whole foundation; treat breakage as expected, not exceptional.
- The window inference above is a heuristic.
- The `ADVICE` panel's thresholds are tuned to one person's usage. Read
  `EXPENSIVE_TOKENS` and friends before trusting the advice.
- Tested on macOS and Windows. Linux should work — same paths — but is untested.

## License

MIT — see [LICENSE](LICENSE). Contact: dev@swamplink.net

Built in a Digital Swamp. From my swamp to yours.
