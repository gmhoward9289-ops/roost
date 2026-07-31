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
import socket
import sys
import time
from pathlib import Path

__version__ = "0.01"

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
# -----------------------------------------------------------------------------

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
            #  in a title could clear the screen or repaint the table.
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
        saving = tok - TYPICAL_BASELINE

        if tok >= EXPENSIVE_TOKENS and idle_h >= PARKED_IDLE_HOURS:
            out.append((saving, c("PARKED+COSTLY", BOLD, RED), tag,
                        "idle %.1fh holding %s tokens. Resuming costs that much on the "
                        "FIRST turn. Start a fresh session instead (~%s) and save ~%s per turn."
                        % (idle_h, "{:,}".format(tok), "{:,}".format(TYPICAL_BASELINE),
                           "{:,}".format(saving))))
        elif pct >= NEAR_LIMIT_PCT:
            out.append((saving, c("NEAR LIMIT", BOLD, YELLOW), tag,
                        "at %.0f%% of its window. Wrap up or /compact before it "
                        "auto-compacts mid-task." % pct))
        elif tok >= EXPENSIVE_TOKENS:
            out.append((saving, c("EXPENSIVE", YELLOW), tag,
                        "every turn now reprocesses %s tokens. Fine to finish the "
                        "current task in; do not start an unrelated one here."
                        % "{:,}".format(tok)))
        elif idle_h >= STALE_IDLE_HOURS:
            out.append((0, c("STALE", DIM), tag,
                        "idle %.1fh at %.0f%%. Costs nothing while it sits, but it hides "
                        "the sessions that matter -- close it." % (idle_h, pct)))

    lines = [c("ADVICE", BOLD)]
    if not out:
        lines.append("  nothing to act on -- no parked, oversized, or stale sessions")
        return lines
    for _, label, tag, text in sorted(out, key=lambda x: -x[0]):
        lines.append("  %s  %s" % (label, c(tag, BOLD)))
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


def render(workers):
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

    tagged = [(bucket(w), w) for w in workers]
    shown = [(b, w) for (b, w) in tagged if b[0] != 4]
    quiet = [w for (b, w) in tagged if b[0] == 4]
    # Within a group, order by what a turn costs. Percentage buries the
    # expensive sessions: 484k tokens on the 1M window reads as a mild 48%,
    # while 140k on a 200k window looks alarming at 70% and costs a third as much.
    shown.sort(key=lambda t: (t[0][0], -(t[1]["ctx_tokens"] or 0)))

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
    for (b, w), row in zip(shown, body):
        if b[1] != last:
            last = b[1]
            lines.append(c(b[1], BOLD, BUCKET_COLORS.get(b[0], DIM)))
        cells = [style_cell(cols[i][0], row[i].ljust(wid[i]), w) for i in range(len(cols))]
        # Last gate before the terminal. Sanitised at collection too, but this
        # is the boundary that matters: every task string reaches the screen here.
        task = ascii_safe(w.get("task") or "")
        if w.get("task_src") == "prompt" and task:
            task = c(task, DIM)  # not yet named -- this is the raw last prompt
        lines.append("  " + "  ".join(cells) + "  " + task)

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


def frame(with_advice=False, with_agents=True):
    workers = collect_workers()
    # Infra leads because it is a constant: one quiet line you skim past, which
    # is exactly the weight it deserves until something turns red.
    lines = render_infra(collect_infra()) + render(workers)
    if with_agents:
        live_sids = set(w["session_id"] for w in workers if w.get("session_id"))
        lines.extend(render_subagents(collect_subagents(live_sids)))
    if with_advice:
        lines.append("")
        lines.extend(advise(workers))
    return lines


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
        """One key within `timeout` seconds, else None."""
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
                        msvcrt.getwch()
                        return None
                    return ch
                time.sleep(0.02)
            return None
        import select

        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None


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
    body = [clip_ansi(ln, cols - 1) for ln in lines][: rows - 1]
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
    ap.epilog = ("keys while running:  space = refresh now   a = advice panel   "
                 "s = subagents panel   q = quit")
    args = ap.parse_args()

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
        print("\n".join(frame(args.advise, not args.no_agents)))
        return
    interval = args.watch if args.watch else REFRESH_SECONDS

    if vt:
        sys.stdout.write("\033[2J\033[?25l")  # one clear up front, then hide the cursor

    # Panels are toggled live rather than fixed at launch: on a short terminal all
    # three at once overflow the window, and what you want to see changes.
    show = {"advice": args.advise, "agents": not args.no_agents}
    try:
        with KeyReader() as keys:
            while True:
                if keys.enabled:
                    hint = "space refresh | %s advice | %s agents | q quit" % (
                        c("a", BOLD, GREEN) if show["advice"] else "a",
                        c("s", BOLD, GREEN) if show["agents"] else "s",
                    )
                else:
                    hint = "Ctrl-C to stop"
                header = [
                    c("roost", BOLD) + "  " + c(socket.gethostname(), CYAN)
                    + "  " + time.strftime("%H:%M:%S") + "   " + c(hint, DIM),
                    "",
                ]
                paint(header + frame(show["advice"], show["agents"]), vt)

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
                    if key == " ":
                        break  # repaint now
                    if key in ("a", "A"):
                        show["advice"] = not show["advice"]
                        break  # repaint immediately, do not wait out the tick
                    if key in ("s", "S"):
                        show["agents"] = not show["agents"]
                        break
                    if key in ("q", "Q", "\x03"):
                        return
    except KeyboardInterrupt:
        pass
    finally:
        if vt:
            sys.stdout.write("\033[?25h\n")  # restore the cursor on the way out
            sys.stdout.flush()


if __name__ == "__main__":
    main()
