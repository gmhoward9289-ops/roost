# roost

[![ci](https://github.com/gmhoward9289-ops/roost/actions/workflows/ci.yml/badge.svg)](https://github.com/gmhoward9289-ops/roost/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/roost-top)](https://www.npmjs.com/package/roost-top)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

`top` for Claude Code. Every live session, the model it is on, how much context
it has burned — and, unlike anything else, **the subagents it spawned**.

One file, no dependencies, Python 3.9+. Runs on macOS, Linux and Windows.

![roost watching a fleet: buckets, subagents, the advice panel, and a cancelled stop](demo/roost-demo.gif)

The short ambient loop below is the same program, idling:

![roost's ambient loop, sessions ticking quietly](demo/roost-loop.gif)

```
  WORKER   MODEL    CTX  TOKENS  TREND  FLOW             IDLE   TASK
NEAR LIMIT
  demo-a1  opus-5   85%  170k    +4k       ..:-=+#+=-..  12s    refactor the parser
PARKED + COSTLY
  demo-b2  opus-5   61%  122k    +2k    ..............   4h10m  audit the build scripts
WORKING NOW
  demo-c3  fable-5  22%  44k     +22k       ...:=+*#+    3s     add integration tests
STARTING
  demo-d4  -        -    -       -                       -

QUIET (4)  demo-e5 . demo-f6 . demo-g7 . demo-h8

8 worker(s)  |  1.2M held  |  61k last 8 turns  |  1 near limit  |  fable-5, opus-5

SUBAGENTS
  STATE    AGENT          MODEL     CTX       IDLE   TASK
  working  Explore/a812a  opus-5    66k/200k  2s     survey the config loaders
  idle     adaffaba4b     sonnet-5  484k/1M   1h22m  draft the migration notes

  2 subagent(s), 1 working

INFRA  ollama:11434 up qwen2.5-coder:14b (9.2 GB)   litellm:4000 up   openwebui:8080 DOWN
```

Sessions are grouped by what it costs to ignore them, not by size: `NEAR LIMIT`
is about to stop working, `PARKED + COSTLY` bills its whole context on the next
turn, and everything quiet collapses to a single line.

`TREND` is context added over the session's last few turns. It is an amount and
not a sparkline because context inside a session only ever rises — it drops
solely on `/compact` — so a shape would draw the same ramp on every row. `CTX`
says how full a session is; `TREND` says how fast it is filling, which is how
`demo-c3` above shows as the one to watch at 22%. It is read back out of the
transcript rather than accumulated while roost runs, so it is there on the first
frame and under `--once`. That is also what separates it from `FLOW` alongside
it: `FLOW` sparks throughput sampled since roost started and begins empty,
`TREND` is an amount already in the file when roost opens it.

The last line totals the fleet. Context held is what a sweep would reclaim.

## Install

Homebrew (macOS and Linux):

```bash
brew install gmhoward9289-ops/tap/roost
```

Debian and Ubuntu, from the repo — this also gets you `apt upgrade`:

```bash
curl -fsSL https://gmhoward9289-ops.github.io/roost/roost-archive-keyring.asc | sudo gpg --dearmor -o /usr/share/keyrings/roost-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/roost-archive-keyring.gpg] https://gmhoward9289-ops.github.io/roost stable main" | sudo tee /etc/apt/sources.list.d/roost.list
sudo apt update
sudo apt install roost
```

Or, without adding a repo — grab `roost_<version>_all.deb` from the
[latest release](https://github.com/gmhoward9289-ops/roost/releases/latest)
and install that file directly, e.g.:

```bash
sudo apt install ./roost_<version>_all.deb
```

Both resolve `python3` exactly as a normal repo install would. The repo is
signed with a dedicated key (not tied to any personal identity); its public
half is `packaging/apt/pubkey.asc` in this repo, published unchanged as
`roost-archive-keyring.asc` above.

With pipx:

```bash
pipx install roost-top
```

PyPI holds the bare name `roost` in reserve — a prior project's name, retained
after deletion — so the *package* is `roost-top`; the command it installs is
plain `roost`. Installing straight from the repo skips the index entirely:

```bash
pipx install git+https://github.com/gmhoward9289-ops/roost
```

With npm — the one channel that gives Windows a real `roost` command:

```bash
npm install -g roost-top
```

Or run it without installing: `npx roost-top`. The npm package is a wrapper, not
a port: it ships the same `roost.py` and finds a Python to run it with (`py -3`
first on Windows, `python3` elsewhere, 3.9 or newer either way). Python still has
to be on `PATH` — npm delivers the script and puts `roost` on `PATH`, it does not
bring an interpreter. As on PyPI, the bare name `roost` was already taken, so the
package is `roost-top` and the command is plain `roost`.

Or just take the file. It is one script, stdlib only, no dependencies:

```bash
curl -o roost https://raw.githubusercontent.com/gmhoward9289-ops/roost/main/roost.py
chmod +x roost && ./roost
```

With winget:

```powershell
winget install gmhoward9289-ops.roost
```

That installs a frozen Windows executable — no Python required. It's built and
attached to every tagged release alongside a `roost-<version>-windows-x64.zip`
on the [Releases page](https://github.com/gmhoward9289-ops/roost/releases), if
you'd rather grab the zip directly.

Or, without any of that: save `roost.py` and run it — `.PY` is in `PATHEXT`,
so `roost.py` works from anywhere on `PATH`. That route and the npm one above
both still need a Python interpreter on `PATH`; the winget install is the one
that doesn't, since it ships a frozen exe.

The man page (`man roost`) ships with the Homebrew and `.deb` installs. A pipx
install puts it under the venv's own `share/man`, which is not on the default
`MANPATH`; read it in place with
`man "$(pipx environment --value PIPX_LOCAL_VENVS)/roost-top/share/man/man1/roost.1"`.

## Use

```
roost              live, refreshing every second
roost -w 5         slower refresh
roost -1           one frame, then exit
roost --json       joined records, for piping
```

While running: `space` refresh now · `a` advice panel · `s` subagents panel · `m` local models panel · `u` usage panel · `g` gateway panel · `r` remote panel · `h` or `?` what am I looking at · `i` arm interactive · `q` quit

The `FLOW` column is a sparkline of each session's recent token throughput —
context growth per refresh, normalised to its own busiest moment, newest at the
right. A `.` is a sample with no flow; the ramp `:-=+*#` is increasing activity.
It is ASCII on purpose (block-drawing characters mojibake in the Windows
console) and starts empty: history begins when roost starts, nothing persists.

The subagent `CTX` column reads `48k/200k` — tokens loaded over the window.
The window is inferred (the smallest standard tier the observed usage fits in),
because nothing in a transcript records which window the session was opened
with. The `AGENT` column shows the agent's type (`Explore/a812a`) once the
parent has recorded it — which only happens when the agent finishes, so a
still-running agent shows its hex id.

## Acting on a session

> **Experimental, and off by default.** `i` arms interactive mode — the
> cursor, `x`, `y`, and the `EXPERIMENTAL` marker in the top-right corner all
> come alive together, and it means it: `x` ends a real process. Reading the
> dashboard has never been the risky half; `i` is the one key that draws the
> line. Press it again to disarm — the cursor drops and `x`/`y`/`j`/`k` stop
> responding until you press it once more.

Once armed, `j`/`k` (or the arrow keys) raise a cursor. Raising it expands the
`QUIET` group, because a session idle for hours is exactly what a sweep is
looking for and it is unreachable while collapsed.

| key | does |
| --- | --- |
| `i` | arm or disarm interactive mode |
| `j` `k` `↓` `↑` | move the cursor (interactive mode only) |
| `x` | stop the selected session — confirms first, and only `y` proceeds |
| `y` | copy its sessionId, for `claude --resume <id>` |
| `esc` | drop the cursor, re-collapse `QUIET` |

Start already armed with `roost --interactive` if you know you'll be acting on
a session right away.

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

Only one panel is open at a time: `a`, `s`, `m`, `u`, `g`, `r`, and `h` flip between
ADVICE, SUBAGENTS, LOCAL MODELS, USAGE, GATEWAY, REMOTE, and HELP rather than stacking. With two dozen sessions
on screen a stacked second panel lands below the bottom of the terminal, which
is indistinguishable from the key not working. For the same reason the frame
now says `... N more line(s) below` instead of quietly truncating.

## Help

`h` or `?` opens a HELP panel — not a keybinding reference (the footer hint
already lists the keys), but a one-line-each rundown of what each screen on
the display means: INFRA, WORKERS, SUBAGENTS, ADVICE, LOCAL MODELS, USAGE,
GATEWAY, REMOTE.
roost is small enough that this is the whole manual.

## Local models

The INFRA line only ever shows what Ollama currently has resident in VRAM —
`ollama:11434 up  qwen-coder-16k:latest (5.5 GB)` — because it reads
`/api/ps`. A model that is installed but idle drops out of that line entirely,
which reads as "roost doesn't see it" rather than "it isn't loaded right now."

Press `m` for the full picture: every model `ollama list` knows about, each
row showing disk size, residency, VRAM when loaded, and how long until Ollama
unloads it. It reads `/api/tags` merged with `/api/ps`; if Ollama isn't
running, the panel says so instead of showing nothing.

## Usage and the weekly budget

`u` opens the USAGE panel: tokens per day per model over the last week, tallied
from the transcripts on disk (input + output from each assistant turn; cache
reads are excluded on purpose — they are billed and limited differently, and
counting them would swamp the number with re-reads of unchanged context).

```
USAGE  observed transcript tokens (input+output) -- an estimate, not the Anthropic meter
  2026-08-02  2.9M  opus-5 1.6M, sonnet-5 965k, fable-5 253k <- today
  2026-08-01  9.1M  opus-5 4.5M, fable-5 3.3M, sonnet-5 984k
  2026-07-29  1.9M  gemma4-32k (local) 5.3M, opus-5 1.9M

  today 2.9M  |  7d 24.8M cloud  / 60.0M budget (41%)
```

Two honesty rules bake into this panel. First, **it is an estimate, not the
Anthropic meter** — there is no local file or API that records a plan's real
rate-limit balance, so the budget is a number you set yourself:

```bash
export ROOST_WEEKLY_BUDGET=60M    # or 850k, or a plain token count
```

Run `/usage` inside Claude Code once, pick a number that matches what it shows,
and the panel tracks your burn against it from then on. Unset, the tallies show
without the budget fraction. Second, **local models are free** — anything
without a `claude-` model name (Ollama via LiteLLM writes transcripts too) is
flagged `(local)` in the breakdown and excluded from the cloud totals and the
budget math.

The first `u` scans a week of transcripts and can pause for a moment; after
that only appended bytes are read, so keeping the panel open costs almost
nothing. Day boundaries are UTC, because transcript timestamps are.

## Gateway and batch runs

`g` opens the GATEWAY panel: whether the LiteLLM proxy answers on
`127.0.0.1:4000`, plus one row per batch-extraction run under the batch
directory (`ROOST_BATCH_DIR`, default `~/litellm-server/batch`):

```
GATEWAY
  litellm up (127.0.0.1:4000)   last request 42s ago   3 req/min
  jobs queue: inbox 0  running 1  done 12  failed 0
  BATCH RUN              MODEL           DONE/TOTAL  FAIL  RATE   ETA    LAST
  results-laneB-derived  gemma4-32k      121/300     2     64/hr  2h48m  35s ago
  results-laneA-derived  qwen-coder-16k  5/5         0     -      done   3d ago
```

The proxy itself is asked nothing beyond "are you up" — a DB-less LiteLLM
keeps no request history (every activity endpoint 400s), so progress is
derived from the batch pipeline's own output files: one JSON per finished
item means done/total, failure count, write rate, and ETA all fall out of a
directory listing. Runs whose `extract.py` wrote a `_run.json` breadcrumb show
their model and worklist; older runs still appear, just with less detail.
Green rows are actively writing; the last-request/req-per-min figures are a
best-effort read of `proxy.log`'s tail and disappear rather than guess when
the log doesn't parse. The `jobs queue` line counts the file-based job queue's
dirs (`JOBS_ROOT`, default `~/jobs`) if present.

## Remote hosts

`r` opens the REMOTE panel — other machines' roost, over ssh:

```
REMOTE
  HOST    WORKERS  WORKING  RESIDENT MODELS  BATCH                      JOBS              AGE
  hyrule  8        2        -                -                          in 0 run 1 fail 0  12s
```

Hosts come from `ROOST_REMOTES` (comma-separated ssh aliases, default
`hyrule`) — from the environment only, never from file contents. Each host is
fetched with `ssh <host> roost --json` on a background thread and cached: an
unreachable host (a closed laptop lid) keeps its last good row with the AGE
column saying how old it is, instead of hanging the display. Only the very
first fetch per host blocks, which is what makes `roost --remote -1` useful.
`ROOST_REMOTE_CMD` overrides the remote command if roost lives somewhere the
non-login ssh PATH can't see.

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

## Help wanted

roost is one person's tool with a public issue tracker. The
[`help wanted` issues](https://github.com/gmhoward9289-ops/roost/labels/help%20wanted)
are real asks, not decoration: Linux field reports, a canary test for the
undocumented on-disk format, adapters for other agent CLIs (gated on demand —
comment if you would use one), and config seams for the window tiers and the
ADVICE thresholds.

## Repo structure

- `roost.py` — the entire program. One file, stdlib only.
- `bin/roost.js` — the npm wrapper; locates a Python interpreter and runs
  `roost.py` under it.
- `packaging/` — the Homebrew formula (`roost.rb`), the `.deb` build
  (`build-deb.sh`), the version-consistency check that CI runs on every PR
  (`check-version-consistency.sh`), and the apt signing key (`apt/`).
- `tests/` — the unittest suite `ci.yml` runs on Linux, macOS and Windows.
- `demo/` — the vhs tapes and GIFs in this README, plus the fleet stager that
  produces the staged data they record against.
- `.github/workflows/` — `ci.yml` (tests, packaging checks, semgrep),
  `release.yml` (publishes a tagged release to PyPI, npm and the Homebrew
  tap), `release-please.yml` (maintains the release PR), and
  `pr-title-lint.yml` (enforces conventional commit titles on PRs).

## Release notes

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE). Contact: dev@swamplink.com

Built in a Digital Swamp. From my swamp to yours.
