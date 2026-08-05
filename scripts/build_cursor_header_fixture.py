#!/usr/bin/env python3
"""Build tests/fixtures/cursor/headers.sqlite for unit tests."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cursor" / "headers.sqlite"
OUT.parent.mkdir(parents=True, exist_ok=True)
if OUT.exists():
    OUT.unlink()

now_ms = int(time.time() * 1000)
composer_id = "abc12345-aaaa-bbbb-cccc-ddddeeeeffff"
value = {
    "type": "head",
    "composerId": composer_id,
    "name": "Add cursor support to roost",
    "subtitle": "Edited roost.py",
    "contextUsagePercent": 42.5,
    "lastUpdatedAt": now_ms,
    "createdAt": now_ms - 3600_000,
    "unifiedMode": "agent",
    "isDraft": False,
    "isArchived": False,
    "workspaceIdentifier": {"id": "test-ws"},
}

con = sqlite3.connect(str(OUT))
con.execute(
    "CREATE TABLE composerHeaders ("
    "composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, "
    "lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
    "recency INTEGER, checkpointAt INTEGER, value TEXT)")
con.execute(
    "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
    (composer_id, "test-ws", now_ms - 3600_000, now_ms, 0, 0, now_ms, None,
     json.dumps(value)))
# noise rows that must be filtered out
con.execute(
    "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
    ("empty-state-draft", "empty", now_ms, now_ms, 0, 0, now_ms, None,
     json.dumps({"isDraft": True, "composerId": "empty-state-draft"})))
con.execute(
    "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
    ("sub-agent-id", "test-ws", now_ms, now_ms, 0, 1, now_ms, None,
     json.dumps({"name": "Explore loaders", "isSubagent": True,
                 "contextUsagePercent": 10, "lastUpdatedAt": now_ms})))
con.commit()
con.close()
print("wrote", OUT)
