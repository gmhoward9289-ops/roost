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
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.4"

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"

# ---- config ----------------------------------------------------------------
# Seconds between automatic repaints. Override per-run with `-w N`; space forces
# an immediate repaint regardless.
REFRESH_SECONDS = 1.0

# Only the tail of a transcript matters and they grow to hundreds of MB.
TAIL_BYTES = 262144

# A subagent whose parent has exited is still worth seeing for a while -- usually
# it is the run that just finished. Older than this and it is history.
AGENT_RECENT_SECS = 3600

# A subagent counts as working if its transcript was written this recently.
AGENT_ACTIVE_SECS = 30

# Every session roost stops gets one JSON line here. Same shape and the same cap
# as the hook logs next to it, so the same one-liners read all of them.
LOG_PATH = HOME / ".claude" / "logs" / "roost.jsonl"
LOG_MAX_LINES = 5000
# -----------------------------------------------------------------------------

# Set in main(); --no-log turns it off.
LOGGING = True

# agentId -> {description, status, model}, harvested from the parent transcript.
# Descriptions never change, so this is filled once and never invalidated.
_AGENT_META = {}
_AGENT_LABEL = {}

# path -> (mtime, parsed result). At a 1s refresh, re-reading 256 KB from every
# transcript each tick is megabytes of disk per second for no new information --
# a transcript that has not been written to cannot have a new model or usage.
_SCAN_CACHE = {}

# Nothing in the transcript records which context window a session was opened with,
# so it is inferred: the smallest standard tier the observed usage still fits in.
# A session on the 1M window read 482k cache tokens in one call here -- scoring that
# against 200k is what produced a nonsense "242%". The chosen tier is displayed
# rather than assumed silently.
WINDOW_TIERS = ((200000, "200k"), (1000000, "1M"))


def window_for(tokens):
    for size, label in WINDOW_TIERS:
        if tokens <= size:
            return size, label
    return WINDOW_TIERS[-1]

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
    """Model and context from the newest assistant turn that carries usage."""
    out = {"model": None, "ctx_tokens": None, "last_write": None,
           "title": None, "prompt": None}
    if not path:
        return out
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

        if out["model"] is None:
            msg = d.get("message") or {}
            usage = msg.get("usage") or {}
            if usage:
                out["model"] = msg.get("model")
                out["ctx_tokens"] = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                )

        if out["model"] and out["title"] and out["prompt"]:
            break

    if out["last_write"] is not None:
        _SCAN_CACHE[path] = (out["last_write"], dict(out))
    return out


