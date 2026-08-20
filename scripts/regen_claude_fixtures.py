#!/usr/bin/env python3
"""Regenerate scrubbed Claude on-disk fixtures from ~/.claude.

Usage:
    python scripts/regen_claude_fixtures.py

Picks one session lock, one transcript line with message.usage, and one
subagent first line. Free-form text (cwd, prompts, titles, names) is replaced
with placeholders. After a Claude Code upgrade, re-run this and let the canary
test fail with a key-tree diff if the shape moved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "claude"
HOME = Path.home() / ".claude"
SCRUB_CWD = "/tmp/roost-canary"
SCRUB_SID = "11111111-2222-3333-4444-555555555555"


def _keys(obj):
    return sorted(obj.keys()) if isinstance(obj, dict) else []


def _scrub_scalar(v):
    if isinstance(v, str) and len(v) > 24:
        return "scrubbed"
    return v


def _scrub_obj(obj, keep_keys=()):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("cwd",):
                out[k] = SCRUB_CWD
            elif k in ("sessionId", "session_id"):
                out[k] = SCRUB_SID
            elif k in ("content", "text", "customTitle", "lastPrompt", "name"):
                out[k] = _scrub_content(v)
            else:
                out[k] = _scrub_obj(v)
        return out
    if isinstance(obj, list):
        return [_scrub_obj(x) for x in obj]
    return obj


def _scrub_content(v):
    if isinstance(v, list):
        return [_scrub_obj(x) for x in v]
    if isinstance(v, str):
        return "scrubbed prompt" if v.strip() else v
    return v


def pick_session():
    files = sorted((HOME / "sessions").glob("*.json"))
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "pid" in data and "sessionId" in data:
            data["cwd"] = SCRUB_CWD
            data["name"] = "canary-session"
            data["sessionId"] = SCRUB_SID
            data["pid"] = 4242
            return data
    raise SystemExit("no ~/.claude/sessions/*.json with pid+sessionId")


def pick_transcript_lines():
    hits = list((HOME / "projects").glob("*/*.jsonl"))
    usage_line = None
    title_line = None
    prompt_line = None
    harvest_line = None
    for path in hits:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > 400000:
                    fh.seek(size - 400000)
                    fh.readline()
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(data.splitlines()):
            if '"usage"' in line and '"assistant"' in line and usage_line is None:
                try:
                    usage_line = json.loads(line)
                except ValueError:
                    continue
            if '"customTitle"' in line and title_line is None:
                try:
                    title_line = json.loads(line)
                except ValueError:
                    continue
            if '"lastPrompt"' in line and prompt_line is None:
                try:
                    prompt_line = json.loads(line)
                except ValueError:
                    continue
            if '"agentId"' in line and harvest_line is None:
                try:
                    harvest_line = json.loads(line)
                except ValueError:
                    continue
        if usage_line:
            break
    if usage_line is None:
        raise SystemExit("no transcript line with assistant+usage")
    lines = [_scrub_obj(usage_line)]
    if title_line:
        lines.append({"customTitle": "canary title", "sessionId": SCRUB_SID})
    else:
        lines.append({"customTitle": "canary title", "sessionId": SCRUB_SID})
    lines.append({"lastPrompt": "scrubbed prompt", "sessionId": SCRUB_SID})
    if harvest_line:
        lines.append(_scrub_obj(harvest_line))
    return lines, usage_line


def pick_subagent():
    hits = list((HOME / "projects").glob("*/*/subagents/agent-*.jsonl"))
    if not hits:
        raise SystemExit("no subagent transcripts")
    path = hits[0]
    with open(path, "rb") as fh:
        first = fh.readline().decode("utf-8", "replace")
    data = json.loads(first)
    data = _scrub_obj(data)
    data["agentId"] = "abc123"
    return data, path


def write_shape(session, usage_line, sub_first):
    msg = (usage_line.get("message") or {}) if isinstance(usage_line, dict) else {}
    usage = msg.get("usage") or {}
    shape = {
        "session_keys": _keys(session),
        "session_keys_required": ["cwd", "name", "pid", "sessionId", "startedAt"],
        "transcript_assistant_top_keys": _keys(usage_line),
        "transcript_assistant_message_keys": _keys(msg),
        "usage_parent": "message.usage",
        "usage_keys_required": [
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "input_tokens",
        ],
        "title_fields": ["customTitle", "lastPrompt"],
        "harvest_tool_result_keys": [
            "agentId", "agentType", "description", "resolvedModel", "status",
        ],
        "subagent_first_keys": _keys(sub_first),
        "subagent_first_keys_required": ["agentId", "isSidechain", "message"],
        "subagent_path": "projects/<slug>/<parentSessionId>/subagents/agent-<id>.jsonl",
    }
    # Keep required usage keys that the live file actually has; extra live keys
    # belong in the observed list so the canary diffs the full tree.
    shape["usage_keys_observed"] = _keys(usage)
    return shape


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    session = pick_session()
    tlines, usage_line = pick_transcript_lines()
    sub_first, sub_path = pick_subagent()
    (OUT / "session.json").write_text(
        json.dumps(session, indent=2) + "\n", encoding="utf-8")
    with open(OUT / "transcript.jsonl", "w", encoding="utf-8") as fh:
        for rec in tlines:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    (OUT / "subagent.jsonl").write_text(
        json.dumps(sub_first, separators=(",", ":")) + "\n", encoding="utf-8")
    shape = write_shape(session, usage_line, sub_first)
    (OUT / "shape.json").write_text(
        json.dumps(shape, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT)
    print("subagent source", sub_path)


if __name__ == "__main__":
    main()
