#!/usr/bin/env python3
"""Inventory Cursor's local agent state for roost adapter work (#48/#49).

Read-only. Prints paths and counts -- never prompt text or key material.
Run from the repo root:  python scripts/cursor_recon.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
CURSOR_HOME = Path(os.environ.get("CURSOR_AGENT_HOME", str(HOME / ".cursor")))
PROJECTS = CURSOR_HOME / "projects"

APP_SUPPORT = {
    "win32": HOME / "AppData" / "Roaming" / "Cursor" / "User",
    "darwin": HOME / "Library" / "Application Support" / "Cursor" / "User",
}.get(sys.platform, HOME / ".config" / "Cursor" / "User")

GLOBAL_DB = APP_SUPPORT / "globalStorage" / "state.vscdb"
CHATS = CURSOR_HOME / "chats"
TRACKING = CURSOR_HOME / "ai-tracking" / "ai-code-tracking.db"


def count_glob(root: Path, pattern: str) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.glob(pattern))


def sqlite_tables(path: Path):
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall()
        con.close()
        return [r[0] for r in rows]
    except sqlite3.Error as e:
        return ["<error: %s>" % e]


def main():
    print("Cursor recon (read-only)")
    print("  CURSOR_HOME =", CURSOR_HOME)
    print("  exists:", CURSOR_HOME.is_dir())
    print()
    print("Projects dir:", PROJECTS)
    if PROJECTS.is_dir():
        slugs = sorted(p.name for p in PROJECTS.iterdir() if p.is_dir())
        print("  workspace slugs:", len(slugs))
        for slug in slugs[:12]:
            n = count_glob(PROJECTS / slug / "agent-transcripts", "*/*")
            print("    %s  agent-transcript files: %d" % (slug, n))
        if len(slugs) > 12:
            print("    ... +%d more" % (len(slugs) - 12))
    print()
    print("Global state DB:", GLOBAL_DB)
    print("  exists:", GLOBAL_DB.is_file())
    if GLOBAL_DB.is_file():
        print("  size MB:", round(GLOBAL_DB.stat().st_size / 1e6, 2))
        print("  tables:", ", ".join(sqlite_tables(GLOBAL_DB)[:4]))
    print()
    print("Per-chat store root:", CHATS)
    print("  store.db count:", count_glob(CHATS, "**/store.db"))
    print()
    print("AI tracking DB:", TRACKING)
    print("  exists:", TRACKING.is_file())
    if TRACKING.is_file():
        print("  tables:", ", ".join(sqlite_tables(TRACKING)))
    print()
    print("Env overrides:")
    for key in ("CURSOR_AGENT_HOME", "ROOST_BACKENDS", "ROOST_CURSOR_MAX_IDLE_SECS"):
        val = os.environ.get(key)
        if val:
            print("  %s=%s" % (key, val))


if __name__ == "__main__":
    main()