def harvest_agent_meta(parent_transcript):
    """Pull subagent description/status out of the parent's tool results.

    A subagent's own transcript never states what it was asked to do in short
    form -- only the parent's `toolUseResult` carries `description`, `status` and
    `resolvedModel`, keyed by agentId. Scanning the tail is enough for anything
    recent, and results are cached permanently since they never change.
    """
    if not parent_transcript:
        return
    for line in read_tail(parent_transcript):
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
        if aid and aid not in _AGENT_META:
            _AGENT_META[aid] = {
                "description": r.get("description") or "",
                "status": r.get("status") or "",
                "model": r.get("resolvedModel") or "",
            }


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

        if agent_id not in _AGENT_META:
            harvest_agent_meta(transcript_for(parent_sid))
        meta = _AGENT_META.get(agent_id) or {}

        label = ascii_safe(
            meta.get("description") or agent_first_prompt(path, agent_id) or "-")
        model = info["model"] or meta.get("model") or "-"
        pct = None
        win_label = "-"
        if info["ctx_tokens"]:
            window, win_label = window_for(info["ctx_tokens"])
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
            window, win_label = window_for(info["ctx_tokens"])
            pct = 100.0 * info["ctx_tokens"] / float(window)
        idle = None
        if info["last_write"]:
            idle = now - info["last_write"]
        started = s.get("startedAt")
        age = (now - started / 1000.0) if isinstance(started, (int, float)) else None
        rows.append({
            "name": s.get("name") or "-",
            "pid": pid,
            "session_id": sid,
            "cwd": s.get("cwd") or "",
            "project": Path(s.get("cwd") or ".").name or "-",
            "model": info["model"] or "-",
            "ctx_tokens": info["ctx_tokens"],
            "ctx_pct": pct,
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
            out.append((saving, c("NEAR LIMIT", BOLD, YELLOW), tag, task,
                        "at %.0f%% of its window. Wrap up or /compact before it "
                        "auto-compacts mid-task." % pct))
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

    models = sorted(set(r["model"] for r in workers if r["model"] and r["model"] != "-"))
    summary = "%d worker(s)" % len(workers)
    if models:
        summary += "  |  " + ", ".join(m.replace("claude-", "") for m in models)
    lines.append("")
    lines.append(c(summary, BOLD))
    return lines


def render_infra(infra):
    """One horizontal line: it is always present and rarely the thing you need."""
    parts = []
    for s in infra:
        mark = c("up", GREEN) if s["up"] else c("DOWN", BOLD, RED)
        extra = ""
        if s["up"] and s["detail"] and s["detail"] != "no model resident":
            extra = " " + c(s["detail"], CYAN)
        parts.append("%s:%d %s%s" % (c(s["name"], BOLD), s["port"], mark, extra))
    return [c("INFRA  ", BOLD) + "   ".join(parts), ""]


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
        ("AGENT", lambda r: r["agent_id"][:10]),
        ("MODEL", lambda r: (r["model"] or "-").replace("claude-", "")),
        ("CTX", lambda r: "-" if r["ctx_pct"] is None else "%.0f%%" % r["ctx_pct"]),
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


def frame(with_advice=False, with_agents=True, sel=None):
    """Returns (lines, rows, sel).

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
    if with_agents:
        live_sids = set(w["session_id"] for w in workers if w.get("session_id"))
        lines.extend(render_subagents(collect_subagents(live_sids)))
    if with_advice:
        lines.append("")
        lines.extend(advise(workers))
    return lines, rows, sel


def enable_vt():
    """Turn on ANSI escape handling. Windows consoles have it off by default.

    Without it the cursor-home and erase sequences are ignored, so every frame is
    appended below the last instead of overwriting it -- the screen pages away.
    """
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
        return bool(k.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


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

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
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

        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        # A bare Esc and the start of an arrow sequence are the same byte. The
        # rest of a real CSI arrives in the same burst, so nothing further within
        # a beat means the user pressed Esc.
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not r or sys.stdin.read(1) != "[":
            return "ESC"
        return {"A": "UP", "B": "DOWN"}.get(sys.stdin.read(1))


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
            c("... %d more line(s) below -- taller window, or s/a to close a panel"
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
    ap.add_argument("--interactive", action="store_true",
                    help="start with interactive mode armed -- cursor, x/y, and the "
                         "EXPERIMENTAL tag (default off; toggle live with 'i')")
    ap.add_argument("--no-log", action="store_true",
                    help="do not record stopped sessions to %s" % LOG_PATH)
    ap.epilog = (
        "keys while running:  space = refresh now   a = advice panel   "
        "s = subagents panel   i = arm interactive mode   q = quit\n"
        "interactive mode (armed with i):  j/k or arrows move a cursor   "
        "x = stop the session (confirms)   y = copy its sessionId   esc = deselect")
    args = ap.parse_args()

    global LOGGING
    LOGGING = not args.no_log

    if args.json:
        print(json.dumps({"workers": collect_workers(), "infra": collect_infra()}, indent=2))
        return

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

    # Live is the default, as with top/htop -- `-h` is argparse's help and exits,
    # so keys have nothing to act on there. A single frame is opt-in.
    if args.once:
        print("\n".join(frame(args.advise, not args.no_agents)[0]))
        return
    interval = args.watch if args.watch else REFRESH_SECONDS

    if vt:
        sys.stdout.write("\033[2J\033[?25l")  # one clear up front, then hide the cursor

    # Panels are toggled live rather than fixed at launch: on a short terminal all
    # three at once overflow the window, and what you want to see changes.
    # One panel at a time. They used to be independent toggles, which meant
    # opening ADVICE while SUBAGENTS was up pushed it past the bottom of the
    # terminal -- you had to close the other one first to see the one you asked
    # for. Flipping is what "show me the advice" actually means.
    view = "advice" if args.advise else (None if args.no_agents else "agents")
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
                    i_tag = c("i", BOLD, GREEN) if interactive else "i"
                    a_tag = c("a", BOLD, GREEN) if view == "advice" else "a"
                    s_tag = c("s", BOLD, GREEN) if view == "agents" else "s"
                    if not interactive:
                        hint = "space refresh | %s interactive | %s advice | %s agents | q quit" % (
                            i_tag, a_tag, s_tag)
                    elif sel is None:
                        hint = "j/k select | space refresh | %s interactive | %s advice | %s agents | q quit" % (
                            i_tag, a_tag, s_tag)
                    else:
                        hint = "j/k move | x stop | y yank id | esc deselect | %s interactive | q quit" % i_tag
                lines, rows, sel = frame(view == "advice", view == "agents", sel)
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


if __name__ == "__main__":
    main()
