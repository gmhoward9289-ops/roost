#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 george
"""roost -- every Claude worker and the local infra, on one screen, live.

Runs unchanged on COOPER (Windows, py3.14) and hyrule (macOS, py3.9). Stdlib only,
no claudectl: claudectl ships no Windows build, and everything it reports is
already in Claude Code's own state.

    roost.py            one frame                 (Windows: .PY is in PATHEXT)
    ./roost -w          live, REFRESH_SECONDS apart   (macOS)
    roost.py -w 5       slower refresh for this run
    roost.py --json     records, for piping

In live mode: space repaints now, q quits, Ctrl-C quits. The refresh interval is
the REFRESH_SECONDS constant below -- edit it to change the default everywhere.

i arms interactive mode, off by default. Arming it is what turns on the cursor
and the EXPERIMENTAL tag together -- one key for the whole risky half, so a
stray keypress on a dashboard left running cannot end a session by accident.
Once armed, j/k (or the arrow keys) raise a cursor, which is the only way to
act on a row: x stops the selected session, y copies its sessionId for
`claude --resume`. Both act on the row object that was on screen when the key
was pressed, never on an index re-resolved afterwards -- rows reorder between
frames as sessions go quiet, and an index that outlived its frame would
eventually hit the wrong one.

Three sources, all local and all read-only:

  ~/.claude/sessions/<pid>.json    live workers: pid, sessionId, launch cwd, name
  ~/.claude/projects/*/<sid>.jsonl the transcript -- model in use, and the usage
                                   block that gives context actually consumed
  127.0.0.1 ports                  ollama / litellm / openwebui

WORKERS is what each session *is*; INFRA is what it is running against. hyrule has
no local inference stack, so its INFRA panel reads "not running" -- that is
accurate there, not a failure.

Context is an estimate: the last assistant turn's input + cache_read +
cache_creation, over the model's window. It tracks what Claude Code shows without
being derived from it.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import socket
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# release-please rewrites the line below on a release PR. The marker is on
# its own line rather than trailing the assignment because release.yml,
# build-deb.sh and check-version-consistency.sh all parse this line with a
# greedy `sed -n 's/^__version__ = "\(.*\)"/\1/p'`, which would swallow a
# trailing comment into the version string.
# x-release-please-start-version
__version__ = "0.5.0"
# x-release-please-end

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"

# The real Anthropic meter -- session/weekly/Fable caps -- has no local source;
# this cache is written by the "claude-usage-scrape" scheduled task (a Claude
# session driving claude-in-chrome against claude.ai/settings/usage), not by
# roost itself. Missing/stale is the normal state on a machine without that
# task, or on hyrule, which has no browser control at all.
USAGE_CACHE = HOME / "claude-usage" / "usage.json"
USAGE_STALE_SECS = 4 * 2 * 3600  # 4x the scrape task's 2h cadence

# ---- config ----------------------------------------------------------------
# Seconds between automatic repaints. Override per-run with `-w N`; space forces
# an immediate repaint regardless.
REFRESH_SECONDS = 1.0

# Only the tail of a transcript matters and they grow to hundreds of MB.
TAIL_BYTES = 262144

# How many past context readings the TREND column spans. Turns, not seconds:
# context only moves when a turn completes, so a time-based window would be
# empty on a session that has been thinking for a minute and misleading on one
# taking a turn a second. Kept small -- it is read out of the transcript tail,
# and a long window would need a longer tail to fill.
HISTORY_TURNS = 8

# A subagent whose parent has exited is still worth seeing for a while -- usually
# it is the run that just finished. Older than this and it is history.
AGENT_RECENT_SECS = 3600

# A subagent counts as working if its transcript was written this recently.
AGENT_ACTIVE_SECS = 30

# Token-flow sparkline on the WORKER table: sample count kept per session, and
# the minimum seconds between samples so a keypress-forced repaint does not
# stuff the history with extra zeros.
SPARK_LEN = 15
SPARK_MIN_STEP = 1.0
# ASCII ramp, dimmest to hottest. Block-drawing characters mojibake in the
# Windows console (same reason bar() is ASCII), so the ramp is punctuation.
# "." is a taken sample with zero flow; a space means no sample yet.
SPARK_RAMP = ".:-=+*#"

# USAGE panel: how far back the tally reaches, and the weekly budget it is
# measured against. There is no local source for the real Anthropic meter, so
# the budget is a number the user sets once after looking at /usage --
# e.g. ROOST_WEEKLY_BUDGET=60M or 850k or a plain integer of tokens.
USAGE_DAYS = 7
USAGE_BUDGET_ENV = "ROOST_WEEKLY_BUDGET"

# GATEWAY panel: where the batch pipeline writes its runs, and where the job
# queue lives. The gateway itself is DB-less, so every activity endpoint 400s --
# the filesystem is the source of truth for what it has been doing.
BATCH_DIR_ENV = "ROOST_BATCH_DIR"
JOBS_DIR_ENV = "JOBS_ROOT"
# A batch run counts as actively writing if its newest output landed within
# ~2x the slower lane's per-item time (~110s for gemma; see the batch README).
BATCH_ACTIVE_SECS = 240
PROXY_LOG_TAIL = 65536

# REMOTE panel: ssh aliases come from the environment only, never from file
# contents -- anything writable over the network must not choose ssh targets.
REMOTES_ENV = "ROOST_REMOTES"
REMOTE_CMD_ENV = "ROOST_REMOTE_CMD"
# ssh runs a non-login shell, whose PATH misses Homebrew and per-user bin dirs
# -- so the default widens PATH rather than assuming an install location.
REMOTE_CMD_DEFAULT = (
    'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/Claude/bin" roost --json')
REMOTE_REFRESH_SECS = 30
REMOTE_TIMEOUT_SECS = 15

# Every session roost stops gets one JSON line here. Same shape and the same cap
# as the hook logs next to it, so the same one-liners read all of them.
LOG_PATH = HOME / ".claude" / "logs" / "roost.jsonl"
LOG_MAX_LINES = 5000
# -----------------------------------------------------------------------------

# Set in main(); --no-log turns it off.
LOGGING = True

# agentId -> {description, status, model, type}, harvested from the parent
# transcript. Merged as records arrive; evicted only when the agent itself is
# gone (prune_caches below).
_AGENT_META = {}
_AGENT_LABEL = {}

# parent transcript path -> bytes already harvested for agent meta.
_HARVEST_POS = {}

# Keys touched since the last prune. Anything not re-touched belongs to a
# session or agent that no longer exists; keeping it is a slow leak in a
# dashboard left open for days.
_SEEN_PATHS = set()
_SEEN_AGENTS = set()

# path -> (mtime, parsed result). At a 1s refresh, re-reading 256 KB from every
# transcript each tick is megabytes of disk per second for no new information --
# a transcript that has not been written to cannot have a new model or usage.
_SCAN_CACHE = {}

# sessionId -> {"prev": last ctx_tokens, "t": last sample time, "hist": deque}.
# In-memory only: the sparkline shows flow since roost started, nothing older.
_SPARK = {}

# path -> {"mtime", "size", "counts": {(day, model): tokens}}. Incremental: on
# growth only the appended bytes are read, so the full-file pass happens once
# per transcript per roost run.
_USAGE_CACHE = {}

# Nothing in the transcript records which context window a session was opened
# with. Best source: the model name itself -- Anthropic documents each model's
# real window, and unlike usage that is exact regardless of how little of it
# has been used. A claude-fable-5 worker at 177k tokens is ~18% of its real 1M
# window; scored against the wrong 200k tier (usage-only inference) that read
# as an alarming 89% and tripped a false NEAR LIMIT warning.
#
# MODEL_WINDOWS is exact-match first, then longest-matching-prefix, so a dated
# snapshot under a known family (any claude-haiku-4-5-*) resolves without its
# own table row. A model this table has never heard of falls back to the old
# behaviour -- the smallest standard tier the observed usage still fits in --
# and its label is "~"-marked so an inferred window never looks as certain as
# a known one on screen.
MODEL_WINDOWS = {
    "claude-fable-5": 1000000,
    "claude-opus-5": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-haiku-4-5": 200000,
    # Legacy -- may still appear in old transcripts.
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-sonnet-4-5-20250929": 200000,
    "claude-opus-4-5-20251101": 200000,
    "claude-opus-4-1-20250805": 200000,
}

WINDOW_TIERS = ((200000, "200k"), (1000000, "1M"))


def model_window(model):
    """Known window size for `model`, or None if it is not in MODEL_WINDOWS --
    exact match first, then the longest matching prefix."""
    if not model:
        return None
    if model in MODEL_WINDOWS:
        return MODEL_WINDOWS[model]
    best_key, best_size = "", None
    for key, size in MODEL_WINDOWS.items():
        if model.startswith(key) and len(key) > len(best_key):
            best_key, best_size = key, size
    return best_size


def _window_label(size):
    return "1M" if size >= 1000000 else "%dk" % (size // 1000)


def window_for(tokens, model=None):
    """(window_size, label) for a session's context window. Known models
    resolve exactly off MODEL_WINDOWS; anything else falls back to the old
    usage-based inference, its label "~"-marked to say so."""
    size = model_window(model)
    if size is not None:
        return size, _window_label(size)
    for size, label in WINDOW_TIERS:
        if tokens <= size:
            return size, "~" + label
    return WINDOW_TIERS[-1][0], "~" + WINDOW_TIERS[-1][1]


# ---- auto-compact resolution ------------------------------------------------
# autoCompactEnabled is a Claude Code setting -- never written to a session lock
# file or a transcript, so seeing a worker with it off takes walking the same
# settings.json hierarchy Claude Code itself merges: managed, then a project's
# own .claude/settings.local.json, then its .claude/settings.json, then the
# user's ~/.claude/settings.json. Highest scope that sets the key wins; a file
# that exists but never mentions autoCompactEnabled (or its env-var twin,
# DISABLE_AUTO_COMPACT) is transparent, same as Claude Code's own merge.
#
# What this cannot see: a CLI flag the session itself was launched with, or
# DISABLE_AUTO_COMPACT exported in a shell rather than written into a
# settings.json's own "env" block -- roost reads files, not another process's
# environment or argv.
AUTO_COMPACT_KEY = "autoCompactEnabled"
AUTO_COMPACT_ENV_KEY = "DISABLE_AUTO_COMPACT"
MANAGED_SETTINGS_PATH = {
    "win32": Path(r"C:\Program Files\ClaudeCode\managed-settings.json"),
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
}.get(sys.platform, Path("/etc/claude-code/managed-settings.json"))


def _auto_compact_from_file(path):
    """True/False if this scope's settings.json decides the setting, else None
    -- meaning fall through to the next scope down."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if AUTO_COMPACT_KEY in data:
        return bool(data[AUTO_COMPACT_KEY])
    env = data.get("env")
    if isinstance(env, dict) and AUTO_COMPACT_ENV_KEY in env:
        # DISABLE_AUTO_COMPACT is the env-var mirror of the settings key --
        # truthy disables, so the boolean it decides is inverted.
        return str(env[AUTO_COMPACT_ENV_KEY]).strip().lower() not in ("1", "true", "yes")
    return None


