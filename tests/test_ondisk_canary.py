"""Canary: pinned on-disk shapes roost actually reads.

Behavioral tests elsewhere can keep passing while Claude Code or Cursor
quietly rename a field. This file diffs fixture key trees against
tests/fixtures/{claude,cursor}/shape.json and fails with a precise mismatch.

Refresh Claude fixtures after an upgrade:
    python scripts/regen_claude_fixtures.py
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLAUDE = ROOT / "fixtures" / "claude"
CURSOR = ROOT / "fixtures" / "cursor"

import importlib.util
spec = importlib.util.spec_from_file_location(
    "roost", str(ROOT.parent / "roost.py"))
roost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roost)


def _keys(obj):
    return sorted(obj.keys()) if isinstance(obj, dict) else []


def _diff(label, got, want):
    return "%s\n  fixture: %s\n  pinned:  %s" % (label, got, want)


class TestClaudeOnDiskCanary(unittest.TestCase):
    def setUp(self):
        self.shape = json.loads((CLAUDE / "shape.json").read_text(encoding="utf-8"))
        self.session = json.loads((CLAUDE / "session.json").read_text(encoding="utf-8"))
        self.transcript = []
        for line in (CLAUDE / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.transcript.append(json.loads(line))
        self.sub = json.loads(
            (CLAUDE / "subagent.jsonl").read_text(encoding="utf-8").splitlines()[0])

    def test_session_key_tree_matches_the_pin(self):
        got = _keys(self.session)
        want = self.shape["session_keys"]
        self.assertEqual(got, want, _diff("session.json keys drifted", got, want))

    def test_session_still_has_fields_roost_reads(self):
        missing = [k for k in self.shape["session_keys_required"]
                   if k not in self.session]
        self.assertEqual(missing, [], "session lock lost %s" % missing)

    def test_usage_lives_under_message_not_the_top_level(self):
        assistant = next(r for r in self.transcript
                         if r.get("type") == "assistant" and isinstance(r.get("message"), dict)
                         and (r["message"].get("usage")))
        self.assertEqual(self.shape["usage_parent"], "message.usage")
        self.assertIn("usage", assistant["message"])
        self.assertNotIn("usage", assistant)
        got_msg = _keys(assistant["message"])
        want_msg = self.shape["transcript_assistant_message_keys"]
        self.assertEqual(got_msg, want_msg,
                         _diff("assistant message keys drifted", got_msg, want_msg))
        usage = assistant["message"]["usage"]
        missing = [k for k in self.shape["usage_keys_required"] if k not in usage]
        self.assertEqual(missing, [], "usage block lost %s" % missing)

    def test_title_and_prompt_fields_still_exist(self):
        blob = json.dumps(self.transcript)
        for field in self.shape["title_fields"]:
            self.assertIn('"%s"' % field, blob, "transcript lost %s" % field)

    def test_harvest_tool_result_shape(self):
        rec = next(r for r in self.transcript if isinstance(r.get("toolUseResult"), dict))
        missing = [k for k in self.shape["harvest_tool_result_keys"]
                   if k not in rec["toolUseResult"]]
        self.assertEqual(missing, [], "toolUseResult lost %s" % missing)

    def test_subagent_path_layout_is_pinned(self):
        self.assertEqual(
            self.shape["subagent_path"],
            "projects/<slug>/<parentSessionId>/subagents/agent-<id>.jsonl")
        got = _keys(self.sub)
        want = self.shape["subagent_first_keys"]
        self.assertEqual(got, want, _diff("subagent first-line keys drifted", got, want))
        missing = [k for k in self.shape["subagent_first_keys_required"] if k not in self.sub]
        self.assertEqual(missing, [], "subagent first line lost %s" % missing)

    def test_roost_still_reads_the_pinned_transcript(self):
        roost._SCAN_CACHE.clear()
        info = roost.scan_transcript(str(CLAUDE / "transcript.jsonl"))
        self.assertEqual(info["model"], "claude-opus-5")
        self.assertGreater(info["ctx_tokens"], 0)
        self.assertEqual(info["title"], "canary title")

    def test_collect_claude_subagents_joins_the_pinned_layout(self):
        td = tempfile.mkdtemp()
        parent = "11111111-2222-3333-4444-555555555555"
        dest = Path(td) / "slug" / parent / "subagents"
        dest.mkdir(parents=True)
        (dest / "agent-abc123.jsonl").write_text(
            (CLAUDE / "subagent.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        old = roost.PROJECTS_DIR
        roost.PROJECTS_DIR = Path(td)
        roost._SCAN_CACHE.clear()
        try:
            rows = roost.collect_claude_subagents({parent})
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent_id"], "abc123")
            self.assertEqual(rows[0]["parent_sid"], parent)
        finally:
            roost.PROJECTS_DIR = old


class TestCursorOnDiskCanary(unittest.TestCase):
    def setUp(self):
        self.shape = json.loads((CURSOR / "shape.json").read_text(encoding="utf-8"))
        self.lines = []
        for line in (CURSOR / "sample.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.lines.append(json.loads(line))

    def test_transcript_still_has_role_and_message(self):
        rec = self.lines[0]
        missing = [k for k in self.shape["transcript_role_keys_required"] if k not in rec]
        self.assertEqual(missing, [], "cursor jsonl lost %s" % missing)

    def test_usage_nesting_and_task_tool(self):
        usage_line = next(r for r in self.lines
                          if isinstance((r.get("message") or {}).get("usage"), dict))
        self.assertEqual(self.shape["usage_parent"], "message.usage")
        missing = [k for k in self.shape["usage_keys_required"]
                   if k not in usage_line["message"]["usage"]]
        self.assertEqual(missing, [], "cursor usage lost %s" % missing)
        task = next(
            part for r in self.lines
            for part in ((r.get("message") or {}).get("content") or [])
            if isinstance(part, dict) and part.get("name") == "Task")
        self.assertEqual(task.get("type"), self.shape["task_tool_use"]["content_part_type"])
        self.assertIn("model", task.get("input") or {})

    def test_user_query_tag_is_still_the_task_seam(self):
        blob = json.dumps(self.lines)
        self.assertIn(self.shape["user_query_tag"], blob)

    def test_composer_headers_table_has_required_columns(self):
        db = CURSOR / "headers.sqlite"
        con = sqlite3.connect(str(db))
        try:
            cols = {row[1] for row in con.execute(
                "PRAGMA table_info(%s)" % self.shape["headers_table"])}
        finally:
            con.close()
        missing = [c for c in self.shape["headers_columns_required"] if c not in cols]
        self.assertEqual(missing, [], "composerHeaders lost %s" % missing)

    def test_roost_still_reads_the_pinned_cursor_transcript(self):
        info = roost.scan_cursor_transcript(str(CURSOR / "sample.jsonl"))
        self.assertEqual(info["model"], "composer-2.5")
        self.assertIsNotNone(info["ctx_tokens"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
