# roost

[![ci](https://github.com/gmhoward9289-ops/roost/actions/workflows/ci.yml/badge.svg)](https://github.com/gmhoward9289-ops/roost/actions/workflows/ci.yml)

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
pipx install roost
```

Homebrew:

```bash
brew install gmhoward9289-ops/tap/roost
```

Debian and Ubuntu — grab `roost_<version>_all.deb` from the
[latest release](https://github.com/gmhoward9289-ops/roost/releases/latest):

```bash
sudo apt install ./roost_0.2_all.deb
```

There is no PPA and no apt repository; the `.deb` is a release artifact, and
`apt install ./file.deb` resolves `python3` exactly as a repo install would.

Or just take the file — it is one script with no dependencies:

```bash
curl -o roost https://raw.githubusercontent.com/gmhoward9289-ops/roost/main/roost.py
chmod +x roost && ./roost
```

Windows: save as `roost.py` and run it — `.PY` is in `PATHEXT`, so `roost.py`
works from anywhere on `PATH`.

The man page (`man roost`) ships with the Homebrew and `.deb` installs. A `pipx`
install puts it under the venv's own `share/man`, which is not on the default
`MANPATH`; `man "$(pipx environment --value PIPX_LOCAL_VENVS)/roost/share/man/man1/roost.1"`
reads it in place.

## Use

```
roost              live, refreshing every second
roost -w 5         slower refresh
roost -1           one frame, then exit
roost --json       joined records, for piping
```

While running: `space` refresh now · `a` advice panel · `s` subagents panel · `q` quit

## Acting on a session

> **Experimental.** Interactive mode carries an `EXPERIMENTAL` marker in the
> top-right corner, and it means it — `x` ends a real process. Reading the
> dashboard has never been the risky half.

`j`/`k` (or the arrow keys) raise a cursor. Raising it expands the `QUIET`
group, because a session idle for hours is exactly what a sweep is looking for
and it is unreachable while collapsed.

| key | does |
| --- | --- |
| `j` `k` `↓` `↑` | move the cursor |
| `x` | stop the selected session — confirms first, and only `y` proceeds |
| `y` | copy its sessionId, for `claude --resume <id>` |
| `esc` | drop the cursor, re-collapse `QUIET` |

`x` ends a process. It does not compact, save, or otherwise negotiate with the
session — **there is no local control channel into a running Claude Code
session**, so nothing gentler is available from outside it. On Unix that is a
`SIGTERM` and the session exits on its own terms; on Windows there is no
cross-process equivalent, so it is a `TerminateProcess` hard kill. Transcripts
are written a turn at a time, so at most an in-flight turn is lost.

Both keys act on the row object that was on screen when you pressed them, never
on an index re-resolved afterwards. Rows reorder between frames as sessions go
quiet, and an index that outlived its frame would eventually stop the wrong one.

roost refuses to stop its own process or its parent — run it from inside the
session it is pointed at and the cursor can land on the row that owns your
terminal.

Only one panel is open at a time: `a` and `s` flip between ADVICE and SUBAGENTS
rather than stacking. With two dozen sessions on screen a stacked second panel
lands below the bottom of the terminal, which is indistinguishable from the key
not working. For the same reason the frame now says `... N more line(s) below`
instead of quietly truncating.

## What it logs

Every session stopped with `x` appends one JSON line to
`~/.claude/logs/roost.jsonl` — same shape and the same 5000-line cap as the hook
logs beside it:

```json
{"ts":"2026-07-31T00:22:57-0400","action":"stop","ok":true,"host":"COOPER",
 "name":"models-ca","pid":4321,"session_id":"abc-123","model":"claude-opus-5",
 "ctx_tokens":484030,"idle_secs":92500}
```

The session's **task text is deliberately not recorded.** It is free-form prose
out of a transcript, and an audit trail of what was stopped should not become a
copy of what was being worked on.

Because each record carries the context that session was holding, the log
answers afterwards what a sweep actually reclaimed rather than just how many
rows you closed. `--no-log` records nothing. A log that cannot be written is
ignored rather than raised — losing the log is survivable, losing the display
is not.

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
- Daily-driven on macOS and Windows. CI runs the suite and a smoke frame on
  Linux, and it detects live sessions there — but nobody lives on it yet.
  Field reports welcome.

## License

MIT — see [LICENSE](LICENSE). Contact: dev@swamplink.com

Built in a Digital Swamp. From my swamp to yours.