def auto_compact_enabled(cwd):
    """Effective autoCompactEnabled for a session launched from `cwd`. Defaults
    True -- Claude Code's own built-in default -- if nothing in the chain sets
    it anywhere."""
    if not cwd:
        return True
    base = Path(cwd)
    for path in (MANAGED_SETTINGS_PATH,
                 base / ".claude" / "settings.local.json",
                 base / ".claude" / "settings.json",
                 HOME / ".claude" / "settings.json"):
        result = _auto_compact_from_file(path)
        if result is not None:
            return result
    return True


SERVICES = (
    ("ollama", 11434, "/api/ps"),
    ("litellm", 4000, "/health/liveliness"),
    ("openwebui", 8080, "/health"),
)


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
REVERSE = "\033[7m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

# Set once in main(), after we know whether the terminal can render escapes.
COLOR = False


def c(text, *codes):
    if not COLOR or not codes:
        return text
    return "".join(codes) + text + RESET


def highlight(line):
    """Reverse-video a whole line that already contains colour.

    Every per-cell colour ends in RESET, which clears reverse video along with
    the colour -- so a naive wrap highlights only up to the first coloured cell.
    Re-arming after each RESET keeps the bar unbroken across the row.
    """
    if not COLOR:
        return line
    return REVERSE + line.replace(RESET, RESET + REVERSE) + RESET


def ascii_safe(s):
    """Drop characters the console cannot render.

    Task text is free-form prose and often carries em dashes and smart quotes;
    the Windows console codepage turns those into replacement blobs mid-table.
    """
    if not s:
        return ""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)


def visible_len(s):
    """Length ignoring ANSI escapes -- what the terminal actually shows."""
    n = 0
    i = 0
    while i < len(s):
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        n += 1
        i += 1
    return n


def clip_ansi(s, width):
    """Clip to `width` visible columns, keeping escapes intact.

    A naive s[:width] counts escape bytes as columns and truncates mid-sequence,
    which both over-clips the text and leaks raw escape codes onto the screen.
    """
    if visible_len(s) <= width:
        return s
    out = []
    n = 0
    i = 0
    while i < len(s) and n < width:
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        out.append(s[i])
        n += 1
        i += 1
    if COLOR:
        out.append(RESET)
    return "".join(out)


def alive(pid):
    """True if the process exists. os.kill(pid, 0) is POSIX-only."""
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def trim_log():
    """Hold the log at LOG_MAX_LINES, checked by size so the common write is one
    append and no read. Records run a few hundred bytes; the slack is deliberate."""
    try:
        if LOG_PATH.stat().st_size < LOG_MAX_LINES * 400:
            return
        kept = LOG_PATH.read_text(encoding="utf-8").splitlines()[-LOG_MAX_LINES:]
        LOG_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


def log_action(action, worker, ok=True, detail=""):
    """Append one record per action roost takes.

    Actions only -- frames are not logged, and neither is task text. The task is
    free-form prose out of a transcript and would turn an audit trail into a
    copy of what was being worked on; name and sessionId identify the session
    without carrying its contents. The numbers alongside are what make the log
    answer a real question later: how much context a sweep actually reclaimed.

    Never raises. A log that cannot be written is not a reason to lose the UI.
    """
    if not LOGGING:
        return
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "ok": ok,
        "host": socket.gethostname(),
        "name": worker.get("name"),
        "pid": worker.get("pid"),
        "session_id": worker.get("session_id"),
        "model": worker.get("model"),
        "ctx_tokens": worker.get("ctx_tokens"),
        "idle_secs": int(worker["idle_secs"]) if worker.get("idle_secs") else None,
    }
    if detail:
        rec["detail"] = detail
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(LOG_PATH), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        return
    trim_log()


def terminate(pid):
    """Stop a session process. Returns an error string, or None on success.

    Windows has no cross-process SIGTERM, so this is TerminateProcess: immediate,
    with no chance for the session to shut down cleanly. Transcripts are written
    a turn at a time, so the most that can be lost is a turn already in flight --
    but it is a kill, not a request, and the man page says so. POSIX gets a real
    SIGTERM and the session exits on its own terms.
    """
    if pid in (os.getpid(), os.getppid()):
        # roost run from inside the session it is pointed at: the cursor lands on
        # the row whose process owns this terminal, and x would take roost with it.
        return "refusing to stop roost's own process tree"
    if os.name == "nt":
        import ctypes

        PROCESS_TERMINATE = 0x0001
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return "cannot open pid %d (already gone, or not yours)" % pid
        ok = ctypes.windll.kernel32.TerminateProcess(h, 1)
        ctypes.windll.kernel32.CloseHandle(h)
        return None if ok else "TerminateProcess failed on pid %d" % pid
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return "%s (pid %d)" % (e.strerror or e, pid)
    return None


# xclip is the one that may genuinely be absent; a failed copy is reported, not
# swallowed, because the whole point is pasting the id into a resume command.
CLIP_CMD = {"win32": ["clip"], "darwin": ["pbcopy"]}.get(
    sys.platform, ["xclip", "-selection", "clipboard"])


def to_clipboard(text):
    try:
        p = subprocess.Popen(CLIP_CMD, stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        return p.returncode == 0
    except OSError:
        return False


def port_open(port, timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_json(port, path, timeout=1.5):
    import urllib.request

    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def transcript_for(session_id):
    if not session_id:
        return None
    hits = glob.glob(str(PROJECTS_DIR / "*" / (session_id + ".jsonl")))
    return hits[0] if hits else None


def read_tail(path, nbytes=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()  # drop the partial line the seek landed in
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def scan_transcript(path):
    """Model and context from the newest assistant turn that carries usage.

    Also the last HISTORY_TURNS context totals, oldest first, out of the same
    backward walk. History read from the transcript rather than accumulated
    across frames is populated on the very first frame and survives a restart,
    so it works under --once and --json too -- a ring buffer kept in memory
    would give neither, and at a 1s refresh would sample the same turn dozens
    of times over.
    """
    out = {"model": None, "ctx_tokens": None, "last_write": None,
           "title": None, "prompt": None, "ctx_history": []}
    if not path:
        return out
    _SEEN_PATHS.add(path)
    try:
        out["last_write"] = os.path.getmtime(path)
    except OSError:
        pass

    cached = _SCAN_CACHE.get(path)
    if cached is not None and cached[0] == out["last_write"]:
        return dict(cached[1])

    # Walking backwards, take the newest of each: usage (model + context),
    # customTitle (what Claude Code named the session), lastPrompt (what was last
    # asked). Both title records recur throughout the file, so the tail has them.
    for line in reversed(read_tail(path)):
        line = line.strip()
        if not line:
            continue
        # Cheap pre-filter -- json.loads on every tail line is the expensive part.
        has_usage = '"usage"' in line and '"assistant"' in line
        has_title = '"customTitle"' in line
        has_prompt = '"lastPrompt"' in line
        if not (has_usage or has_title or has_prompt):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue

        if out["title"] is None and d.get("customTitle"):
            out["title"] = str(d["customTitle"]).strip()
        if out["prompt"] is None and d.get("lastPrompt"):
            out["prompt"] = " ".join(str(d["lastPrompt"]).split())

        msg = d.get("message") or {}
        usage = msg.get("usage") or {}
        if usage:
            total = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            if out["model"] is None:
                out["model"] = msg.get("model")
                out["ctx_tokens"] = total
            # A turn that used tools writes several assistant records carrying
            # the same context total. Only a change is a new data point, or a
            # tool-heavy turn would fill the whole window with one turn's value.
            if len(out["ctx_history"]) < HISTORY_TURNS and (
                    not out["ctx_history"] or out["ctx_history"][-1] != total):
                out["ctx_history"].append(total)

        if (out["model"] and out["title"] and out["prompt"]
                and len(out["ctx_history"]) >= HISTORY_TURNS):
            break

    out["ctx_history"].reverse()  # collected newest-first, reported oldest-first

    if out["last_write"] is not None:
        _SCAN_CACHE[path] = (out["last_write"], dict(out))
    return out


def harvest_agent_meta(parent_transcript):
    """Pull subagent description/status out of the parent's tool results.

    A subagent's own transcript never states what it was asked to do in short
    form -- only the parent's `toolUseResult` carries `description`, `status`,
    `resolvedModel` and `agentType`, keyed by agentId. Incremental rather than
    tail-only: a busy parent grows past TAIL_BYTES with its agents' results in
    the half a tail read never sees. The full pass happens once per parent per
    roost run; after that only appended bytes are read, so the every-tick call
    for a still-running agent costs one getsize().
    """
    if not parent_transcript:
        return
    # Keeps the byte position alive across prunes while anything still asks
    # about this parent; without it an orphan's dead parent would be evicted
    # and re-read from byte 0 every tick.
    _SEEN_PATHS.add(parent_transcript)
    pos = _HARVEST_POS.get(parent_transcript, 0)
    try:
        size = os.path.getsize(parent_transcript)
        if size < pos:
            pos = 0  # truncated or replaced -- start over
        if size == pos:
            return
        with open(parent_transcript, "rb") as fh:
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return
    # Consume whole lines only; a half-written trailing line waits for the
    # next pass instead of being parsed as garbage and skipped forever.
    cut = data.rfind(b"\n") + 1
    _HARVEST_POS[parent_transcript] = pos + cut
    for line in data[:cut].decode("utf-8", "replace").splitlines():
        if '"agentId"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        r = d.get("toolUseResult")
        if not isinstance(r, dict):
            continue
        aid = r.get("agentId")
        if not aid:
            continue
        # Marked seen even when the agent has no live row: evicting it here
        # would just force a re-harvest next tick.
        _SEEN_AGENTS.add(aid)
        # Merge rather than first-write-wins: an async launch writes an early
        # record with no agentType, and the completed result that carries it
        # would otherwise be discarded.
        meta = _AGENT_META.setdefault(
            aid, {"description": "", "status": "", "model": "", "type": ""})
        for key, field in (("description", "description"), ("status", "status"),
                           ("model", "resolvedModel"), ("type", "agentType")):
            val = r.get(field)
            if val:
                meta[key] = val


def agent_first_prompt(path, agent_id):
    """Fallback label: the opening words of the task the subagent was given.

    Used when the parent's tool result has scrolled out of the tail. The first
    line of a subagent transcript is immutable, so this is cached outright.
    """
    if agent_id in _AGENT_LABEL:
        return _AGENT_LABEL[agent_id]
    label = ""
    try:
        with open(path, "rb") as fh:
            first = fh.readline().decode("utf-8", "replace")
        d = json.loads(first)
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict))
        label = " ".join(str(content or "").split())[:60]
    except (OSError, ValueError):
        label = ""
    _AGENT_LABEL[agent_id] = label
    return label


def collect_subagents(live_sids):
    """Subagents run inside their parent process, so they have no pid of their
    own -- but each gets its own transcript at

        projects/<slug>/<parentSessionId>/subagents/agent-<id>.jsonl

    which is one level deeper than the main session transcripts.
    """
    rows = []
    now = time.time()
    pattern = str(PROJECTS_DIR / "*" / "*" / "subagents" / "agent-*.jsonl")
    for path in glob.glob(pattern):
        p = Path(path)
        parent_sid = p.parent.parent.name
        agent_id = p.stem[len("agent-"):] if p.stem.startswith("agent-") else p.stem

        info = scan_transcript(path)
        last = info["last_write"]
        parent_live = parent_sid in live_sids
        age = (now - last) if last else None
        if not parent_live and (age is None or age > AGENT_RECENT_SECS):
            continue  # finished long ago -- history, not a live worker

        _SEEN_AGENTS.add(agent_id)
        # Re-harvest while the type is still missing: a running agent's early
        # records carry no agentType; the completed result does.
        if not (_AGENT_META.get(agent_id) or {}).get("type"):
            harvest_agent_meta(transcript_for(parent_sid))
        meta = _AGENT_META.get(agent_id) or {}

        label = ascii_safe(
            meta.get("description") or agent_first_prompt(path, agent_id) or "-")
        model = info["model"] or meta.get("model") or "-"
        pct = None
        win_label = "-"
        if info["ctx_tokens"]:
            window, win_label = window_for(info["ctx_tokens"], model)
            pct = 100.0 * info["ctx_tokens"] / float(window)

        # Parent liveness gates everything: if the parent process is gone, nothing
        # can still be writing to this transcript, however recent the last write.
        if not parent_live:
            state = "orphan"
        elif age is not None and age <= AGENT_ACTIVE_SECS:
            state = "working"
        else:
            state = "idle"

        rows.append({
            "agent_id": agent_id,
            # Sanitised like task text: it comes out of a transcript, and a
            # crafted agentType could otherwise carry escapes into the TUI.
            "agent_type": ascii_safe(meta.get("type") or ""),
            "parent_sid": parent_sid,
            "task": label,
            "model": model,
            "ctx_tokens": info["ctx_tokens"],
            "ctx_pct": pct,
            "window": win_label,
            "idle_secs": age,
            "state": state,
            "parent_live": parent_live,
        })
    rows.sort(key=lambda r: (r["state"] != "working", r["idle_secs"] or 1e9))
    return rows


def collect_workers():
    rows = []
    if not SESSIONS_DIR.is_dir():
        return rows
    now = time.time()
    ac_cache = {}
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = s.get("pid")
        if not isinstance(pid, int) or not alive(pid):
            continue
        sid = s.get("sessionId") or ""
        info = scan_transcript(transcript_for(sid))
        pct = None
        win_label = "-"
        if info["ctx_tokens"]:
            window, win_label = window_for(info["ctx_tokens"], info["model"])
            pct = 100.0 * info["ctx_tokens"] / float(window)
        idle = None
        if info["last_write"]:
            idle = now - info["last_write"]

        # Token-flow sample for the FLOW sparkline: growth of the newest turn's
        # context since the last sample is a cheap throughput proxy. Compaction
        # shrinks the context -- that is not negative flow, so it clamps to 0.
        # Throttled so a keypress-forced repaint cannot stuff the history.
        flow = " " * SPARK_LEN
        if sid:
            st = _SPARK.setdefault(
                sid, {"prev": None, "t": 0.0, "hist": deque(maxlen=SPARK_LEN)})
            tok = info["ctx_tokens"]
            if tok is not None and now - st["t"] >= SPARK_MIN_STEP:
                if st["prev"] is not None:
                    st["hist"].append(max(0, tok - st["prev"]))
                st["prev"] = tok
                st["t"] = now
            flow = spark(st["hist"])

        started = s.get("startedAt")
        age = (now - started / 1000.0) if isinstance(started, (int, float)) else None
        cwd = s.get("cwd") or ""
        if cwd not in ac_cache:
            ac_cache[cwd] = auto_compact_enabled(cwd)
        rows.append({
            "name": s.get("name") or "-",
            "pid": pid,
            "session_id": sid,
            "cwd": cwd,
            "project": Path(cwd or ".").name or "-",
            "model": info["model"] or "-",
            "ctx_tokens": info["ctx_tokens"],
            "ctx_pct": pct,
            # Oldest first, at most HISTORY_TURNS long. Emitted by --json too:
            # the series is more use to a script than the rendered delta is.
            "ctx_history": info["ctx_history"],
            "window": win_label,
            # The title Claude Code gave the session; the last prompt is the
            # fallback for sessions too young to have been named yet.
            # Sanitised at the source: this is transcript text, and it lands in
            # a TUI that steers the cursor with escape sequences. An unescaped
            # a raw ESC in a title could clear the screen or repaint the table.
            "task": ascii_safe(info["title"] or info["prompt"] or ""),
            "task_src": "title" if info["title"] else ("prompt" if info["prompt"] else "-"),
            "idle_secs": idle,
            "age_secs": age,
            "flow": flow,
            "auto_compact": ac_cache[cwd],
        })
    return rows


def collect_infra():
    out = []
    for name, port, path in SERVICES:
        if not port_open(port):
            out.append({"name": name, "port": port, "up": False, "detail": "not running"})
            continue
        detail = ""
        if name == "ollama":
            ps = http_json(port, path)
            models = (ps or {}).get("models") or []
            if models:
                detail = ", ".join(
                    "%s (%.1f GB)" % (m.get("name", "?"), (m.get("size_vram") or m.get("size") or 0) / 1e9)
                    for m in models
                )
            else:
                detail = "no model resident"
        out.append({"name": name, "port": port, "up": True, "detail": detail})
    return out


def collect_usage_caps():
    """The real 5-hour/weekly/Fable caps, from the claude-usage-scrape cache.

    Returns None if the cache has never been written (task not yet run, or not
    installed on this machine) -- distinct from a stale-but-present cache, which
    render_usage_caps shows with an age instead of hiding.
    """
    try:
        with open(USAGE_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    last_success = data.get("last_success_epoch")
    data["age_secs"] = (time.time() - last_success) if last_success else None
    return data


def _iso_to_epoch(ts):
    """Ollama's expires_at is RFC3339 with an offset, e.g.
    '2026-08-01T15:04:05.123456-07:00'. Any format surprise just drops the
    unload timer -- it is not worth losing the whole panel over."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def collect_local_models():
    """Installed and resident Ollama models, merged from /api/tags and /api/ps.

    /api/ps alone -- what the INFRA line uses -- only sees what is currently in
    VRAM, so a model that is installed but idle is invisible there. This is the
    fuller picture: everything `ollama list` knows about, with residency and a
    VRAM figure layered on top for whichever of those happen to be loaded.
    """
    if not port_open(11434):
        return []
    tags = http_json(11434, "/api/tags") or {}
    ps = http_json(11434, "/api/ps") or {}
    resident = {m.get("name"): m for m in (ps.get("models") or [])}
    out = []
    for m in tags.get("models") or []:
        name = m.get("name", "?")
        r = resident.get(name)
        expires_secs = None
        if r and r.get("expires_at"):
            epoch = _iso_to_epoch(r["expires_at"])
            if epoch is not None:
                expires_secs = max(0, epoch - time.time())
        out.append({
            "name": name,
            "disk_gb": (m.get("size") or 0) / 1e9,
            "resident": r is not None,
            "vram_gb": ((r.get("size_vram") or r.get("size") or 0) / 1e9) if r else None,
            "expires_secs": expires_secs,
        })
    return out


def _batch_root():
    return Path(os.environ.get(BATCH_DIR_ENV,
                               str(HOME / "litellm-server" / "batch")))


def _jobs_root():
    return Path(os.environ.get(JOBS_DIR_ENV, str(HOME / "jobs")))


def _proxy_log_activity(path):
    """Last-request age and requests/min, read off the tail of proxy.log.

    Best-effort by design: the log format is LiteLLM's to change, so anything
    that fails to parse just drops these two numbers rather than the panel.
    Only timestamped lines mentioning a completions route count as requests.
    """
    lines = read_tail(str(path), PROXY_LOG_TAIL)
    stamp = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
    now = time.time()
    last = None
    recent = 0
    for line in lines:
        if "completion" not in line and "chat" not in line:
            continue
        m = stamp.search(line)
        if not m:
            continue
        try:
            t = time.mktime(time.strptime(
                m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        last = t if last is None else max(last, t)
        if now - t <= 60:
            recent += 1
    return {"last_req_secs": (now - last) if last is not None else None,
            "req_per_min": recent if last is not None else None}


def collect_gateway():
    """LiteLLM liveliness plus batch-run progress, derived from files.

    The gateway is DB-less, so its activity endpoints all 400 -- but the batch
    pipeline is resumable by construction (one output file per finished item),
    which means progress is fully derivable from the filesystem with zero
    cooperation from the running process. _run.json, when extract.py wrote one,
    pins the worklist and model; without it the run still shows, just with less.
    """
    now = time.time()
    out = {"litellm_up": port_open(4000), "runs": [], "jobs": None,
           "last_req_secs": None, "req_per_min": None}

    root = _batch_root()
    log = root.parent / "proxy.log"
    if log.exists():
        out.update(_proxy_log_activity(log))

    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            try:
                outputs = [p for p in d.glob("*.json")
                           if not p.name.startswith("_")]
                failures = d / "_failures.jsonl"
                failed = 0
                if failures.exists():
                    failed = sum(
                        1 for ln in failures.read_text(
                            encoding="utf-8", errors="replace").splitlines()
                        if ln.strip())
                meta = {}
                rj = d / "_run.json"
                if rj.exists():
                    try:
                        meta = json.loads(rj.read_text(encoding="utf-8"))
                    except ValueError:
                        meta = {}
                # A dir of loose JSON is not automatically a run: schemas/ and
                # the like would otherwise show up as one. Without a _run.json
                # only the results-* naming convention identifies a run.
                if not meta and not failed and not d.name.startswith("results"):
                    continue
                if not outputs and not failed and not meta:
                    continue  # empty results dir, nothing to say yet

                mtimes = sorted(p.stat().st_mtime for p in outputs)
                newest = mtimes[-1] if mtimes else None
                # Rate from the spread of the newest outputs rather than from
                # the start time: a resumed run's start says nothing about the
                # pace it is writing at now.
                rate_hr = None
                recent = mtimes[-50:]
                if len(recent) >= 2 and recent[-1] > recent[0]:
                    rate_hr = (len(recent) - 1) / (recent[-1] - recent[0]) * 3600.0
                total = meta.get("total")
                eta_secs = None
                if rate_hr and isinstance(total, int):
                    remaining = total - len(outputs) - failed
                    if remaining > 0:
                        eta_secs = remaining / rate_hr * 3600.0
                worklist = meta.get("worklist")
                out["runs"].append({
                    "name": d.name,
                    "model": meta.get("model"),
                    "worklist": Path(worklist).name if worklist else None,
                    "done": len(outputs),
                    "total": total,
                    "failed": failed,
                    "rate_hr": rate_hr,
                    "eta_secs": eta_secs,
                    "last_write_secs": (now - newest) if newest else None,
                    "active": newest is not None
                              and (now - newest) <= BATCH_ACTIVE_SECS,
                })
            except OSError:
                continue
        out["runs"].sort(key=lambda r: (not r["active"],
                                        r["last_write_secs"] or 1e12))

    jobs = _jobs_root()
    if jobs.is_dir():
        depth = {}
        for state in ("inbox", "running", "done", "failed"):
            sub = jobs / state
            if state in ("done", "failed"):
                depth[state] = sum(1 for p in sub.iterdir()
                                   if p.is_dir()) if sub.is_dir() else 0
            else:
                depth[state] = len(list(sub.glob("*.json"))) if sub.is_dir() else 0
        out["jobs"] = depth
    return out


def render_gateway(gw):
    """LiteLLM plus everything it has been fed, without asking it anything --
    a DB-less gateway keeps no history, so the batch pipeline's own output
    files carry the progress story."""
    lines = ["", c("GATEWAY", BOLD)]
    mark = c("up", GREEN) if gw["litellm_up"] else c("DOWN", BOLD, RED)
    head = "  litellm %s (127.0.0.1:4000)" % mark
    if gw["last_req_secs"] is not None:
        head += "   last request %s ago" % dur(gw["last_req_secs"])
    if gw["req_per_min"] is not None:
        head += "   %d req/min" % gw["req_per_min"]
    lines.append(head)
    if gw["jobs"] is not None:
        j = gw["jobs"]
        inbox = str(j["inbox"])
        lines.append("  jobs queue: inbox %s  running %s  done %s  failed %s" % (
            c(inbox, BOLD, YELLOW) if j["inbox"] else inbox,
            c(str(j["running"]), GREEN) if j["running"] else j["running"],
            j["done"],
            c(str(j["failed"]), RED) if j["failed"] else j["failed"]))

    if not gw["runs"]:
        lines.append(c("  no batch runs found", DIM))
        return lines

    cols = [
        ("BATCH RUN", lambda r: r["name"]),
        ("MODEL", lambda r: r["model"] or "?"),
        ("DONE/TOTAL", lambda r: "%d/%s" % (
            r["done"], r["total"] if r["total"] is not None else "?")),
        ("FAIL", lambda r: str(r["failed"])),
        ("RATE", lambda r: "-" if not r["rate_hr"] else "%d/hr" % round(r["rate_hr"])),
        ("ETA", lambda r: dur(r["eta_secs"]) if r["eta_secs"]
            else ("done" if r["total"] is not None
                  and r["done"] + r["failed"] >= r["total"] else "-")),
        ("LAST", lambda r: "-" if r["last_write_secs"] is None
            else dur(r["last_write_secs"]) + " ago"),
    ]
    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in gw["runs"]]
    w = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines.append("  " + c("  ".join(
        table[0][i].ljust(w[i]) for i in range(len(cols))), BOLD))
    for row, r in zip(table[1:], gw["runs"]):
        cells = [row[i].ljust(w[i]) for i in range(len(cols))]
        # Same colouring rule as LOCAL MODELS: green while actively writing,
        # dim once done or stale.
        line = "  " + "  ".join(cells)
        lines.append(c(line, GREEN) if r["active"] else c(line, DIM))
    active = sum(1 for r in gw["runs"] if r["active"])
    lines.append("  " + c("%d run(s), %d active" % (len(gw["runs"]), active), DIM))
    return lines


# host -> {"data", "t", "err", "thread"}. Fetches run on daemon threads so a
# host behind a closed MacBook lid shows as stale rather than hanging the UI.
_REMOTE = {}
_REMOTE_LOCK = threading.Lock()


def _fetch_remote(host):
    cmd = os.environ.get(REMOTE_CMD_ENV, REMOTE_CMD_DEFAULT)
    err, data = None, None
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, cmd],
            capture_output=True, text=True, timeout=REMOTE_TIMEOUT_SECS)
        if p.returncode == 0:
            data = json.loads(p.stdout)
        else:
            tail = (p.stderr or "").strip().splitlines()
            err = tail[-1][:60] if tail else "exit %d" % p.returncode
    except subprocess.TimeoutExpired:
        err = "timeout after %ds" % REMOTE_TIMEOUT_SECS
    except (OSError, ValueError) as e:
        err = str(e)[:60]
    with _REMOTE_LOCK:
        st = _REMOTE.setdefault(host, {})
        if data is not None:
            st["data"] = data
            st["t"] = time.time()
            st["err"] = None
        else:
            st["err"] = err  # keep the last good data and its timestamp
        st["thread"] = None


def collect_remote():
    hosts = [h.strip() for h in
             os.environ.get(REMOTES_ENV, "hyrule").split(",") if h.strip()]
    rows = []
    now = time.time()
    for host in hosts:
        with _REMOTE_LOCK:
            st = _REMOTE.setdefault(host, {})
            age = (now - st["t"]) if st.get("t") else None
            if st.get("thread") is None and (age is None
                                             or age >= REMOTE_REFRESH_SECS):
                t = threading.Thread(target=_fetch_remote, args=(host,),
                                     daemon=True)
                st["thread"] = t
                t.start()
                first_try = not st.get("t") and not st.get("err")
            else:
                t, first_try = None, False
        # Only the very first attempt per host gets a blocking grace period --
        # it is what makes --once useful. A host that already failed once (the
        # closed-lid case) never blocks again; its row shows the error instead.
        if t is not None and first_try:
            t.join(REMOTE_TIMEOUT_SECS + 2)
        with _REMOTE_LOCK:
            st = _REMOTE[host]
            age = (time.time() - st["t"]) if st.get("t") else None
            rows.append({"host": host, "data": st.get("data"),
                         "age_secs": age, "err": st.get("err"),
                         "fetching": st.get("thread") is not None})
    return rows


def render_remote(remotes):
    """One summary row per remote host, rendered from that host's own
    `roost --json` over ssh. Data is cached: a host that stops answering keeps
    its last good row with the age saying how old it is."""
    lines = ["", c("REMOTE", BOLD)]
    if not remotes:
        lines.append(c("  set %s (comma-separated ssh aliases)" % REMOTES_ENV, DIM))
        return lines

    cols = [
        ("HOST", lambda r: r["host"]),
        ("WORKERS", lambda r: r["nworkers"]),
        ("WORKING", lambda r: r["working"]),
        ("RESIDENT MODELS", lambda r: r["resident"]),
        ("BATCH", lambda r: r["batch"]),
        ("JOBS", lambda r: r["jobs"]),
        ("AGE", lambda r: r["age"]),
    ]
    view = []
    for r in remotes:
        d = r["data"]
        if d is None:
            view.append({"host": r["host"], "nworkers": "-", "working": "-",
                         "resident": "-", "batch": "-", "jobs": "-",
                         "age": "fetching..." if r["fetching"]
                                else (r["err"] or "-"), "stale": True})
            continue
        workers = d.get("workers") or []
        working = sum(1 for w in workers
                      if w.get("idle_secs") is not None and w["idle_secs"] < 60)
        resident = ", ".join(m["name"] for m in (d.get("local_models") or [])
                             if m.get("resident")) or "-"
        gw = d.get("gateway") or {}
        runs = [x for x in (gw.get("runs") or []) if x.get("active")]
        if runs:
            batch = ", ".join("%s %d/%s" % (
                x["name"], x["done"],
                x["total"] if x.get("total") is not None else "?")
                for x in runs)
        else:
            batch = "-"
        j = gw.get("jobs")
        jobs = ("in %d run %d fail %d" % (j["inbox"], j["running"], j["failed"])
                if j else "-")
        age = dur(r["age_secs"]) if r["age_secs"] is not None else "-"
        stale = r["age_secs"] is not None and r["age_secs"] > 3 * REMOTE_REFRESH_SECS
        if r["err"]:
            age += " (%s)" % r["err"]
        elif stale:
            age += " (stale)"
        view.append({"host": r["host"], "nworkers": str(len(workers)),
                     "working": str(working), "resident": resident,
                     "batch": batch, "jobs": jobs, "age": age, "stale": stale})

    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in view]
    w = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines.append("  " + c("  ".join(
        table[0][i].ljust(w[i]) for i in range(len(cols))), BOLD))
    for row, r in zip(table[1:], view):
        cells = []
        for i, (header, _) in enumerate(cols):
            txt = row[i].ljust(w[i])
            if header == "HOST":
                cells.append(c(txt, BOLD))
            elif header == "WORKING" and not r["stale"] and row[i] not in ("0", "-"):
                cells.append(c(txt, GREEN))
            elif r["stale"] or header == "AGE":
                cells.append(c(txt, DIM))
            else:
                cells.append(txt)
        lines.append("  " + "  ".join(cells))
    return lines


def parse_budget(s):
    """'60M', '850k', or a plain token count. None on unset or garbage."""
    if not s:
        return None
    s = s.strip()
    try:
        if s and s[-1] in "kK":
            return int(float(s[:-1]) * 1000)
        if s and s[-1] in "mM":
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except ValueError:
        return None


def _tally_lines(lines, counts):
    """Accumulate (day, model) -> input+output tokens from assistant turns.

    Cache reads/creation are deliberately excluded: they are billed and
    rate-limited differently, and counting them would swamp the number with
    re-reads of unchanged context. What is left is closest to "work done".
    """
    for line in lines:
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        day = str(d.get("timestamp") or "")[:10]
        if len(day) != 10:
            continue
        # Raw model name kept: the "claude-" prefix is what later separates
        # cloud burn (counts against the plan) from local models (free).
        # Sanitised: it is transcript text headed for the TUI.
        model = ascii_safe(msg.get("model") or "?") or "?"
        tok = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        key = (day, model)
        counts[key] = counts.get(key, 0) + tok


def collect_usage():
    """Observed tokens per day per model over the last USAGE_DAYS.

    Incremental: each transcript is read in full exactly once per roost run
    (only files touched inside the window), then only appended bytes after
    that. Only called while the USAGE panel is open, so the one full pass
    happens on the first `u`, not at launch.
    """
    cutoff = time.time() - USAGE_DAYS * 86400
    paths = set(glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")))
    paths.update(glob.glob(
        str(PROJECTS_DIR / "*" / "*" / "subagents" / "agent-*.jsonl")))

    # A deleted transcript never reappears in the glob, so its entry would sit
    # in the cache forever -- still counted into the panel, and a slow leak.
    for path in [p for p in _USAGE_CACHE if p not in paths]:
        del _USAGE_CACHE[path]

    for path in paths:
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            _USAGE_CACHE.pop(path, None)  # deleted between glob and stat
            continue
        if mtime < cutoff:
            _USAGE_CACHE.pop(path, None)
            continue
        st = _USAGE_CACHE.get(path)
        if st and st["size"] == size and st["mtime"] == mtime:
            continue
        if st and size >= st["size"]:
            pos, counts = st["size"], st["counts"]
        else:
            pos, counts = 0, {}  # new file, or it shrank -- start over
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
        except OSError:
            continue
        # Consume only whole lines; a half-written trailing line is left for
        # the next pass rather than being parsed as garbage and lost.
        cut = data.rfind(b"\n") + 1
        _tally_lines(data[:cut].decode("utf-8", "replace").splitlines(), counts)
        _USAGE_CACHE[path] = {"mtime": mtime, "size": pos + cut, "counts": counts}

    days = {}
    for st in _USAGE_CACHE.values():
        for (day, model), tok in st["counts"].items():
            byday = days.setdefault(day, {})
            byday[model] = byday.get(model, 0) + tok
    return days


def render_usage(days):
    lines = ["", c("USAGE", BOLD) + "  "
             + c("observed transcript tokens (input+output) -- an estimate, "
                 "not the Anthropic meter", DIM)]
    # Day keys come from transcript timestamps, which are UTC -- so the day
    # boundary is UTC too. Keep only the window and newest first.
    recent = sorted(days, reverse=True)[:USAGE_DAYS]
    if not recent:
        lines.append(c("  nothing recorded in the last %d days" % USAGE_DAYS, DIM))
        return lines

    today = time.strftime("%Y-%m-%d", time.gmtime())
    rows = []
    for day in recent:
        by_model = days[day]
        # Cloud burn is what counts against the plan; local Ollama models cost
        # nothing, so they show in the breakdown but not the budget math.
        cloud = sum(t for m, t in by_model.items() if m.startswith("claude-"))
        top = sorted(by_model.items(), key=lambda kv: -kv[1])
        detail = ", ".join(
            "%s %s" % (m.replace("claude-", "") if m.startswith("claude-")
                       else m + " (local)", compact(t))
            for m, t in top[:3])
        if len(top) > 3:
            detail += ", +%d more" % (len(top) - 3)
        rows.append((day, cloud, detail))

    wid = max(len(compact(t)) for _, t, _ in rows)
    for day, cloud, detail in rows:
        mark = c(" <- today", GREEN) if day == today else ""
        lines.append("  %s  %s  %s%s" % (
            c(day, BOLD if day == today else DIM),
            compact(cloud).rjust(wid), c(detail, DIM), mark))

    week_total = sum(t for _, t, _ in rows)
    today_cloud = sum(t for m, t in days.get(today, {}).items()
                      if m.startswith("claude-"))
    summary = "today %s  |  %dd %s cloud" % (
        compact(today_cloud), len(rows), compact(week_total))
    budget = parse_budget(os.environ.get(USAGE_BUDGET_ENV))
    lines.append("")
    if budget:
        pct = 100.0 * week_total / float(budget)
        code = (BOLD, RED) if pct >= 80 else ((YELLOW,) if pct >= 50 else (GREEN,))
        lines.append("  " + c(summary, BOLD) + "  "
                     + c("/ %s budget (%.0f%%)" % (compact(budget), pct), *code))
    else:
        lines.append("  " + c(summary, BOLD))
        lines.append("  " + c("set %s (e.g. 60M) to measure against your plan -- "
                              "calibrate the number from /usage once" % USAGE_BUDGET_ENV,
                              DIM))
    return lines


# ---- advisory thresholds ----------------------------------------------------
# A turn costs whatever the context currently holds, so a fat session is
# expensive on every future turn, not once. These are the lines where that stops
# being worth paying.
EXPENSIVE_TOKENS = 150000   # per-turn cost above which a fresh session is cheaper
PARKED_IDLE_HOURS = 2       # untouched this long and still fat == parked
STALE_IDLE_HOURS = 6        # untouched this long and small == just clutter
NEAR_LIMIT_PCT = 80
ADVICE_TASK_WIDTH = 52  # task text in ADVICE; the detail lives on the next line
TYPICAL_BASELINE = 50000    # measured: what a fresh session starts at here
# -----------------------------------------------------------------------------


def advise(workers):
    """Concrete actions, ordered by how many tokens they save."""
    out = []
    ranked = sorted(workers, key=lambda r: -(r["ctx_tokens"] or 0))

    for r in ranked:
        tok = r["ctx_tokens"] or 0
        idle_h = (r["idle_secs"] or 0) / 3600.0
        pct = r["ctx_pct"] or 0
        tag = "%s (pid %d)" % (r["name"], r["pid"])
        # The pid alone does not tell you what you would be closing. The task is
        # what makes the call obvious -- "audit the build scripts" is easy to
        # abandon, "migrate the database" is not.
        task = ascii_safe(r.get("task") or "")
        if len(task) > ADVICE_TASK_WIDTH:
            task = task[:ADVICE_TASK_WIDTH - 3] + "..."
        saving = tok - TYPICAL_BASELINE

        if tok >= EXPENSIVE_TOKENS and idle_h >= PARKED_IDLE_HOURS:
            out.append((saving, c("PARKED+COSTLY", BOLD, RED), tag, task,
                        "idle %.1fh holding %s tokens. Resuming costs that much on the "
                        "FIRST turn. Start a fresh session instead (~%s) and save ~%s per turn."
                        % (idle_h, "{:,}".format(tok), "{:,}".format(TYPICAL_BASELINE),
                           "{:,}".format(saving))))
        elif pct >= NEAR_LIMIT_PCT:
            if r.get("auto_compact", True):
                detail = ("at %.0f%% of its window. Wrap up or /compact before it "
                           "auto-compacts mid-task." % pct)
            else:
                detail = ("at %.0f%% of its window with auto-compact off for this "
                           "session -- there is no safety net here. Wrap up or "
                           "/compact now, or it errors out instead of compacting."
                           % pct)
            out.append((saving, c("NEAR LIMIT", BOLD, YELLOW), tag, task, detail))
        elif tok >= EXPENSIVE_TOKENS:
            out.append((saving, c("EXPENSIVE", YELLOW), tag, task,
                        "every turn now reprocesses %s tokens. Fine to finish the "
                        "current task in; do not start an unrelated one here."
                        % "{:,}".format(tok)))
        elif idle_h >= STALE_IDLE_HOURS:
            out.append((0, c("STALE", DIM), tag, task,
                        "idle %.1fh at %.0f%%. Costs nothing while it sits, but it hides "
                        "the sessions that matter -- close it." % (idle_h, pct)))

    lines = [c("ADVICE", BOLD)]
    if not out:
        lines.append("  nothing to act on -- no parked, oversized, or stale sessions")
        return lines
    for _, label, tag, task, text in sorted(out, key=lambda x: -x[0]):
        head = "  %s  %s" % (label, c(tag, BOLD))
        if task:
            head += "  " + c(task, DIM)
        lines.append(head)
        lines.append("      %s" % text)

    total = sum(r["ctx_tokens"] or 0 for r in workers)
    ideal = TYPICAL_BASELINE * len(workers)
    if total > ideal:
        lines.append("")
        lines.append("  One turn in each of these %d sessions reprocesses %s tokens. The "
                     "same %d sessions started fresh would cost %s -- a %.1fx difference."
                     % (len(workers), c("{:,}".format(total), BOLD), len(workers),
                        "{:,}".format(ideal), total / float(ideal)))
    return lines


def growth(history):
    """Context added across the retained turns -- the TREND cell.

    A signed total, not a shape. Context inside a session only ever rises: it
    falls solely on /compact. So a sparkline *of context* draws the same
    monotonic ramp for every row and a rise/fall arrow points up on every row,
    while the amount separates a session creeping by 2k a window from one
    adding 21k. FLOW next door is a sparkline of throughput, not of context --
    a different series, which is why it is worth a shape and this is not.
    """
    if not history or len(history) < 2:
        return "-"
    d = history[-1] - history[0]
    if d == 0:
        return "="
    return ("+" if d > 0 else "-") + compact(abs(d))


def dur(secs):
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)


def compact(n):
    """484k, not 484,030 -- exact digits cost width and buy nothing here."""
    if n is None:
        return "-"
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%dk" % (n // 1000)
    return str(n)


def spark(hist):
    """ASCII sparkline of recent token flow, newest sample at the right.

    Normalised to the buffer's own max, so it shows the *shape* of activity --
    bursts and quiet stretches -- not absolute volume. Left-padded: history
    grows in from the right as samples arrive.
    """
    if not hist:
        return " " * SPARK_LEN
    mx = max(hist)
    out = []
    for v in hist:
        if v <= 0 or mx <= 0:
            out.append(SPARK_RAMP[0])
        else:
            idx = 1 + int((v / float(mx)) * (len(SPARK_RAMP) - 2))
            out.append(SPARK_RAMP[min(idx, len(SPARK_RAMP) - 1)])
    return "".join(out).rjust(SPARK_LEN)


BAR_WIDTH = 12


def bar(pct):
    """A filled bar for context use.

    Carries what a WIN column used to: a short bar beside a large token count
    reads as "big window, room to spare" without a separate column saying so.
    ASCII only -- block-drawing characters mojibake in the Windows console.
    """
    if pct is None:
        return "[" + " " * BAR_WIDTH + "]"
    filled = int(round(min(pct, 100.0) / 100.0 * BAR_WIDTH))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def bucket(w):
    """Which attention group a session belongs in, most actionable first.

    Ordering is by what it costs to ignore, not by size: a session at 85% is
    about to stop working, a fat parked one bills its whole context on the next
    turn, and everything quiet is noise until it is not.
    """
    pct = w["ctx_pct"] or 0
    tok = w["ctx_tokens"] or 0
    idle = w["idle_secs"]
    if w["ctx_tokens"] is None:
        # No transcript yet -- genuinely unknown, not idle and not working.
        return 3, "STARTING"
    if pct >= NEAR_LIMIT_PCT:
        return 0, "NEAR LIMIT"
    if tok > EXPENSIVE_TOKENS and (idle or 0) > PARKED_IDLE_HOURS * 3600:
        return 1, "PARKED + COSTLY"
    if idle is not None and idle < 60:
        return 2, "WORKING NOW"
    return 4, "QUIET"


BUCKET_COLORS = {0: RED, 1: YELLOW, 2: GREEN, 3: DIM, 4: DIM}


def arrange(workers, expand_quiet=False):
    """Split workers into table rows and the collapsed QUIET tail.

    Pulled out of render() so the cursor and the screen agree by construction.
    Two orderings computed separately would eventually disagree, and the failure
    mode of that disagreement is stopping the wrong session.

    QUIET expands under the cursor because that group is precisely what the
    sweep is for: a session idle for hours is invisible in the collapsed line,
    and unreachable if the cursor cannot enter it.
    """
    tagged = [(bucket(w), w) for w in workers]
    shown = [(b, w) for (b, w) in tagged if b[0] != 4 or expand_quiet]
    quiet = [] if expand_quiet else [w for (b, w) in tagged if b[0] == 4]
    # Within a group, order by what a turn costs. Percentage buries the
    # expensive sessions: 484k tokens on the 1M window reads as a mild 48%,
    # while 140k on a 200k window looks alarming at 70% and costs a third as much.
    shown.sort(key=lambda t: (t[0][0], -(t[1]["ctx_tokens"] or 0)))
    return shown, quiet


def render(workers, sel=None):
    """Table for `workers`. `sel` is an index into arrange()'s shown rows."""
    lines = []
    if not workers:
        lines.append("no live Claude Code sessions")
        return lines

    cols = [
        ("WORKER", lambda r: r["name"]),
        ("MODEL", lambda r: (r["model"] or "-").replace("claude-", "")),
        ("CONTEXT", lambda r: bar(r["ctx_pct"])),
        ("CTX", lambda r: "-" if r["ctx_pct"] is None else "%.0f%%" % r["ctx_pct"]),
        ("TOKENS", lambda r: "-" if not r["ctx_tokens"] else compact(r["ctx_tokens"])),
        # TREND and FLOW are not the same reading twice. TREND is an amount of
        # context added, read back out of the transcript -- so it is populated
        # on the first frame and survives --once and --json. FLOW is a shape
        # sampled while roost runs and starts empty. How much, versus when.
        ("TREND", lambda r: growth(r.get("ctx_history"))),
        ("FLOW", lambda r: r.get("flow") or " " * SPARK_LEN),
        ("IDLE", lambda r: dur(r["idle_secs"])),
    ]

    shown, quiet = arrange(workers, expand_quiet=sel is not None)

    # Widths span every row that will be printed, so groups stay aligned with
    # each other rather than each group forming its own ragged table.
    body = [[f(w) for _, f in cols] for (_, w) in shown]
    head = [h for h, _ in cols]
    wid = [max([len(head[i])] + [len(row[i]) for row in body])
           for i in range(len(cols))]

    if shown:
        lines.append(c("  " + "  ".join(head[i].ljust(wid[i]) for i in range(len(cols)))
                       + "  TASK", BOLD))
    last = None
    for i, ((b, w), row) in enumerate(zip(shown, body)):
        if b[1] != last:
            last = b[1]
            lines.append(c(b[1], BOLD, BUCKET_COLORS.get(b[0], DIM)))
        cells = [style_cell(cols[j][0], row[j].ljust(wid[j]), w) for j in range(len(cols))]
        # Last gate before the terminal. Sanitised at collection too, but this
        # is the boundary that matters: every task string reaches the screen here.
        task = ascii_safe(w.get("task") or "")
        if w.get("task_src") == "prompt" and task:
            task = c(task, DIM)  # not yet named -- this is the raw last prompt
        if not w.get("auto_compact", True):
            # The one WORKERS-row marker that is not about token economics --
            # auto-compact off means NEAR LIMIT has no safety net, so it is
            # called out even on a session nowhere near its window yet.
            tag = c("[no-compact]", BOLD, YELLOW)
            task = tag + " " + task if task else tag
        # The marker is printed whether or not colour is on: over SSH, in a pipe,
        # or on a terminal with no reverse video it is the only thing that says
        # which row x would act on.
        mark = "> " if i == sel else "  "
        line = mark + "  ".join(cells) + "  " + task
        lines.append(highlight(line) if i == sel else line)

    if quiet:
        names = " . ".join(x["name"] for x in quiet[:12])
        if len(quiet) > 12:
            names += " . +%d" % (len(quiet) - 12)
        lines.append("")
        lines.append(c("QUIET (%d)  " % len(quiet), BOLD, DIM) + c(names, DIM))

    # Totals, because the per-row numbers do not add up in your head. The one
    # that matters is context held across the fleet: it is what a sweep would
    # reclaim, and until now it was only answerable after the fact, from the
    # stop log. Percentages are deliberately not totalled -- they are fractions
    # of different windows, so their sum means nothing.
    held = sum(r["ctx_tokens"] or 0 for r in workers)
    added = sum(h[-1] - h[0] for h in
                (r.get("ctx_history") or [] for r in workers) if len(h) >= 2)
    near = sum(1 for r in workers if (r["ctx_pct"] or 0) >= NEAR_LIMIT_PCT)

    models = sorted(set(r["model"] for r in workers if r["model"] and r["model"] != "-"))
    summary = "%d worker(s)  |  %s held" % (len(workers), compact(held))
    if added:
        summary += "  |  %s last %d turns" % (compact(added), HISTORY_TURNS)
    if near:
        summary += "  |  " + c("%d near limit" % near, YELLOW)
    if models:
        summary += "  |  " + ", ".join(m.replace("claude-", "") for m in models)
    lines.append("")
    # Re-arm BOLD after the inner colour's RESET, for the reason highlight()
    # spells out: a RESET clears the line's bold along with the colour, so a
    # naive wrap would leave everything after "near limit" unbolded. A no-op
    # when COLOR is off, since then there are no escapes to replace.
    lines.append(c(summary.replace(RESET, RESET + BOLD), BOLD))
    return lines


def render_infra(infra):
    """One horizontal line: it is always present and rarely the thing you need."""
    parts = []
    for s in infra:
        mark = c("up", GREEN) if s["up"] else c("DOWN", BOLD, RED)
        extra = ""
        if s["up"] and s["detail"]:
            extra = " " + c(s["detail"], CYAN)
        parts.append("%s:%d %s%s" % (c(s["name"], BOLD), s["port"], mark, extra))
    return [c("INFRA  ", BOLD) + "   ".join(parts), ""]


def _pct_color(pct):
    if pct is None:
        return DIM
    if pct >= 80:
        return (BOLD, RED)
    if pct >= 50:
        return (YELLOW,)
    return (GREEN,)


def render_usage_caps(usage):
    """Real Anthropic caps, one line, always visible next to INFRA -- same
    "quiet until it matters" weight, refreshed on a 2h cadence rather than
    every frame, since the source is a scrape cache, not a live probe."""
    label = c("CAPS   ", BOLD)
    if usage is None:
        return [label + c("no data yet -- claude-usage-scrape task hasn't run "
                           "(see claude-usage/README.md)", DIM), ""]

    caps = usage.get("caps") or {}
    age = usage.get("age_secs")
    stale = age is not None and age > USAGE_STALE_SECS

    def cell(key, tag):
        c_ = caps.get(key) or {}
        if not c_.get("visible"):
            return None
        pct = c_.get("pct")
        if pct is None:
            return None
        return "%s: %s" % (tag, c(str(pct) + "%", *_pct_color(pct)))

    parts = [p for p in (
        cell("five_hour", "5h"),
        cell("weekly_all_models", "Weekly"),
        cell("weekly_sonnet", "Sonnet"),
        cell("fable5_max", "Fable5"),
    ) if p]

    if not parts:
        return [label + c("cache present but no caps parsed", DIM), ""]

    if age is None:
        age_txt = "age unknown"
    else:
        age_txt = "%dm ago" % (age / 60) if age < 3600 else "%.1fh ago" % (age / 3600)
    age_style = (BOLD, RED) if stale else (DIM,)
    suffix = c("  (stale, %s)" % age_txt, *age_style) if stale else c("  (%s)" % age_txt, DIM)

    return [label + "  ".join(parts) + suffix, ""]


MODEL_COLORS = {"opus": MAGENTA, "sonnet": BLUE, "fable": CYAN, "haiku": GREEN}


def style_cell(header, text, row):
    """Colour one padded cell. Padding is already applied, so widths are fixed."""
    if header == "CTX":
        pct = row["ctx_pct"]
        if pct is None:
            return c(text, DIM)
        if pct >= 80:
            return c(text, BOLD, RED)
        if pct >= 50:
            return c(text, YELLOW)
        return c(text, GREEN)
    if header == "TREND":
        # Dim, not coloured by size. Growth is normal -- every working session
        # grows, and colouring it would put warning colour on healthy rows and
        # compete with CTX, which is the column that actually says "act on me".
        return c(text, DIM)
    if header == "MODEL":
        for family, code in MODEL_COLORS.items():
            if family in (row["model"] or ""):
                return c(text, code)
        return c(text, DIM)
    if header == "IDLE":
        # An hour idle is the signal the maintenance sweep looks for.
        if row["idle_secs"] is None:
            return c(text, DIM)
        if row["idle_secs"] >= 3600:
            return c(text, DIM)
        if row["idle_secs"] <= 60:
            return c(text, GREEN)
        return text
    if header == "NAME":
        return c(text, BOLD)
    if header == "FLOW":
        return c(text, CYAN)
    if header in ("TOKENS", "WIN", "PID"):
        return c(text, DIM)
    return text


def render_subagents(agents):
    """Subagents are the work a session farmed out -- and they are invisible in
    any pid-based view, since they share the parent's process."""
    lines = ["", c("SUBAGENTS", BOLD)]
    if not agents:
        lines.append(c("  none running", DIM))
        return lines

    cols = [
        ("STATE", lambda r: r["state"]),
        # The agent type is the readable name, but the parent only records it in
        # the tool result -- a still-running agent has no type yet, so the hex id
        # is kept as a suffix (and the whole label while running) to stay unique.
        ("AGENT", lambda r: ("%s/%s" % (r["agent_type"], r["agent_id"][:5]))
            if r["agent_type"] else r["agent_id"][:10]),
        ("MODEL", lambda r: (r["model"] or "-").replace("claude-", "")),
        # Absolute over window, not a bare percentage: 48k/200k says both how
        # much is loaded and how much room is left. The window label is inferred
        # (see WINDOW_TIERS); colour still follows ctx_pct.
        ("CTX", lambda r: "-" if not r["ctx_tokens"]
            else "%s/%s" % (compact(r["ctx_tokens"]), r["window"])),
        ("IDLE", lambda r: dur(r["idle_secs"])),
        ("TASK", lambda r: r["task"]),
    ]
    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in agents]
    w = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines.append("  " + c("  ".join(table[0][i].ljust(w[i]) for i in range(len(cols))), BOLD))
    for row, r in zip(table[1:], agents):
        cells = []
        for i, (header, _) in enumerate(cols):
            txt = row[i].ljust(w[i])
            if header == "STATE":
                code = {"working": GREEN, "idle": YELLOW}.get(r["state"], DIM)
                cells.append(c(txt, BOLD, code))
            elif header == "MODEL":
                cells.append(style_cell("MODEL", txt, r))
            elif header == "CTX":
                cells.append(style_cell("CTX", txt, r))
            elif header == "AGENT":
                cells.append(c(txt, DIM))
            else:
                cells.append(txt)
        lines.append("  " + "  ".join(cells))

    working = sum(1 for r in agents if r["state"] == "working")
    lines.append("  " + c("%d subagent(s), %d working" % (len(agents), working), DIM))
    return lines


def render_models(models):
    """The INFRA line only ever shows what is resident in VRAM right now -- a
    model that is installed but idle drops out of it entirely. This is the
    full inventory: everything `ollama list` knows about, with residency and
    VRAM called out for whichever happen to be loaded."""
    lines = ["", c("LOCAL MODELS", BOLD)]
    if not models:
        lines.append(c("  none installed (or ollama not running)", DIM))
        return lines

    cols = [
        ("MODEL", lambda m: m["name"]),
        ("DISK", lambda m: "%.1f GB" % m["disk_gb"]),
        ("STATE", lambda m: "resident" if m["resident"] else "unloaded"),
        ("VRAM", lambda m: "-" if m["vram_gb"] is None else "%.1f GB" % m["vram_gb"]),
        ("UNLOADS IN", lambda m: "-" if m["expires_secs"] is None else dur(m["expires_secs"])),
    ]
    table = [[h for h, _ in cols]] + [[f(m) for _, f in cols] for m in models]
    w = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines.append("  " + c("  ".join(table[0][i].ljust(w[i]) for i in range(len(cols))), BOLD))
    for row, m in zip(table[1:], models):
        cells = []
        for i, (header, _) in enumerate(cols):
            txt = row[i].ljust(w[i])
            if header == "MODEL":
                cells.append(c(txt, BOLD))
            elif header == "STATE":
                cells.append(c(txt, BOLD, GREEN) if m["resident"] else c(txt, DIM))
            else:
                cells.append(c(txt, DIM))
        lines.append("  " + "  ".join(cells))

    resident = sum(1 for m in models if m["resident"])
    lines.append("  " + c("%d installed, %d resident" % (len(models), resident), DIM))
    return lines


# name, key, what it shows. Not a keybinding reference -- the footer hint
# already has the keys -- just what each screen on the display means. roost
# is small enough that this list is the whole manual.
HELP_SCREENS = (
    ("INFRA", None,
     "ollama / litellm / openwebui: up or down, plus what's resident in Ollama's VRAM right now."),
    ("WORKERS", None,
     "every live Claude Code session: model, context window used, idle time, current task. "
     "TREND is how much context the session added over its last few turns, read out of the "
     "transcript, so it is filled in on the first frame. FLOW is a sparkline of recent token "
     "throughput -- '.' is a quiet sample, the ramp is "
     "activity; history starts when roost starts. QUIET collapses idle sessions to one line; "
     "raise the cursor to expand it."),
    ("SUBAGENTS", "s",
     "work a session farmed out. Invisible in any pid-based view, since a subagent shares its "
     "parent's process rather than running as one of its own. AGENT shows the agent's type once "
     "it finishes (hex id while running); CTX is tokens over the inferred window."),
    ("ADVICE", "a",
     "concrete actions, ranked by how many tokens each would save -- which sessions are "
     "expensive to resume, near their context limit, or just idle clutter."),
    ("LOCAL MODELS", "m",
     "everything Ollama has installed, not just what's resident in VRAM -- disk size, "
     "residency, and time until an idle model unloads."),
    ("USAGE", "u",
     "tokens per day per model over the last week, tallied from the transcripts on disk. "
     "An estimate of burn, not the real Anthropic meter; set ROOST_WEEKLY_BUDGET to see "
     "it as a share of your plan. Local (non claude-*) models are flagged and excluded "
     "from the budget math. First open scans a week of transcripts and can pause for a "
     "moment; after that it reads only what was appended."),
    ("GATEWAY", "g",
     "LiteLLM liveliness plus batch-run progress. The gateway is DB-less so it keeps no "
     "request history -- progress is derived from the batch pipeline's own output files "
     "(one JSON per finished item), the job queue dirs, and a best-effort read of "
     "proxy.log. Green rows are actively writing."),
    ("REMOTE", "r",
     "other machines' roost, over ssh. One row per host in ROOST_REMOTES: workers, "
     "resident models, batch progress, job queue. Fetched on a background thread and "
     "cached, so an unreachable host shows its last good row with an age instead of "
     "hanging the display."),
)


def render_help():
    """What each screen means, not how to drive it -- the footer hint already
    lists the keys, and roost has few enough screens that this fits on one page."""
    lines = ["", c("HELP", BOLD)]
    for name, key, text in HELP_SCREENS:
        label = "%s (%s)" % (name, key) if key else name
        lines.append("  " + c(label, BOLD))
        lines.append("      " + c(text, DIM))
    lines.append("")
    lines.append("  " + c("interactive mode (i) arms the cursor: j/k select, x stop, "
                          "y copy sessionId, esc deselect.", DIM))
    return lines


def frame(view=None, sel=None):
    """Returns (lines, rows, sel). `view` is the open panel: "agents",
    "models", "advice", "usage", "help", or None for the bare worker table.

    `rows` is what the cursor indexes, handed back so the key handler acts on the
    frame the user was actually looking at. `sel` comes back clamped: sessions
    exit between frames, and a cursor left pointing past the end would silently
    address nothing.
    """
    workers = collect_workers()
    shown, _ = arrange(workers, expand_quiet=sel is not None)
    rows = [w for _, w in shown]
    if sel is not None:
        sel = min(sel, len(rows) - 1) if rows else None
    # Infra leads because it is a constant: one quiet line you skim past, which
    # is exactly the weight it deserves until something turns red.
    lines = render_infra(collect_infra()) + render(workers, sel)
    if view == "agents":
        live_sids = set(w["session_id"] for w in workers if w.get("session_id"))
        lines.extend(render_subagents(collect_subagents(live_sids)))
    elif view == "models":
        lines.extend(render_models(collect_local_models()))
    elif view == "usage":
        lines.extend(render_usage(collect_usage()))
    elif view == "gateway":
        lines.extend(render_gateway(collect_gateway()))
    elif view == "remote":
        lines.extend(render_remote(collect_remote()))
    elif view == "help":
        lines.extend(render_help())
    elif view == "advice":
        lines.append("")
        lines.extend(advise(workers))
    prune_caches()
    return lines, rows, sel


def prune_caches():
    """Evict cache entries whose session or agent vanished since the last frame.

    Every live path and agent re-registers itself each tick, so anything left
    over is history -- without this the caches grow for as long as the dashboard
    stays open, which is days.
    """
    for cache in (_SCAN_CACHE, _HARVEST_POS):
        for k in [k for k in cache if k not in _SEEN_PATHS]:
            del cache[k]
    for cache in (_AGENT_META, _AGENT_LABEL):
        for k in [k for k in cache if k not in _SEEN_AGENTS]:
            del cache[k]
    _SEEN_PATHS.clear()
    _SEEN_AGENTS.clear()


# (handle, original mode) when enable_vt() changed the console, else None.
# Conhost keeps a mutated mode after the process exits, so it must be put back.
_VT_ORIGINAL = None


def enable_vt():
    """Turn on ANSI escape handling. Windows consoles have it off by default.

    Without it the cursor-home and erase sequences are ignored, so every frame is
    appended below the last instead of overwriting it -- the screen pages away.
    """
    global _VT_ORIGINAL
    if os.name != "nt":
        return True
    try:
        import ctypes

        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # redirected, or not attached to a console at all
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        if k.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            _VT_ORIGINAL = (handle, mode.value)
            return True
        return False
    except Exception:
        return False


def restore_vt():
    """Put the console mode back exactly as enable_vt() found it."""
    if _VT_ORIGINAL is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleMode(_VT_ORIGINAL[0], _VT_ORIGINAL[1])
    except Exception:
        pass


class KeyReader(object):
    """Non-blocking single keypresses, or a no-op when stdin is not a terminal.

    Used so the interval sleep stays interruptible: space repaints immediately
    instead of waiting out the remainder of the tick.
    """

    def __init__(self):
        self.enabled = False
        self._fd = None
        self._saved = None

    def __enter__(self):
        try:
            if not sys.stdin.isatty():
                return self
        except (ValueError, AttributeError):
            return self
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401

                self.enabled = True
            except ImportError:
                pass
            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.enabled = True
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            try:
                import termios

                # TCSAFLUSH, not TCSADRAIN: discard queued input (the tail of an
                # arrow sequence, keys typed during a slow frame) instead of
                # delivering it to the shell prompt after exit.
                termios.tcsetattr(self._fd, termios.TCSAFLUSH, self._saved)
            except Exception:
                pass
        return False

    def get(self, timeout):
        """One key within `timeout` seconds, else None.

        Returns a single character, or one of the names "UP", "DOWN", "ESC" --
        arrows are multi-byte on both platforms and the caller should not have to
        know either encoding.
        """
        if not self.enabled:
            time.sleep(timeout)
            return None
        if os.name == "nt":
            import msvcrt

            end = time.time() + timeout
            while time.time() < end:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    # Arrows and function keys arrive as a two-char sequence.
                    if ch in ("\x00", "\xe0"):
                        return {"H": "UP", "P": "DOWN"}.get(msvcrt.getwch())
                    return "ESC" if ch == "\x1b" else ch
                time.sleep(0.02)
            return None
        import select

        # os.read on the raw fd, never sys.stdin.read: the buffered TextIO can
        # slurp several bytes into user space, after which select() on the fd
        # reports "not ready" while input sits in the buffer -- arrow tails then
        # surface as stray characters, or spill to the shell after exit.
        def read1():
            b = os.read(self._fd, 1)
            return b.decode("utf-8", "replace") if b else ""

        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        ch = read1()
        if ch != "\x1b":
            return ch
        # A bare Esc and the start of an arrow sequence are the same byte. The
        # rest of a real CSI arrives in the same burst, so nothing further within
        # a beat means the user pressed Esc.
        r, _, _ = select.select([self._fd], [], [], 0.05)
        if not r or read1() != "[":
            return "ESC"
        return {"A": "UP", "B": "DOWN"}.get(read1())


def term_size():
    size = shutil.get_terminal_size((150, 40))
    # Redirected output reports 0x0, which would otherwise clip every line to nothing.
    cols = size.columns if size.columns >= 20 else 150
    rows = size.lines if size.lines >= 5 else 40
    return cols, rows


def paint(lines, vt):
    """Redraw in place.

    Clipped to the window in both directions on purpose: a line that wraps, or a
    frame taller than the terminal, scrolls the display -- which looks identical
    to a clear that never happened.
    """
    cols, rows = term_size()
    body = [clip_ansi(ln, cols - 1) for ln in lines]
    # Say so when the frame does not fit. Silent truncation is how a confirmation
    # prompt and an ADVICE panel both went missing without appearing to fail --
    # the screen looked complete, so nothing suggested there was more below it.
    if len(body) > rows - 1:
        hidden = len(body) - (rows - 2)
        body = body[: rows - 2] + [clip_ansi(
            c("... %d more line(s) below -- taller window, or s/a/m/u/g/r/h to close a panel"
              % hidden, DIM), cols - 1)]

    # Version, bottom-right. Stamped onto whatever the last visible line turns
    # out to be -- including the overflow notice above -- so it cannot itself be
    # the thing that gets clipped off. INFRA is not a footer to hang it on: it
    # leads the frame. Padded by visible_len, since escape bytes are not columns
    # and len() would push it off the right edge by the number of colour codes
    # in the line. Dropped rather than wrapped when there is no room: a wrapped
    # line scrolls the display, which looks identical to a clear that never ran.
    if body:
        stamp = c("v" + __version__, DIM)
        room = cols - 1 - visible_len(body[-1]) - visible_len(stamp)
        if room >= 2:
            body[-1] += " " * room + stamp
    if vt:
        # Home, overwrite each line erasing its old tail, then wipe any rows left
        # over from a taller previous frame. Flicker-free, unlike a full clear.
        sys.stdout.write("\033[H" + "".join(ln + "\033[K\n" for ln in body) + "\033[J")
    else:
        os.system("cls" if os.name == "nt" else "clear")
        sys.stdout.write("\n".join(body) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="Live Claude workers and local infra on one screen.")
    ap.add_argument("-w", "--watch", nargs="?", const=REFRESH_SECONDS, type=float,
                    metavar="SECS",
                    help="refresh interval in seconds (default %g, set by REFRESH_SECONDS)"
                         % REFRESH_SECONDS)
    ap.add_argument("-1", "--once", action="store_true",
                    help="print a single frame and exit (live is the default)")
    ap.add_argument("--version", action="version", version="roost " + __version__)
    ap.add_argument("--json", action="store_true", help="emit records as JSON and exit")
    ap.add_argument("--no-color", action="store_true", help="disable colour output")
    ap.add_argument("--advise", action="store_true",
                    help="start with the ADVICE panel open (toggle live with 'a')")
    ap.add_argument("--no-agents", action="store_true",
                    help="start with the SUBAGENTS panel closed (toggle live with 's')")
    ap.add_argument("--models", action="store_true",
                    help="start with the LOCAL MODELS panel open (toggle live with 'm')")
    ap.add_argument("--usage", action="store_true",
                    help="start with the USAGE panel open (toggle live with 'u'); "
                         "set %s (e.g. 60M) to show weekly burn against a budget"
                         % USAGE_BUDGET_ENV)
    ap.add_argument("--gateway", action="store_true",
                    help="start with the GATEWAY panel open (toggle live with 'g')")
    ap.add_argument("--remote", action="store_true",
                    help="start with the REMOTE panel open (toggle live with 'r'); "
                         "hosts come from %s (comma-separated ssh aliases)" % REMOTES_ENV)
    ap.add_argument("--interactive", action="store_true",
                    help="start with interactive mode armed -- cursor, x/y, and the "
                         "EXPERIMENTAL tag (default off; toggle live with 'i')")
    ap.add_argument("--no-log", action="store_true",
                    help="do not record stopped sessions to %s" % LOG_PATH)
    ap.epilog = (
        "keys while running:  space = refresh now   a = advice panel   "
        "s = subagents panel   m = local models panel   u = usage panel   "
        "g = gateway panel   r = remote panel   "
        "h or ? = what am I looking at   "
        "i = arm interactive mode   q = quit\n"
        "interactive mode (armed with i):  j/k or arrows move a cursor   "
        "x = stop the session (confirms)   y = copy its sessionId   esc = deselect")
    args = ap.parse_args()

    global LOGGING
    LOGGING = not args.no_log

    if args.json:
        print(json.dumps({
            "workers": collect_workers(),
            "infra": collect_infra(),
            "usage_caps": collect_usage_caps(),
            "local_models": collect_local_models(),
            "gateway": collect_gateway(),
        }, indent=2))
        return

    # Die by unwinding, not by default disposition: a plain kill would skip the
    # KeyReader __exit__ and the cursor/console restore below, leaving the tty
    # in cbreak/no-echo with the cursor hidden. SystemExit runs both.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))

    vt = enable_vt()

    global COLOR
    # Colour needs escape support and a real terminal. NO_COLOR is the community
    # convention (https://no-color.org) and costs nothing to honour.
    COLOR = (
        vt
        and not args.no_color
        and not os.environ.get("NO_COLOR")
        and sys.stdout.isatty()
    )

    # Panels are toggled live rather than fixed at launch: on a short terminal all
    # of them at once overflow the window, and what you want to see changes.
    # One panel at a time. They used to be independent toggles, which meant
    # opening ADVICE while SUBAGENTS was up pushed it past the bottom of the
    # terminal -- you had to close the other one first to see the one you asked
    # for. Flipping is what "show me the advice" actually means.
    if args.advise:
        view = "advice"
    elif args.models:
        view = "models"
    elif args.usage:
        view = "usage"
    elif args.gateway:
        view = "gateway"
    elif args.remote:
        view = "remote"
    elif args.no_agents:
        view = None
    else:
        view = "agents"

    # Live is the default, as with top/htop -- `-h` is argparse's help and exits,
    # so keys have nothing to act on there. A single frame is opt-in.
    if args.once:
        print("\n".join(frame(view)[0]))
        return
    interval = args.watch if args.watch else REFRESH_SECONDS

    if vt:
        sys.stdout.write("\033[2J\033[?25l")  # one clear up front, then hide the cursor

    # Off by default: this is the gate on the half that can end a process, and
    # arming it is one deliberate keypress rather than "having a terminal."
    interactive = bool(args.interactive)
    sel = None      # cursor row index, or None when there is no cursor
    pending = None  # the worker row awaiting a y/n answer
    note = None     # result of the last action, cleared by the next keypress
    try:
        with KeyReader() as keys:
            while True:
                if not keys.enabled:
                    hint = "Ctrl-C to stop"
                else:
                    # One hint for every state. Its text never changes when
                    # interactive is armed or a cursor is raised -- only the
                    # colours do -- so the header cannot reflow underfoot
                    # ("off" is even padded to ARMED's width). The armed state
                    # is spelled out, not just tinted: a lone green "i" reads
                    # as decoration, and the one mode that can end a process
                    # should never be ambiguous. Cursor keys sit dimmed until
                    # they can do something.
                    if interactive:
                        i_tag = c("i", BOLD, GREEN) + " interactive " + c("ARMED", BOLD, GREEN)
                        cur_tag = "j/k x y esc"
                    else:
                        i_tag = c("i", BOLD) + " interactive " + c("off  ", DIM)
                        cur_tag = c("j/k x y esc", DIM)
                    a_tag = c("a", BOLD, GREEN) if view == "advice" else "a"
                    s_tag = c("s", BOLD, GREEN) if view == "agents" else "s"
                    m_tag = c("m", BOLD, GREEN) if view == "models" else "m"
                    u_tag = c("u", BOLD, GREEN) if view == "usage" else "u"
                    g_tag = c("g", BOLD, GREEN) if view == "gateway" else "g"
                    r_tag = c("r", BOLD, GREEN) if view == "remote" else "r"
                    h_tag = c("h", BOLD, GREEN) if view == "help" else "h"
                    hint = ("space refresh | %s | %s | %s advice | %s agents | "
                            "%s models | %s usage | %s gateway | %s remote | "
                            "%s help | q quit") % (
                        i_tag, cur_tag, a_tag, s_tag, m_tag, u_tag, g_tag,
                        r_tag, h_tag)
                lines, rows, sel = frame(view, sel)
                # A session can exit while its confirmation is on screen. Matching
                # on pid rather than on the row dict is what makes that detectable:
                # every frame rebuilds the dicts, so identity and equality both
                # fail on rows that are in fact the same session.
                if pending and not any(r["pid"] == pending["pid"] for r in rows):
                    pending, note = None, c("that session exited on its own", DIM)

                # The status line lives in the header, above the table, and the
                # blank placeholder keeps it there so nothing shifts when it
                # fills. It used to be appended under the table, where paint()
                # clipped it away: 24 sessions and their subagents make a frame
                # taller than the terminal, so the confirmation was invisible
                # precisely when there was most to act on, and the next keypress
                # cancelled a prompt that had never been seen.
                if pending:
                    status = c("stop %s (pid %d)?   y = yes, any other key = no" % (
                        pending["name"], pending["pid"]), BOLD, RED)
                else:
                    status = note or ""
                title = (c("roost", BOLD) + "  " + c(socket.gethostname(), CYAN)
                         + "  " + time.strftime("%H:%M:%S") + "   " + c(hint, DIM))
                if keys.enabled and interactive:
                    # Only while interactive mode is armed, because that is the
                    # half that can end a process. Reading the dashboard has
                    # never been the risky part. Pinned top-right so it sits
                    # above the table rather than anywhere the frame can clip
                    # it away.
                    tag = c(" EXPERIMENTAL ", BOLD, REVERSE, YELLOW)
                    pad = term_size()[0] - 1 - visible_len(title) - visible_len(tag)
                    title += " " * max(1, pad) + tag
                header = [title, status, ""]
                paint(header + lines, vt)

                # Sleep in slices so a keypress lands within ~0.2s rather than at
                # the end of the tick.
                deadline = time.time() + interval
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    key = keys.get(min(0.2, remaining))
                    if key is None:
                        continue
                    note = None

                    # The confirmation swallows every key: only an explicit y
                    # stops a session, and q here cancels rather than quitting so
                    # that a reflexive quit cannot be read as consent.
                    if pending is not None:
                        if key in ("y", "Y"):
                            err = terminate(pending["pid"])
                            log_action("stop", pending, ok=err is None, detail=err or "")
                            note = c("stopped %s (pid %d)" % (
                                pending["name"], pending["pid"]), GREEN) if err is None \
                                else c(err, BOLD, RED)
                        else:
                            note = c("cancelled", DIM)
                        pending = None
                        break

                    if key in ("q", "Q", "\x03"):
                        return
                    if key == " ":
                        break  # repaint now
                    if key == "ESC":
                        sel = None
                        break
                    if key in ("a", "A"):
                        view = None if view == "advice" else "advice"
                        break  # repaint immediately, do not wait out the tick
                    if key in ("s", "S"):
                        view = None if view == "agents" else "agents"
                        break
                    if key in ("m", "M"):
                        view = None if view == "models" else "models"
                        break
                    if key in ("u", "U"):
                        view = None if view == "usage" else "usage"
                        break
                    if key in ("g", "G"):
                        view = None if view == "gateway" else "gateway"
                        break
                    if key in ("r", "R"):
                        view = None if view == "remote" else "remote"
                        break
                    if key in ("h", "H", "?"):
                        view = None if view == "help" else "help"
                        break
                    if key in ("i", "I"):
                        # The one key that arms the whole risky half at once --
                        # cursor, x/y, and the EXPERIMENTAL tag all come alive
                        # together, so there is exactly one thing to remember
                        # before a keypress can end a process.
                        interactive = not interactive
                        if interactive:
                            note = c("interactive armed -- j/k select, x stop, y yank", BOLD, YELLOW)
                        else:
                            # Drop the cursor rather than leave it parked: a
                            # stale sel would resurface on re-arming, pointing
                            # at whatever row happens to occupy that index by
                            # then, not the one it was left on.
                            sel = None
                            note = c("interactive off -- view only", DIM)
                        break
                    if key in ("j", "J", "DOWN"):
                        if not interactive:
                            note = c("press i to arm interactive mode first", YELLOW)
                        else:
                            # Unbounded on purpose -- frame() clamps against the row
                            # count it actually rendered, which is the only correct one.
                            sel = 0 if sel is None else sel + 1
                        break
                    if key in ("k", "K", "UP"):
                        if not interactive:
                            note = c("press i to arm interactive mode first", YELLOW)
                        else:
                            sel = 0 if sel is None else max(0, sel - 1)
                        break
                    if key in ("x", "X", "y", "Y"):
                        # All three need interactive armed, and x/y also need a
                        # row. Saying so beats doing nothing: a key that silently
                        # no-ops is indistinguishable from a broken one.
                        if not interactive:
                            note = c("press i to arm interactive mode first", YELLOW)
                        elif sel is None or not rows:
                            note = c("select a row first -- j/k or the arrow keys", YELLOW)
                        elif key in ("x", "X"):
                            # Captured from the frame on screen, not re-resolved later.
                            pending = rows[sel]
                        else:
                            w = rows[sel]
                            note = c("copied %s -- claude --resume <paste>" % w["name"], GREEN) \
                                if to_clipboard(w["session_id"]) \
                                else c("no clipboard helper (%s not found)" % CLIP_CMD[0], YELLOW)
                        break
    except KeyboardInterrupt:
        pass
    finally:
        if vt:
            sys.stdout.write("\033[?25h\n")  # restore the cursor on the way out
            sys.stdout.flush()
        restore_vt()


if __name__ == "__main__":
    main()
