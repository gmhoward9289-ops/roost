"""Tests for roost.

Written against stdlib unittest so `python -m unittest` works with nothing
installed; pytest collects them unchanged in CI.

The bar for a test here: it must be able to fail. Several of these encode bugs
that actually shipped during development -- the 242% context reading, escape
codes leaking through width clipping, and a mtime cache that returned stale rows.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("roost", str(ROOT / "roost.py"))
roost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roost)


def worker(**kw):
    """A worker row with sane defaults; override only what a test cares about."""
    base = {
        "name": "demo-a1", "pid": 1234, "session_id": "sid-1", "source": "claude",
        "cwd": "/tmp/demo", "project": "demo", "model": "claude-opus-5",
        "ctx_tokens": 50000, "ctx_pct": 25.0, "window": "200k",
        "idle_secs": 120.0, "age_secs": 600.0, "task": "a task", "task_src": "title",
        "auto_compact": True,
    }
    base.update(kw)
    return base


class TestWindowInference(unittest.TestCase):
    """A 1M-window session reads 480k+ cache tokens in one call; scoring that
    against 200k is what produced a nonsense '242%' reading. Known models now
    resolve their real window by name instead of guessing from usage -- the bug
    this fixed: a claude-fable-5 worker at 177k tokens used to read as '200k
    window, 89%' when the real window is 1M and usage is ~18%."""

    def test_known_model_resolves_exactly_regardless_of_usage(self):
        size, label = roost.window_for(177000, "claude-fable-5")
        self.assertEqual((size, label), (1000000, "1M"))
        self.assertNotIn("~", label)

    def test_haiku_4_5_dated_snapshot_matches_by_prefix(self):
        self.assertEqual(
            roost.window_for(1000, "claude-haiku-4-5-20251001"), (200000, "200k"))

    def test_unseen_dated_snapshot_still_matches_known_family_prefix(self):
        self.assertEqual(
            roost.window_for(1000, "claude-haiku-4-5-99999999"), (200000, "200k"))

    def test_legacy_dated_model_is_an_exact_match(self):
        self.assertEqual(
            roost.window_for(1000, "claude-opus-4-5-20251101"), (200000, "200k"))

    def test_unknown_model_falls_back_to_inference_and_is_marked(self):
        self.assertEqual(
            roost.window_for(100000, "claude-nonexistent-9"), (200000, "~200k"))

    def test_no_model_falls_back_to_inference_and_is_marked(self):
        self.assertEqual(roost.window_for(100000), (200000, "~200k"))

    def test_usage_over_200k_escalates_to_1m_when_inferring(self):
        self.assertEqual(roost.window_for(484030), (1000000, "~1M"))

    def test_boundary_is_inclusive_when_inferring(self):
        self.assertEqual(roost.window_for(200000, "unknown-model")[1], "~200k")

    def test_absurd_usage_clamps_to_largest_tier_when_inferring(self):
        self.assertEqual(roost.window_for(9_000_000)[1], "~1M")

    def test_no_percentage_exceeds_100_for_known_tiers(self):
        for tokens in (1, 199_999, 200_000, 200_001, 999_999, 1_000_000):
            size, _ = roost.window_for(tokens)
            self.assertLessEqual(100.0 * tokens / size, 100.0)

    def test_model_window_exact_and_prefix(self):
        self.assertEqual(roost.model_window("claude-opus-5"), 1000000)
        self.assertEqual(roost.model_window("claude-haiku-4-5-20251001"), 200000)
        self.assertIsNone(roost.model_window("claude-not-a-real-model"))
        self.assertIsNone(roost.model_window(None))
        self.assertIsNone(roost.model_window(""))


class TestAutoCompactResolution(unittest.TestCase):
    """autoCompactEnabled is never written to a session lock or transcript --
    roost has to walk the same settings.json hierarchy Claude Code itself
    merges, or a session with it truly off looks identical to a normal one
    that just has not hit NEAR LIMIT yet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.cwd, ".claude"))
        self.home = os.path.join(self.tmp, "home", ".claude")
        os.makedirs(self.home)
        self._home = roost.HOME
        self._managed = roost.MANAGED_SETTINGS_PATH
        roost.HOME = Path(self.tmp) / "home"
        roost.MANAGED_SETTINGS_PATH = Path(self.tmp) / "no-managed-file.json"

    def tearDown(self):
        roost.HOME = self._home
        roost.MANAGED_SETTINGS_PATH = self._managed

    def test_defaults_true_with_nothing_on_disk(self):
        self.assertTrue(roost.auto_compact_enabled(self.cwd))

    def test_user_settings_can_turn_it_off(self):
        Path(self.home, "settings.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        self.assertFalse(roost.auto_compact_enabled(self.cwd))

    def test_project_settings_overrides_user_settings(self):
        Path(self.home, "settings.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        Path(self.cwd, ".claude", "settings.json").write_text(
            json.dumps({"autoCompactEnabled": True}))
        self.assertTrue(roost.auto_compact_enabled(self.cwd))

    def test_local_settings_overrides_project_settings(self):
        Path(self.cwd, ".claude", "settings.json").write_text(
            json.dumps({"autoCompactEnabled": True}))
        Path(self.cwd, ".claude", "settings.local.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        self.assertFalse(roost.auto_compact_enabled(self.cwd))

    def test_managed_settings_wins_over_everything(self):
        managed = Path(self.tmp) / "managed-settings.json"
        managed.write_text(json.dumps({"autoCompactEnabled": True}))
        roost.MANAGED_SETTINGS_PATH = managed
        Path(self.cwd, ".claude", "settings.local.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        self.assertTrue(roost.auto_compact_enabled(self.cwd))

    def test_disable_auto_compact_env_key_is_equivalent(self):
        Path(self.cwd, ".claude", "settings.json").write_text(
            json.dumps({"env": {"DISABLE_AUTO_COMPACT": "1"}}))
        self.assertFalse(roost.auto_compact_enabled(self.cwd))

    def test_a_scope_that_sets_neither_key_falls_through(self):
        Path(self.cwd, ".claude", "settings.local.json").write_text(json.dumps({"other": 1}))
        Path(self.cwd, ".claude", "settings.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        self.assertFalse(roost.auto_compact_enabled(self.cwd))

    def test_malformed_json_is_skipped_not_fatal(self):
        Path(self.cwd, ".claude", "settings.local.json").write_text("{not json")
        Path(self.cwd, ".claude", "settings.json").write_text(
            json.dumps({"autoCompactEnabled": False}))
        self.assertFalse(roost.auto_compact_enabled(self.cwd))

    def test_no_cwd_defaults_true(self):
        self.assertTrue(roost.auto_compact_enabled(""))
        self.assertTrue(roost.auto_compact_enabled(None))


class TestAutoCompactDisplay(unittest.TestCase):
    def setUp(self):
        self._color = roost.COLOR
        roost.COLOR = False

    def tearDown(self):
        roost.COLOR = self._color

    def test_tag_appears_only_when_auto_compact_is_off(self):
        # idle_secs under a minute keeps the row in WORKING NOW rather than
        # collapsing into the QUIET summary line, where task text (and so the
        # tag) would not be shown at all.
        on = worker(name="a", pid=1, idle_secs=5, auto_compact=True)
        off = worker(name="b", pid=2, idle_secs=5, auto_compact=False)
        self.assertNotIn("no-compact", "\n".join(roost.render([on])))
        self.assertIn("no-compact", "\n".join(roost.render([off])))

    def test_advice_warns_there_is_no_safety_net_when_auto_compact_is_off(self):
        out = "\n".join(roost.advise([worker(
            ctx_tokens=400000, ctx_pct=85.0, idle_secs=10, auto_compact=False)]))
        self.assertIn("NEAR LIMIT", out)
        self.assertIn("auto-compact off", out)

    def test_advice_keeps_the_normal_wording_when_auto_compact_is_on(self):
        out = "\n".join(roost.advise([worker(
            ctx_tokens=400000, ctx_pct=85.0, idle_secs=10, auto_compact=True)]))
        self.assertIn("auto-compacts mid-task", out)


class TestDuration(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(roost.dur(None), "-")
        self.assertEqual(roost.dur(45), "45s")
        self.assertEqual(roost.dur(90), "1m")
        self.assertEqual(roost.dur(3661), "1h01m")


class TestAsciiSafe(unittest.TestCase):
    def test_replaces_non_renderable(self):
        self.assertEqual(roost.ascii_safe(u"a—b"), "a?b")

    def test_empty(self):
        self.assertEqual(roost.ascii_safe(""), "")

    def test_plain_ascii_untouched(self):
        self.assertEqual(roost.ascii_safe("plain text 123"), "plain text 123")


class TestAnsiWidth(unittest.TestCase):
    """Colour must never shift a column. A naive s[:width] counts escape bytes
    as columns and can truncate mid-sequence, leaking raw codes to the screen."""

    def setUp(self):
        self._color = roost.COLOR

    def tearDown(self):
        roost.COLOR = self._color

    def test_visible_len_ignores_escapes(self):
        roost.COLOR = True
        self.assertEqual(roost.visible_len(roost.c("abc", roost.RED)), 3)

    def test_clip_never_exceeds_width(self):
        roost.COLOR = True
        s = roost.c("x" * 40, roost.GREEN) + roost.c("y" * 40, roost.RED)
        for width in (1, 5, 39, 40, 41, 80):
            self.assertLessEqual(roost.visible_len(roost.clip_ansi(s, width)), width)

    def test_clip_leaves_no_partial_escape(self):
        roost.COLOR = True
        s = roost.c("hello world", roost.CYAN)
        for width in range(1, 12):
            out = roost.clip_ansi(s, width)
            self.assertEqual(out.count("\033["), out.count("m"),
                             "unterminated escape at width %d" % width)

    def test_short_string_passes_through(self):
        roost.COLOR = True
        s = roost.c("hi", roost.RED)
        self.assertEqual(roost.clip_ansi(s, 50), s)

    def test_coloured_render_matches_plain_width(self):
        rows = [worker(name="a", ctx_pct=85.0), worker(name="b", ctx_pct=10.0, model="claude-fable-5")]
        roost.COLOR = False
        plain = roost.render(rows)
        roost.COLOR = True
        coloured = roost.render(rows)
        self.assertEqual(len(plain), len(coloured))
        for p, col in zip(plain, coloured):
            self.assertEqual(roost.visible_len(col), len(p))


class TestBuckets(unittest.TestCase):
    """Ordering is by cost of ignoring, not by size."""

    def test_near_limit_wins(self):
        self.assertEqual(roost.bucket(worker(ctx_pct=85.0))[1], "NEAR LIMIT")

    def test_parked_and_costly(self):
        w = worker(ctx_pct=40.0, ctx_tokens=400000, idle_secs=5 * 3600)
        self.assertEqual(roost.bucket(w)[1], "PARKED + COSTLY")

    def test_fat_but_recent_is_not_parked(self):
        w = worker(ctx_pct=40.0, ctx_tokens=400000, idle_secs=10)
        self.assertEqual(roost.bucket(w)[1], "WORKING NOW")

    def test_working_now(self):
        self.assertEqual(roost.bucket(worker(idle_secs=5))[1], "WORKING NOW")

    def test_no_transcript_is_starting_not_working(self):
        """Regression: an unknown idle time once fell into WORKING NOW."""
        w = worker(ctx_tokens=None, ctx_pct=None, idle_secs=None)
        self.assertEqual(roost.bucket(w)[1], "STARTING")

    def test_quiet_is_the_default(self):
        self.assertEqual(roost.bucket(worker(idle_secs=9000, ctx_tokens=1000))[1], "QUIET")

    def test_quiet_rows_collapse_out_of_the_table(self):
        roost.COLOR = False
        rows = [worker(name="quiet-%d" % i, idle_secs=9000, ctx_tokens=1000) for i in range(5)]
        out = "\n".join(roost.render(rows))
        self.assertIn("QUIET (5)", out)
        self.assertNotIn("WORKING NOW", out)


class TestTranscriptScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s.jsonl")
        with open(self.path, "w") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"model": "claude-opus-5", "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 111479,
                    "cache_creation_input_tokens": 374,
                }},
            }) + "\n")
        roost._SCAN_CACHE.clear()

    def test_reads_model_and_sums_context(self):
        got = roost.scan_transcript(self.path)
        self.assertEqual(got["model"], "claude-opus-5")
        self.assertEqual(got["ctx_tokens"], 2 + 111479 + 374)

    def test_cache_avoids_a_second_read(self):
        """At a 1s refresh an uncached scan is megabytes of disk per second."""
        roost.scan_transcript(self.path)
        calls = []
        original = roost.read_tail
        roost.read_tail = lambda p, n=roost.TAIL_BYTES: (calls.append(p), original(p, n))[1]
        try:
            roost.scan_transcript(self.path)
        finally:
            roost.read_tail = original
        self.assertEqual(calls, [], "cached scan re-read the file")

    def test_cache_invalidates_when_file_changes(self):
        first = roost.scan_transcript(self.path)
        time.sleep(0.01)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"model": "claude-fable-5",
                            "usage": {"input_tokens": 7, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}},
            }) + "\n")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        second = roost.scan_transcript(self.path)
        self.assertNotEqual(first["model"], second["model"])

    def test_missing_file_is_not_fatal(self):
        got = roost.scan_transcript(os.path.join(self.tmp, "nope.jsonl"))
        self.assertIsNone(got["model"])


class TestContextHistory(unittest.TestCase):
    """History comes out of the transcript, not a buffer kept across frames."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s.jsonl")
        roost._SCAN_CACHE.clear()

    def write(self, totals, per_turn=1):
        """One assistant record per entry; per_turn repeats each total."""
        with open(self.path, "w") as fh:
            for t in totals:
                for _ in range(per_turn):
                    fh.write(json.dumps({
                        "type": "assistant",
                        "message": {"model": "claude-opus-5", "usage": {
                            "input_tokens": t, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0}},
                    }) + "\n")

    def test_history_is_oldest_first(self):
        self.write([10, 20, 30])
        self.assertEqual(roost.scan_transcript(self.path)["ctx_history"],
                         [10, 20, 30])

    def test_ctx_tokens_is_still_the_newest_turn(self):
        """The headline number must not become the oldest retained sample."""
        self.write([10, 20, 30])
        self.assertEqual(roost.scan_transcript(self.path)["ctx_tokens"], 30)

    def test_history_is_capped(self):
        self.write(list(range(1, 40)))
        got = roost.scan_transcript(self.path)["ctx_history"]
        self.assertEqual(len(got), roost.HISTORY_TURNS)
        self.assertEqual(got[-1], 39, "cap dropped the newest, not the oldest")

    def test_repeated_totals_within_a_turn_count_once(self):
        """A tool-using turn writes several records at the same total. Without
        de-duping, one busy turn fills the window and TREND reads +0."""
        self.write([10, 20, 30], per_turn=5)
        self.assertEqual(roost.scan_transcript(self.path)["ctx_history"],
                         [10, 20, 30])

    def test_missing_file_gives_an_empty_history(self):
        got = roost.scan_transcript(os.path.join(self.tmp, "nope.jsonl"))
        self.assertEqual(got["ctx_history"], [])


class TestGrowth(unittest.TestCase):
    def test_reports_the_span_not_the_last_step(self):
        self.assertEqual(roost.growth([100000, 101000, 116000]), "+16k")

    def test_flat_history(self):
        self.assertEqual(roost.growth([5000, 5000]), "=")

    def test_compaction_shows_as_a_drop(self):
        """/compact is the one thing that lowers context; it must not read +."""
        self.assertEqual(roost.growth([180000, 20000]), "-160k")

    def test_too_short_to_have_a_trend(self):
        for h in ([], [42], None):
            self.assertEqual(roost.growth(h), "-")


class TestSubagentDiscovery(unittest.TestCase):
    """Subagents have no pid; they live one directory deeper than sessions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.parent_sid = "parent-sid-1"
        d = Path(self.tmp) / "slug" / self.parent_sid / "subagents"
        d.mkdir(parents=True)
        self.agent_file = d / "agent-abc123.jsonl"
        with open(str(self.agent_file), "w") as fh:
            fh.write(json.dumps({
                "type": "user", "isSidechain": True, "agentId": "abc123",
                "message": {"role": "user", "content": "Scout the source URLs"},
            }) + "\n")
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"model": "claude-opus-5", "usage": {
                    "input_tokens": 1, "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 0}},
            }) + "\n")
        self._orig = roost.PROJECTS_DIR
        roost.PROJECTS_DIR = Path(self.tmp)
        roost._SCAN_CACHE.clear()
        roost._AGENT_META.clear()
        roost._AGENT_LABEL.clear()

    def tearDown(self):
        roost.PROJECTS_DIR = self._orig

    def test_finds_subagent_of_a_live_parent(self):
        rows = roost.collect_subagents({self.parent_sid})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "abc123")
        self.assertEqual(rows[0]["parent_sid"], self.parent_sid)
        self.assertEqual(rows[0]["model"], "claude-opus-5")

    def test_falls_back_to_first_prompt_for_a_label(self):
        rows = roost.collect_subagents({self.parent_sid})
        self.assertIn("Scout the source URLs", rows[0]["task"])

    def test_orphan_older_than_window_is_dropped(self):
        old = time.time() - (roost.AGENT_RECENT_SECS + 600)
        os.utime(str(self.agent_file), (old, old))
        roost._SCAN_CACHE.clear()
        self.assertEqual(roost.collect_subagents(set()), [])

    def test_recent_orphan_is_kept_and_marked(self):
        roost._SCAN_CACHE.clear()
        rows = roost.collect_subagents(set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "orphan")


class TestSubagentTypeAndCtx(unittest.TestCase):
    """The AGENT column shows the harvested type, and CTX shows tokens/window."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.parent_sid = "parent-sid-2"
        d = Path(self.tmp) / "slug" / self.parent_sid / "subagents"
        d.mkdir(parents=True)
        with open(str(d / "agent-def456.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "type": "user", "isSidechain": True, "agentId": "def456",
                "message": {"role": "user", "content": "Scout"},
            }) + "\n")
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"model": "claude-opus-5", "usage": {
                    "input_tokens": 1, "cache_read_input_tokens": 48000,
                    "cache_creation_input_tokens": 0}},
            }) + "\n")
        # Parent transcript: an early result with no type (async launch), then
        # the completed one that carries it. First-write-wins would keep "".
        with open(str(Path(self.tmp) / "slug" / (self.parent_sid + ".jsonl")), "w") as fh:
            fh.write(json.dumps({"toolUseResult": {
                "agentId": "def456", "status": "queued"}}) + "\n")
            fh.write(json.dumps({"toolUseResult": {
                "agentId": "def456", "status": "completed",
                "agentType": "Explore", "description": "Scout the loaders",
                "resolvedModel": "claude-opus-5"}}) + "\n")
        self._orig = roost.PROJECTS_DIR
        roost.PROJECTS_DIR = Path(self.tmp)
        roost._SCAN_CACHE.clear()
        roost._AGENT_META.clear()
        roost._AGENT_LABEL.clear()
        roost._HARVEST_POS.clear()
        roost.COLOR = False

    def tearDown(self):
        roost.PROJECTS_DIR = self._orig

    def test_later_result_fills_the_type_the_early_record_lacked(self):
        rows = roost.collect_subagents({self.parent_sid})
        self.assertEqual(rows[0]["agent_type"], "Explore")

    def test_agent_column_reads_type_slash_short_id(self):
        out = "\n".join(roost.render_subagents(
            roost.collect_subagents({self.parent_sid})))
        self.assertIn("Explore/def45", out)

    def test_ctx_column_reads_tokens_over_window(self):
        # claude-opus-5 is a known model (MODEL_WINDOWS) -- its real window is
        # 1M, not the old 200k usage-inferred tier.
        out = "\n".join(roost.render_subagents(
            roost.collect_subagents({self.parent_sid})))
        self.assertIn("48k/1M", out)

    def test_running_agent_without_a_type_keeps_the_hex_id(self):
        roost._AGENT_META.clear()
        os.remove(str(Path(self.tmp) / "slug" / (self.parent_sid + ".jsonl")))
        roost._HARVEST_POS.clear()
        rows = roost.collect_subagents({self.parent_sid})
        out = "\n".join(roost.render_subagents(rows))
        self.assertEqual(rows[0]["agent_type"], "")
        self.assertIn("def456", out)


class TestSpark(unittest.TestCase):
    """FLOW is shape, not volume: normalised to the buffer's own max."""

    def test_empty_history_is_blank(self):
        self.assertEqual(roost.spark([]), " " * roost.SPARK_LEN)

    def test_zero_flow_samples_show_as_dots(self):
        out = roost.spark([0, 0, 0])
        self.assertEqual(out, ("..." ).rjust(roost.SPARK_LEN))

    def test_the_busiest_sample_gets_the_hottest_glyph(self):
        out = roost.spark([1, 50, 100]).strip()
        self.assertEqual(out[-1], roost.SPARK_RAMP[-1])
        self.assertNotEqual(out[0], roost.SPARK_RAMP[-1])

    def test_stays_ascii(self):
        """Block-drawing characters mojibake in the Windows console; the ramp
        must never drift back to them."""
        for ch in roost.spark([0, 1, 2, 3, 1000]):
            self.assertLess(ord(ch), 127)


class TestUsage(unittest.TestCase):
    def setUp(self):
        roost.COLOR = False

    def test_parse_budget(self):
        self.assertEqual(roost.parse_budget("60M"), 60000000)
        self.assertEqual(roost.parse_budget("850k"), 850000)
        self.assertEqual(roost.parse_budget("1234"), 1234)
        self.assertIsNone(roost.parse_budget(None))
        self.assertIsNone(roost.parse_budget("a lot"))

    def test_local_models_are_flagged_and_kept_out_of_the_budget_math(self):
        days = {"2026-08-02": {"claude-opus-5": 1000000, "gemma4-32k": 5000000}}
        os.environ[roost.USAGE_BUDGET_ENV] = "10M"
        try:
            out = "\n".join(roost.render_usage(days))
        finally:
            del os.environ[roost.USAGE_BUDGET_ENV]
        self.assertIn("gemma4-32k (local)", out)
        self.assertIn("(10%)", out)  # 1M cloud of 10M -- not 60% with gemma in

    def test_tally_ignores_cache_reads(self):
        counts = {}
        roost._tally_lines([json.dumps({
            "type": "assistant", "timestamp": "2026-08-02T10:00:00.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "output_tokens": 5,
                                  "cache_read_input_tokens": 900000}},
        })], counts)
        self.assertEqual(counts[("2026-08-02", "claude-opus-5")], 15)

    def test_without_a_budget_the_panel_still_tallies(self):
        out = "\n".join(roost.render_usage(
            {"2026-08-02": {"claude-opus-5": 500}}))
        self.assertIn("cloud", out)
        self.assertNotIn("budget (", out)
        self.assertIn(roost.USAGE_BUDGET_ENV, out)


class TestCursor(unittest.TestCase):
    """The cursor addresses a row that x will stop. Anything that lets the screen
    and the index disagree is a wrong-session kill, so the invariants are here."""

    def setUp(self):
        self._color = roost.COLOR
        roost.COLOR = False

    def tearDown(self):
        roost.COLOR = self._color

    def test_quiet_is_unreachable_without_a_cursor(self):
        rows = [worker(name="q%d" % i, idle_secs=9000, ctx_tokens=1000) for i in range(4)]
        shown, quiet = roost.arrange(rows)
        self.assertEqual(shown, [])
        self.assertEqual(len(quiet), 4)

    def test_cursor_expands_quiet_so_stale_sessions_can_be_reached(self):
        """The sweep exists for idle sessions; collapsing them hides the targets."""
        rows = [worker(name="q%d" % i, idle_secs=9000, ctx_tokens=1000) for i in range(4)]
        shown, quiet = roost.arrange(rows, expand_quiet=True)
        self.assertEqual(len(shown), 4)
        self.assertEqual(quiet, [])

    def test_one_marker_and_it_is_on_the_selected_row(self):
        rows = [worker(name="a", ctx_pct=85.0), worker(name="b", idle_secs=5)]
        shown, _ = roost.arrange(rows, expand_quiet=True)
        for sel in range(len(shown)):
            out = roost.render(rows, sel)
            marked = [ln for ln in out if ln.startswith("> ")]
            self.assertEqual(len(marked), 1)
            self.assertIn(shown[sel][1]["name"], marked[0])

    def test_marker_is_present_without_colour(self):
        """Reverse video is unavailable over plain pipes; the marker is the
        only thing left saying which row x acts on."""
        out = roost.render([worker(name="solo", idle_secs=5)], 0)
        self.assertTrue(any(ln.startswith("> ") for ln in out))

    def test_selection_does_not_shift_columns(self):
        rows = [worker(name="a", ctx_pct=85.0), worker(name="b", model="claude-fable-5")]
        plain = roost.render(rows, 0)
        roost.COLOR = True
        coloured = roost.render(rows, 0)
        self.assertEqual(len(plain), len(coloured))
        for p, col in zip(plain, coloured):
            self.assertEqual(roost.visible_len(col), len(p))

    def test_highlight_rearms_after_every_reset(self):
        """A per-cell RESET clears reverse video too, so a naive wrap highlights
        only as far as the first coloured cell."""
        roost.COLOR = True
        line = roost.c("aa", roost.RED) + " " + roost.c("bb", roost.GREEN)
        out = roost.highlight(line)
        self.assertEqual(out.count(roost.REVERSE), line.count(roost.RESET) + 1)
        self.assertEqual(roost.visible_len(out), roost.visible_len(line))

    def test_highlight_is_a_noop_without_colour(self):
        self.assertEqual(roost.highlight("plain"), "plain")


class TestFrameClamping(unittest.TestCase):
    """Sessions exit between frames. A cursor left pointing past the end would
    silently address nothing -- or, worse, whatever slid into that index."""

    # frame() is the only function that touches all three collectors at once, so
    # every stub has to be handed back -- an earlier version leaked its empty
    # collect_subagents into the subagent tests and failed three of them.
    PATCHED = ("collect_workers", "collect_infra", "collect_subagents")

    def setUp(self):
        self._saved = {n: getattr(roost, n) for n in self.PATCHED}
        roost.collect_infra = lambda: []
        roost.collect_subagents = lambda sids: []

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(roost, name, fn)

    def test_sel_past_the_end_clamps_to_the_last_row(self):
        roost.collect_workers = lambda: [worker(name="a", pid=1, idle_secs=5),
                                         worker(name="b", pid=2, idle_secs=5)]
        _, rows, sel = roost.frame(sel=99)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sel, 1)

    def test_sel_drops_when_every_session_is_gone(self):
        roost.collect_workers = lambda: []
        _, rows, sel = roost.frame(sel=3)
        self.assertEqual(rows, [])
        self.assertIsNone(sel)

    def test_rows_match_what_was_rendered(self):
        """frame() hands back the list the key handler indexes; if it disagreed
        with the table by even one row, x would stop the wrong session."""
        roost.collect_workers = lambda: [
            worker(name="near", pid=1, ctx_pct=85.0),
            worker(name="quiet", pid=2, idle_secs=9000, ctx_tokens=1000),
        ]
        lines, rows, sel = roost.frame(sel=0)
        table = [ln for ln in lines if ln.startswith(("  ", "> ")) and "WORKER" not in ln]
        self.assertEqual(len(rows), 2)  # quiet expanded under the cursor
        for r in rows:
            self.assertTrue(any(r["name"] in ln for ln in table), r["name"])


class TestTerminateGuard(unittest.TestCase):
    def test_refuses_to_stop_its_own_process(self):
        self.assertIn("own process tree", roost.terminate(os.getpid()))

    def test_refuses_to_stop_its_parent(self):
        """roost launched from inside a session puts that session's pid on screen;
        x on that row would take roost's own terminal with it."""
        self.assertIn("own process tree", roost.terminate(os.getppid()))

    def test_actually_stops_a_real_child_process(self):
        """The only test that runs the platform kill itself.

        Windows is the load-bearing leg here: there is no cross-process SIGTERM,
        so that branch is OpenProcess + TerminateProcess through ctypes and no
        Linux or macOS run will ever execute it. The guard tests above return
        before reaching any of that.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertIsNone(roost.terminate(child.pid))
            # Exit status, not roost.alive(): Popen holds an open handle to the
            # child, and Windows keeps a process object queryable for as long as
            # any handle exists -- so OpenProcess still succeeds here even though
            # the process is dead. It does not affect roost, whose rows are
            # sessions it never spawned and holds no handle to.
            child.wait(timeout=15)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:  # terminate() failed; do not leak the process
                child.kill()
                child.wait(timeout=10)


class TestPaintOverflow(unittest.TestCase):
    """Silent truncation is how a confirmation prompt and the ADVICE panel both
    went missing without appearing to fail: 24 sessions plus subagents make a
    frame taller than the terminal, and the screen still looked complete."""

    def setUp(self):
        self._color, self._term = roost.COLOR, roost.term_size
        roost.COLOR = False
        roost.term_size = lambda: (100, 12)

    def tearDown(self):
        roost.COLOR, roost.term_size = self._color, self._term

    def _paint(self, n):
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            roost.paint(["line %d" % i for i in range(n)], vt=False)
        finally:
            sys.stdout = stdout
        return buf.getvalue().splitlines()

    def test_overflow_is_announced_not_swallowed(self):
        out = self._paint(40)
        self.assertIn("more line(s) below", out[-1])

    def test_the_count_accounts_for_the_notice_itself(self):
        """The notice occupies a row, so the line it displaces must be counted."""
        out = self._paint(40)
        shown = len(out) - 1  # every row but the notice
        self.assertIn("%d more line(s)" % (40 - shown), out[-1])

    def test_a_frame_that_fits_keeps_every_line(self):
        out = self._paint(5)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[:-1], ["line %d" % i for i in range(4)])
        # The last line carries the version stamp, so it is compared by prefix.
        self.assertTrue(out[-1].startswith("line 4"))
        self.assertNotIn("more line(s)", "\n".join(out))

    def test_never_paints_more_rows_than_the_terminal_has(self):
        for n in (1, 10, 11, 12, 13, 200):
            self.assertLessEqual(len(self._paint(n)), 11)

    def test_version_is_stamped_bottom_right(self):
        out = self._paint(3)
        self.assertTrue(out[-1].endswith("v" + roost.__version__))

    def test_version_survives_an_overflowing_frame(self):
        """It rides the overflow notice rather than the line the notice replaced,
        so the thing that reports clipping cannot itself clip the version away."""
        out = self._paint(40)
        self.assertIn("more line(s) below", out[-1])
        self.assertTrue(out[-1].endswith("v" + roost.__version__))

    def test_version_is_dropped_rather_than_wrapped(self):
        """A wrapped line scrolls the display, which is indistinguishable from a
        clear that never happened."""
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            roost.paint(["x" * 200], vt=False)
        finally:
            sys.stdout = stdout
        line = buf.getvalue().splitlines()[-1]
        self.assertLessEqual(len(line), 99)
        self.assertNotIn("v" + roost.__version__, line)

    def test_padding_counts_columns_not_bytes(self):
        """len() on a coloured line counts escape bytes as columns and shoves the
        stamp past the right edge by one column per colour code."""
        roost.COLOR = True
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            roost.paint([roost.c("a", roost.RED) + roost.c("b", roost.GREEN)], vt=False)
        finally:
            sys.stdout = stdout
        self.assertEqual(roost.visible_len(buf.getvalue().splitlines()[-1]), 99)


class TestActionLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._path, self._on = roost.LOG_PATH, roost.LOGGING
        # A nested directory on purpose: on a fresh machine ~/.claude/logs does
        # not exist yet, and the first stop must not be the thing that discovers it.
        roost.LOG_PATH = Path(self.tmp) / "logs" / "roost.jsonl"
        roost.LOGGING = True

    def tearDown(self):
        roost.LOG_PATH, roost.LOGGING = self._path, self._on

    def test_writes_one_json_record_per_action(self):
        roost.log_action("stop", worker(name="a", pid=7))
        roost.log_action("stop", worker(name="b", pid=8), ok=False, detail="denied")
        records = [json.loads(l) for l in roost.LOG_PATH.read_text().splitlines()]
        self.assertEqual([r["pid"] for r in records], [7, 8])
        self.assertEqual([r["ok"] for r in records], [True, False])
        self.assertEqual(records[1]["detail"], "denied")

    def test_task_text_is_never_logged(self):
        """The task is free-form transcript prose. An audit trail of what was
        stopped must not become a copy of what was being worked on."""
        roost.log_action("stop", worker(task="private notes about a client"))
        body = roost.LOG_PATH.read_text()
        self.assertNotIn("private notes", body)
        self.assertNotIn("task", json.loads(body.splitlines()[0]))

    def test_records_carry_what_makes_a_sweep_answerable_later(self):
        roost.log_action("stop", worker(ctx_tokens=484030, idle_secs=92500.7))
        rec = json.loads(roost.LOG_PATH.read_text().splitlines()[0])
        self.assertEqual(rec["ctx_tokens"], 484030)
        self.assertEqual(rec["idle_secs"], 92500)
        for field in ("ts", "host", "name", "session_id", "model"):
            self.assertIn(field, rec)

    def test_no_log_writes_nothing(self):
        roost.LOGGING = False
        roost.log_action("stop", worker())
        self.assertFalse(roost.LOG_PATH.exists())

    def test_an_unwritable_log_does_not_take_down_the_ui(self):
        """Losing the log is survivable; losing the frame is not."""
        roost.LOG_PATH = Path(self.tmp) / "logs"  # a directory, so open() fails
        roost.LOG_PATH.mkdir(parents=True, exist_ok=True)
        roost.log_action("stop", worker())  # must not raise

    def test_trim_holds_the_file_at_the_cap(self):
        roost.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fat = json.dumps({"pad": "x" * 500})
        roost.LOG_PATH.write_text("\n".join([fat] * (roost.LOG_MAX_LINES + 400)) + "\n")
        roost.trim_log()
        kept = len(roost.LOG_PATH.read_text().splitlines())
        self.assertEqual(kept, roost.LOG_MAX_LINES)


class TestInfraLine(unittest.TestCase):
    def setUp(self):
        roost.COLOR = False

    def test_reports_down_services(self):
        out = "\n".join(roost.render_infra(
            [{"name": "litellm", "port": 4000, "up": False, "detail": "not running"}]))
        self.assertIn("litellm", out)
        self.assertIn("DOWN", out)

    def test_no_sessions_still_renders(self):
        self.assertIn("no live agent sessions", "\n".join(roost.render([])))

    def test_no_model_resident_is_shown_not_swallowed(self):
        """Installed-but-unloaded used to collapse the whole detail away, leaving
        a bare 'ollama:11434 up' that reads as roost not knowing Ollama exists."""
        out = "\n".join(roost.render_infra(
            [{"name": "ollama", "port": 11434, "up": True, "detail": "no model resident"}]))
        self.assertIn("no model resident", out)


class TestLocalModels(unittest.TestCase):
    """The INFRA line only ever reflects /api/ps -- a model that is installed but
    idle drops out of it entirely. collect_local_models() is the fuller picture,
    merging /api/tags (everything installed) with /api/ps (what is resident)."""

    def setUp(self):
        roost.COLOR = False
        self._port_open, self._http_json = roost.port_open, roost.http_json

    def tearDown(self):
        roost.port_open, roost.http_json = self._port_open, self._http_json

    def test_empty_when_ollama_is_down(self):
        roost.port_open = lambda port, timeout=0.35: False
        roost.http_json = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not fetch"))
        self.assertEqual(roost.collect_local_models(), [])

    def test_merges_installed_with_resident(self):
        roost.port_open = lambda port, timeout=0.35: True

        def fake_http_json(port, path, timeout=1.5):
            if path == "/api/tags":
                return {"models": [
                    {"name": "qwen-coder-16k:latest", "size": 5_500_000_000},
                    {"name": "gemma4-32k:latest", "size": 9_610_000_000},
                ]}
            if path == "/api/ps":
                return {"models": [
                    {"name": "qwen-coder-16k:latest", "size_vram": 5_500_000_000,
                     "expires_at": "2099-01-01T00:00:00-07:00"},
                ]}
            return None

        roost.http_json = fake_http_json
        models = {m["name"]: m for m in roost.collect_local_models()}
        self.assertTrue(models["qwen-coder-16k:latest"]["resident"])
        self.assertAlmostEqual(models["qwen-coder-16k:latest"]["vram_gb"], 5.5)
        self.assertIsNotNone(models["qwen-coder-16k:latest"]["expires_secs"])
        self.assertFalse(models["gemma4-32k:latest"]["resident"])
        self.assertIsNone(models["gemma4-32k:latest"]["vram_gb"])
        self.assertIsNone(models["gemma4-32k:latest"]["expires_secs"])

    def test_an_unparseable_expiry_drops_the_timer_not_the_model(self):
        roost.port_open = lambda port, timeout=0.35: True

        def fake_http_json(port, path, timeout=1.5):
            if path == "/api/tags":
                return {"models": [{"name": "m", "size": 1_000_000_000}]}
            if path == "/api/ps":
                return {"models": [{"name": "m", "size_vram": 1_000_000_000,
                                     "expires_at": "not-a-timestamp"}]}
            return None

        roost.http_json = fake_http_json
        models = roost.collect_local_models()
        self.assertEqual(len(models), 1)
        self.assertTrue(models[0]["resident"])
        self.assertIsNone(models[0]["expires_secs"])


class TestRenderModels(unittest.TestCase):
    def setUp(self):
        roost.COLOR = False

    def test_no_models_says_so(self):
        out = "\n".join(roost.render_models([]))
        self.assertIn("LOCAL MODELS", out)
        self.assertIn("none installed", out)

    def test_lists_disk_state_and_vram(self):
        out = "\n".join(roost.render_models([
            {"name": "qwen-coder-16k:latest", "disk_gb": 5.5, "resident": True,
             "vram_gb": 5.5, "expires_secs": 240},
            {"name": "gemma4-32k:latest", "disk_gb": 9.6, "resident": False,
             "vram_gb": None, "expires_secs": None},
        ]))
        self.assertIn("qwen-coder-16k:latest", out)
        self.assertIn("gemma4-32k:latest", out)
        self.assertIn("resident", out)
        self.assertIn("unloaded", out)
        self.assertIn("5.5 GB", out)
        self.assertIn("2 installed, 1 resident", out)


class TestRenderHelp(unittest.TestCase):
    """Help is an overview of what's on screen, not a keybinding reference --
    the footer hint already lists the keys."""

    PATCHED = ("collect_workers", "collect_infra", "collect_subagents")

    def setUp(self):
        roost.COLOR = False
        self._saved = {n: getattr(roost, n) for n in self.PATCHED}
        roost.collect_workers = lambda: []
        roost.collect_infra = lambda: []
        roost.collect_subagents = lambda sids: []

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(roost, name, fn)

    def test_names_every_toggleable_panel(self):
        out = "\n".join(roost.render_help())
        self.assertIn("HELP", out)
        for name, key, _ in roost.HELP_SCREENS:
            self.assertIn(name, out)
            if key:
                self.assertIn("(%s)" % key, out)

    def test_is_reachable_through_frame(self):
        """The 'h' toggle wires through frame() the same way a/s/m do."""
        lines, _, _ = roost.frame(view="help")
        self.assertIn(roost.c("HELP", roost.BOLD), lines)



class TestAdviceNamesTheTask(unittest.TestCase):
    """A pid says which process to kill; the task says whether you want to."""

    def _advice_for(self, **kw):
        roost.COLOR = False
        return "\n".join(roost.advise([worker(**kw)]))

    def test_task_appears_beside_the_pid(self):
        out = self._advice_for(ctx_tokens=400000, ctx_pct=40.0,
                               idle_secs=5 * 3600, task="audit the build scripts")
        self.assertIn("PARKED+COSTLY", out)
        self.assertIn("demo-a1 (pid 1234)", out)
        self.assertIn("audit the build scripts", out)

    def test_long_task_is_truncated_not_wrapped(self):
        out = self._advice_for(ctx_tokens=400000, ctx_pct=40.0,
                               idle_secs=5 * 3600, task="x" * 200)
        head = [ln for ln in out.splitlines() if "demo-a1" in ln][0]
        self.assertIn("...", head)
        trailing = head.split("demo-a1 (pid 1234)")[1].strip()
        self.assertLessEqual(len(trailing), roost.ADVICE_TASK_WIDTH)

    def test_escapes_in_a_task_never_reach_the_advice_line(self):
        # ADVICE renders transcript text just as the table does, so it needs the
        # same guard: a title carrying a clear-screen sequence would blank the
        # display from here too.
        evil = "evil" + chr(27) + "[2J" + chr(27) + "[Hgone"
        out = self._advice_for(ctx_tokens=400000, ctx_pct=40.0,
                               idle_secs=5 * 3600, task=evil)
        self.assertNotIn(chr(27) + "[2J", out)
        self.assertIn("evil", out)

    def test_missing_task_still_renders_cleanly(self):
        out = self._advice_for(ctx_tokens=400000, ctx_pct=40.0,
                               idle_secs=5 * 3600, task="")
        head = [ln for ln in out.splitlines() if "demo-a1" in ln][0]
        self.assertIn("demo-a1 (pid 1234)", head)
        self.assertTrue(head.rstrip().endswith(")"))

    def test_quiet_session_produces_no_advice(self):
        roost.COLOR = False
        out = "\n".join(roost.advise([worker(ctx_tokens=20000, ctx_pct=10.0,
                                             idle_secs=60)]))
        self.assertIn("nothing to act on", out)

class TestUsageCacheEviction(unittest.TestCase):
    """A deleted transcript's cache entry lived forever and its tokens were
    still summed into the USAGE panel -- wrong numbers plus a slow leak."""

    def setUp(self):
        roost._USAGE_CACHE.clear()

    def tearDown(self):
        roost._USAGE_CACHE.clear()

    def test_deleted_transcript_is_evicted_and_uncounted(self):
        roost._USAGE_CACHE["gone.jsonl"] = {
            "mtime": time.time(), "size": 10,
            "counts": {("2026-08-01", "claude-opus-5"): 1234}}
        days = roost.collect_usage()
        self.assertNotIn("gone.jsonl", roost._USAGE_CACHE)
        for byday in days.values():
            self.assertNotIn(1234, byday.values())


class TestCachePruning(unittest.TestCase):
    """The scan/agent caches grew for as long as the dashboard stayed open --
    days -- because nothing ever removed sessions and agents that had exited."""

    def setUp(self):
        for d in (roost._SCAN_CACHE, roost._AGENT_META, roost._AGENT_LABEL):
            d.clear()
        roost._SEEN_PATHS.clear()
        roost._SEEN_AGENTS.clear()

    def test_unseen_entries_are_evicted(self):
        roost._SCAN_CACHE["dead.jsonl"] = (1.0, {})
        roost._AGENT_META["dead-agent"] = {"description": "x"}
        roost._AGENT_LABEL["dead-agent"] = "x"
        roost.prune_caches()
        self.assertEqual(roost._SCAN_CACHE, {})
        self.assertEqual(roost._AGENT_META, {})
        self.assertEqual(roost._AGENT_LABEL, {})

    def test_seen_entries_survive(self):
        roost._SCAN_CACHE["live.jsonl"] = (1.0, {})
        roost._AGENT_META["live-agent"] = {"description": "x"}
        roost._SEEN_PATHS.add("live.jsonl")
        roost._SEEN_AGENTS.add("live-agent")
        roost.prune_caches()
        self.assertIn("live.jsonl", roost._SCAN_CACHE)
        self.assertIn("live-agent", roost._AGENT_META)

    def test_seen_sets_reset_after_prune(self):
        roost._SEEN_PATHS.add("live.jsonl")
        roost.prune_caches()
        self.assertEqual(roost._SEEN_PATHS, set())
        self.assertEqual(roost._SEEN_AGENTS, set())

    def test_scan_transcript_registers_its_path(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s.jsonl")
            with open(p, "w") as fh:
                fh.write("\n")
            roost.scan_transcript(p)
            self.assertIn(p, roost._SEEN_PATHS)


class TestTerminalTeardown(unittest.TestCase):
    """After exit the shell prompt must come back exactly as it was: console
    mode restored on Windows, no stale input delivered on POSIX."""

    def test_restore_vt_is_safe_when_nothing_was_changed(self):
        old = roost._VT_ORIGINAL
        roost._VT_ORIGINAL = None
        try:
            roost.restore_vt()  # must not raise
        finally:
            roost._VT_ORIGINAL = old

    def test_posix_restore_flushes_pending_input(self):
        # TCSADRAIN handed queued arrow-sequence tails to the shell after exit;
        # the restore must discard them instead.
        src = (ROOT / "roost.py").read_text(encoding="utf-8")
        self.assertIn("termios.TCSAFLUSH", src)
        self.assertNotIn("termios.TCSADRAIN,", src)

    def test_key_reader_never_reads_buffered_stdin(self):
        # sys.stdin.read buffers past what select() sees; keys then leak to the
        # shell prompt after exit. Only raw os.read on the fd is allowed.
        src = (ROOT / "roost.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.stdin.read(", src)

class TestInfraCached(unittest.TestCase):
    """The render loop must not block on infra sockets. A DOWN service costs
    the full connect timeout (0.35 s measured on Windows), and that stall
    shipped: every keypress and repaint lagged by it while OpenWebUI was off.
    """

    def setUp(self):
        self._orig = roost.collect_infra
        roost._INFRA_SNAPSHOT = None
        roost._INFRA_THREAD = None

    def tearDown(self):
        roost.collect_infra = self._orig
        roost._INFRA_SNAPSHOT = None
        roost._INFRA_THREAD = None

    def test_first_call_probes_synchronously(self):
        roost.collect_infra = lambda: [{"name": "x", "port": 1, "up": True,
                                        "detail": ""}]
        snap = roost.infra_cached()
        self.assertEqual(snap[0]["name"], "x")

    def test_later_calls_never_probe_inline(self):
        calls = []

        def slow_probe():
            calls.append(time.perf_counter())
            time.sleep(0.35)  # a DOWN service burning its socket timeout
            return [{"name": "x", "port": 1, "up": False, "detail": "not running"}]

        roost.collect_infra = slow_probe
        roost.infra_cached()  # seeds and starts the worker
        t0 = time.perf_counter()
        for _ in range(5):
            roost.infra_cached()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.1,
                        "infra_cached blocked the caller after seeding "
                        "(%.3fs for 5 calls)" % elapsed)

    def test_worker_refreshes_the_snapshot(self):
        state = {"n": 0}

        def counting_probe():
            state["n"] += 1
            return [{"name": "x", "port": 1, "up": True,
                     "detail": "gen %d" % state["n"]}]

        roost.collect_infra = counting_probe
        old = roost.INFRA_REFRESH_SECONDS
        roost.INFRA_REFRESH_SECONDS = 0.05
        try:
            first = roost.infra_cached()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if roost.infra_cached() != first:
                    break
                time.sleep(0.02)
            self.assertNotEqual(roost.infra_cached(), first,
                                "background worker never refreshed the snapshot")
        finally:
            roost.INFRA_REFRESH_SECONDS = old


class TestCompactAndBar(unittest.TestCase):
    """Both are width-constrained formatters: every column downstream is laid
    out against what they return, so a stray digit shifts the whole table."""

    def test_compact_switches_units_at_the_thresholds(self):
        self.assertEqual(roost.compact(999), "999")
        self.assertEqual(roost.compact(1000), "1k")
        self.assertEqual(roost.compact(484030), "484k")
        self.assertEqual(roost.compact(999999), "999k")
        self.assertEqual(roost.compact(1000000), "1.0M")
        self.assertEqual(roost.compact(1250000), "1.2M")

    def test_compact_of_nothing_is_a_dash_not_a_zero(self):
        # A session with no transcript has no token count; printing "0" would
        # claim it is empty rather than unknown.
        self.assertEqual(roost.compact(None), "-")

    def test_bar_is_always_the_same_width(self):
        for pct in (None, 0, 1.0, 49.9, 50.0, 99.9, 100.0, 240.0):
            self.assertEqual(len(roost.bar(pct)), roost.BAR_WIDTH + 2)

    def test_bar_never_overflows_on_a_bad_percentage(self):
        # The 242% reading that WINDOW_TIERS fixed could still arrive from a
        # tier that has not shipped yet; the bar has to clamp, not wrap.
        self.assertEqual(roost.bar(240.0), "[" + "#" * roost.BAR_WIDTH + "]")

    def test_bar_is_empty_when_the_context_is_unknown(self):
        self.assertEqual(roost.bar(None), "[" + " " * roost.BAR_WIDTH + "]")

    def test_bar_stays_ascii(self):
        for ch in roost.bar(55.0):
            self.assertLess(ord(ch), 127)


class TestBurnRates(unittest.TestCase):
    """The runway figure ("~2.1d left") is the one number that can tell someone
    to stop working, so its sample has to be honest: rows from before a weekly
    reset average a cliff into the rate and understate the burn badly."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._history = roost.USAGE_HISTORY
        roost.USAGE_HISTORY = Path(self.td.name) / "history.jsonl"

    def tearDown(self):
        roost.USAGE_HISTORY = self._history
        self.td.cleanup()

    def write(self, rows):
        roost.USAGE_HISTORY.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def at(self, hours_ago, **caps):
        row = {"epoch": time.time() - hours_ago * 3600}
        row.update(caps)
        return row

    def test_rate_is_percent_per_hour_across_the_span(self):
        self.write([self.at(8, weekly=10), self.at(4, weekly=14),
                    self.at(0, weekly=18)])
        self.assertAlmostEqual(roost._burn_rates()["weekly"], 1.0, places=6)

    def test_rows_before_a_reset_are_dropped(self):
        # 90% -> 5% is a reset, not a -85 point/hour burn. Only the three rows
        # after it are a valid sample.
        self.write([self.at(20, weekly=80), self.at(16, weekly=90),
                    self.at(12, weekly=5), self.at(8, weekly=10),
                    self.at(4, weekly=15)])
        self.assertAlmostEqual(roost._burn_rates()["weekly"], 1.25, places=6)

    def test_two_points_are_not_a_trend(self):
        self.write([self.at(8, weekly=10), self.at(0, weekly=30)])
        self.assertEqual(roost._burn_rates(), {})

    def test_a_span_under_six_hours_is_not_a_trend(self):
        self.write([self.at(4, weekly=10), self.at(2, weekly=14),
                    self.at(0, weekly=18)])
        self.assertEqual(roost._burn_rates(), {})

    def test_rows_outside_the_window_are_ignored(self):
        self.write([self.at(100, weekly=1), self.at(90, weekly=2),
                    self.at(80, weekly=3)])
        self.assertEqual(roost._burn_rates(), {})

    def test_a_flat_week_reports_no_rate_rather_than_zero(self):
        # A zero rate would divide into an infinite runway; the caps panel
        # falls back to even-burn pacing instead.
        self.write([self.at(8, weekly=20), self.at(4, weekly=20),
                    self.at(0, weekly=20)])
        self.assertNotIn("weekly", roost._burn_rates())

    def test_each_cap_is_tracked_independently(self):
        self.write([self.at(8, weekly=10, fable=40),
                    self.at(4, weekly=14, fable=40),
                    self.at(0, weekly=18, fable=48)])
        rates = roost._burn_rates()
        self.assertAlmostEqual(rates["weekly"], 1.0, places=6)
        self.assertAlmostEqual(rates["fable"], 1.0, places=6)

    def test_no_history_file_is_not_fatal(self):
        self.assertEqual(roost._burn_rates(), {})

    def test_a_malformed_row_is_not_fatal(self):
        roost.USAGE_HISTORY.write_text("{not json\n", encoding="utf-8")
        self.assertEqual(roost._burn_rates(), {})


class TestIsoToEpoch(unittest.TestCase):
    """Ollama's expires_at is the only RFC3339 roost parses. A format surprise
    has to drop the unload timer, never the LOCAL MODELS panel."""

    def test_offsets_resolve_to_the_same_instant(self):
        self.assertEqual(roost._iso_to_epoch("2026-08-01T15:04:05-07:00"),
                         roost._iso_to_epoch("2026-08-01T22:04:05+00:00"))

    def test_fractional_seconds_are_accepted(self):
        self.assertIsNotNone(roost._iso_to_epoch("2026-08-01T15:04:05.123456-07:00"))

    def test_garbage_drops_the_timer_not_the_panel(self):
        for bad in (None, "", "soon", "2026-13-45T99:99:99"):
            self.assertIsNone(roost._iso_to_epoch(bad))


class TestProxyLogActivity(unittest.TestCase):
    """LiteLLM's log format is LiteLLM's to change. Anything unparseable has to
    cost the two activity numbers and nothing else."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.log = Path(self.td.name) / "proxy.log"

    def tearDown(self):
        self.td.cleanup()

    def write(self, lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def stamp(self, secs_ago):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(time.time() - secs_ago))

    def test_a_recent_request_is_dated_and_counted(self):
        self.write(["INFO %s POST /chat/completions 200" % self.stamp(10)])
        act = roost._proxy_log_activity(self.log)
        self.assertLess(act["last_req_secs"], 60)
        self.assertEqual(act["req_per_min"], 1)

    def test_only_the_last_minute_counts_toward_the_rate(self):
        self.write(["INFO %s POST /chat/completions 200" % self.stamp(10),
                    "INFO %s POST /chat/completions 200" % self.stamp(600)])
        act = roost._proxy_log_activity(self.log)
        self.assertEqual(act["req_per_min"], 1)

    def test_an_idle_gateway_reports_the_age_with_a_zero_rate(self):
        self.write(["INFO %s POST /chat/completions 200" % self.stamp(7200)])
        act = roost._proxy_log_activity(self.log)
        self.assertGreater(act["last_req_secs"], 3600)
        self.assertEqual(act["req_per_min"], 0)

    def test_lines_that_are_not_requests_are_ignored(self):
        self.write(["INFO %s server started" % self.stamp(5),
                    "INFO %s health probe ok" % self.stamp(5)])
        self.assertEqual(roost._proxy_log_activity(self.log),
                         {"last_req_secs": None, "req_per_min": None})

    def test_an_undated_request_line_is_ignored(self):
        self.write(["POST /chat/completions 200"])
        self.assertIsNone(roost._proxy_log_activity(self.log)["last_req_secs"])

    def test_a_missing_log_is_not_fatal(self):
        self.assertEqual(
            roost._proxy_log_activity(Path(self.td.name) / "nope.log"),
            {"last_req_secs": None, "req_per_min": None})


class TestGatewayCollect(unittest.TestCase):
    """A DB-less LiteLLM answers nothing about its own history, so every number
    on the GATEWAY panel is derived from a directory listing. That makes the
    filesystem rules -- what counts as a run, what counts as done -- the whole
    correctness surface."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "batch"
        self.root.mkdir()
        self.jobs = Path(self.td.name) / "jobs"
        self._env = {k: os.environ.get(k)
                     for k in (roost.BATCH_DIR_ENV, roost.JOBS_DIR_ENV)}
        os.environ[roost.BATCH_DIR_ENV] = str(self.root)
        os.environ[roost.JOBS_DIR_ENV] = str(self.jobs)
        self._port_open = roost.port_open
        roost.port_open = lambda port, timeout=0.35: True

    def tearDown(self):
        roost.port_open = self._port_open
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()

    def run_dir(self, name, outputs=0, meta=None, failures=0, age_secs=0):
        d = self.root / name
        d.mkdir()
        for i in range(outputs):
            p = d / ("item-%03d.json" % i)
            p.write_text("{}", encoding="utf-8")
            when = time.time() - age_secs
            os.utime(p, (when, when))
        if meta is not None:
            (d / "_run.json").write_text(json.dumps(meta), encoding="utf-8")
        if failures:
            (d / "_failures.jsonl").write_text(
                "\n".join('{"id":%d}' % i for i in range(failures)) + "\n\n",
                encoding="utf-8")
        return d

    def named(self, gw, name):
        for r in gw["runs"]:
            if r["name"] == name:
                return r
        self.fail("no run named %r in %r" % (name, [r["name"] for r in gw["runs"]]))

    def test_a_dir_of_loose_json_is_not_a_batch_run(self):
        # schemas/ sits beside the runs and is full of .json; counting it as a
        # run would put a permanent phantom row on the panel.
        self.run_dir("schemas", outputs=3)
        self.assertEqual(roost.collect_gateway()["runs"], [])

    def test_the_results_naming_convention_identifies_a_run(self):
        self.run_dir("results-laneA", outputs=2)
        self.assertEqual(self.named(roost.collect_gateway(), "results-laneA")["done"], 2)

    def test_a_run_json_identifies_a_run_whatever_it_is_called(self):
        self.run_dir("laneB", outputs=1,
                     meta={"model": "gemma4-32k", "total": 4,
                           "worklist": "/srv/lists/laneB.txt"})
        r = self.named(roost.collect_gateway(), "laneB")
        self.assertEqual((r["model"], r["total"], r["worklist"]),
                         ("gemma4-32k", 4, "laneB.txt"))

    def test_underscore_files_are_bookkeeping_not_output(self):
        # _run.json and _failures.jsonl live in the same dir as the results;
        # counting them as done inflates every progress figure by two.
        self.run_dir("results-laneC", outputs=3, failures=2, meta={"total": 10})
        r = self.named(roost.collect_gateway(), "results-laneC")
        self.assertEqual((r["done"], r["failed"]), (3, 2))

    def test_blank_lines_in_the_failure_log_are_not_failures(self):
        self.run_dir("results-laneD", outputs=1, failures=2)
        self.assertEqual(self.named(roost.collect_gateway(), "results-laneD")["failed"], 2)

    def test_an_empty_results_dir_has_nothing_to_say_yet(self):
        self.run_dir("results-unstarted")
        self.assertEqual(roost.collect_gateway()["runs"], [])

    def test_rate_and_eta_come_from_the_spacing_of_the_writes(self):
        d = self.run_dir("results-paced", outputs=3)
        for i, ago in enumerate((7200, 3600, 0)):
            p = d / ("item-%03d.json" % i)
            os.utime(p, (time.time() - ago, time.time() - ago))
        (d / "_run.json").write_text(json.dumps({"total": 5}), encoding="utf-8")
        r = self.named(roost.collect_gateway(), "results-paced")
        self.assertAlmostEqual(r["rate_hr"], 1.0, places=6)
        self.assertAlmostEqual(r["eta_secs"], 7200.0, places=3)

    def test_a_finished_run_has_no_eta(self):
        self.run_dir("results-finished", outputs=2, age_secs=3600,
                     meta={"total": 2})
        self.assertIsNone(self.named(roost.collect_gateway(),
                                     "results-finished")["eta_secs"])

    def test_a_single_output_is_not_enough_to_time_a_rate(self):
        self.run_dir("results-one", outputs=1, meta={"total": 9})
        r = self.named(roost.collect_gateway(), "results-one")
        self.assertIsNone(r["rate_hr"])
        self.assertIsNone(r["eta_secs"])

    def test_a_stale_run_is_not_active_and_sorts_below_a_live_one(self):
        self.run_dir("results-cold", outputs=2,
                     age_secs=roost.BATCH_ACTIVE_SECS + 600)
        self.run_dir("results-hot", outputs=2)
        gw = roost.collect_gateway()
        self.assertEqual([r["name"] for r in gw["runs"]],
                         ["results-hot", "results-cold"])
        self.assertTrue(gw["runs"][0]["active"])
        self.assertFalse(gw["runs"][1]["active"])

    def test_the_jobs_queue_counts_files_in_flight_and_dirs_at_rest(self):
        for state in ("inbox", "running", "done", "failed"):
            (self.jobs / state).mkdir(parents=True)
        for i in range(2):
            (self.jobs / "inbox" / ("j%d.json" % i)).write_text("{}", encoding="utf-8")
        (self.jobs / "inbox" / "notes.txt").write_text("x", encoding="utf-8")
        (self.jobs / "running" / "j9.json").write_text("{}", encoding="utf-8")
        for i in range(3):
            (self.jobs / "done" / ("j%d" % i)).mkdir()
        self.assertEqual(roost.collect_gateway()["jobs"],
                         {"inbox": 2, "running": 1, "done": 3, "failed": 0})

    def test_no_jobs_dir_is_absent_rather_than_zero(self):
        # Nobody without the job queue installed should see a row of zeroes
        # implying they have an idle one.
        self.assertIsNone(roost.collect_gateway()["jobs"])

    def test_a_missing_batch_root_is_not_fatal(self):
        os.environ[roost.BATCH_DIR_ENV] = str(Path(self.td.name) / "nowhere")
        gw = roost.collect_gateway()
        self.assertEqual(gw["runs"], [])
        self.assertIsNone(gw["last_req_secs"])

    def test_the_proxy_log_is_read_from_beside_the_batch_root(self):
        (self.root.parent / "proxy.log").write_text(
            "INFO %s POST /chat/completions 200\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5)),
            encoding="utf-8")
        self.assertIsNotNone(roost.collect_gateway()["last_req_secs"])

    def test_a_down_gateway_still_reports_its_runs(self):
        roost.port_open = lambda port, timeout=0.35: False
        self.run_dir("results-laneE", outputs=1)
        gw = roost.collect_gateway()
        self.assertFalse(gw["litellm_up"])
        self.assertEqual(len(gw["runs"]), 1)


class TestRenderGateway(unittest.TestCase):
    def setUp(self):
        self._color = roost.COLOR
        roost.COLOR = False

    def tearDown(self):
        roost.COLOR = self._color

    def gw(self, **kw):
        base = {"litellm_up": True, "runs": [], "jobs": None,
                "last_req_secs": None, "req_per_min": None}
        base.update(kw)
        return base

    def batch(self, **kw):
        base = {"name": "results-laneA", "model": "gemma4-32k",
                "worklist": "laneA.txt", "done": 121, "total": 300,
                "failed": 2, "rate_hr": 64.0, "eta_secs": 10080.0,
                "last_write_secs": 35.0, "active": True}
        base.update(kw)
        return base

    def test_a_down_gateway_says_so(self):
        self.assertIn("DOWN", "\n".join(roost.render_gateway(
            self.gw(litellm_up=False))))

    def test_no_runs_is_stated_not_left_blank(self):
        self.assertIn("no batch runs found",
                      "\n".join(roost.render_gateway(self.gw())))

    def test_a_run_shows_its_progress(self):
        out = "\n".join(roost.render_gateway(self.gw(runs=[self.batch()])))
        self.assertIn("121/300", out)
        self.assertIn("64/hr", out)
        self.assertIn("1 run(s), 1 active", out)

    def test_an_unknown_total_reads_as_a_question_mark_not_a_zero(self):
        out = "\n".join(roost.render_gateway(
            self.gw(runs=[self.batch(total=None, eta_secs=None)])))
        self.assertIn("121/?", out)

    def test_a_complete_run_reads_done_rather_than_dash(self):
        out = "\n".join(roost.render_gateway(self.gw(runs=[
            self.batch(done=298, total=300, failed=2, eta_secs=None)])))
        self.assertIn("done", out)

    def test_the_jobs_line_appears_only_with_a_queue(self):
        out = "\n".join(roost.render_gateway(self.gw(
            jobs={"inbox": 0, "running": 1, "done": 12, "failed": 0})))
        self.assertIn("jobs queue: inbox 0  running 1  done 12  failed 0", out)
        self.assertNotIn("jobs queue", "\n".join(roost.render_gateway(self.gw())))

    def test_activity_figures_are_dropped_rather_than_guessed(self):
        out = "\n".join(roost.render_gateway(self.gw()))
        self.assertNotIn("req/min", out)
        self.assertNotIn("last request", out)

    def test_every_line_stays_ascii(self):
        for line in roost.render_gateway(self.gw(runs=[self.batch()])):
            for ch in line:
                self.assertLess(ord(ch), 127)


class TestRemoteHosts(unittest.TestCase):
    """A remote host is a laptop with a lid. The invariant that matters is that
    a host which has already failed once never blocks the render loop again --
    it keeps its last good row and an age saying how old that row is."""

    def setUp(self):
        self._color = roost.COLOR
        roost.COLOR = False
        self._env = os.environ.get(roost.REMOTES_ENV)
        self._fetch = roost._fetch_remote
        self.release = threading.Event()
        roost._REMOTE.clear()

    def tearDown(self):
        self.release.set()
        roost._fetch_remote = self._fetch
        roost._REMOTE.clear()
        roost.COLOR = self._color
        if self._env is None:
            os.environ.pop(roost.REMOTES_ENV, None)
        else:
            os.environ[roost.REMOTES_ENV] = self._env

    def test_hosts_are_read_from_the_environment_only(self):
        os.environ[roost.REMOTES_ENV] = " alpha , beta ,, gamma "
        roost._fetch_remote = lambda host: None
        self.assertEqual([r["host"] for r in roost.collect_remote()],
                         ["alpha", "beta", "gamma"])

    def test_a_host_that_already_failed_never_blocks_again(self):
        os.environ[roost.REMOTES_ENV] = "closed-lid"
        roost._REMOTE["closed-lid"] = {"err": "timeout after 15s", "thread": None}
        roost._fetch_remote = lambda host: self.release.wait(30)
        t0 = time.perf_counter()
        rows = roost.collect_remote()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0,
                        "collect_remote blocked on a known-bad host (%.2fs)"
                        % elapsed)
        self.assertEqual(rows[0]["err"], "timeout after 15s")

    def test_the_first_attempt_is_allowed_to_block(self):
        # Without this, `roost --remote -1` would print an empty panel every
        # time, since one frame is all it gets.
        os.environ[roost.REMOTES_ENV] = "first"

        def fetch(host):
            roost._REMOTE[host] = {"data": {"workers": []}, "t": time.time(),
                                   "err": None, "thread": None}

        roost._fetch_remote = fetch
        self.assertIsNotNone(roost.collect_remote()[0]["data"])

    def test_a_fresh_cache_is_not_refetched(self):
        os.environ[roost.REMOTES_ENV] = "warm"
        roost._REMOTE["warm"] = {"data": {"workers": []}, "t": time.time(),
                                 "err": None, "thread": None}
        calls = []
        roost._fetch_remote = lambda host: calls.append(host)
        rows = roost.collect_remote()
        self.assertEqual(calls, [])
        self.assertLess(rows[0]["age_secs"], roost.REMOTE_REFRESH_SECS)

    def test_a_stale_cache_is_refetched_without_losing_the_old_row(self):
        os.environ[roost.REMOTES_ENV] = "stale"
        old = {"workers": [{"idle_secs": 1}]}
        roost._REMOTE["stale"] = {
            "data": old, "t": time.time() - 10 * roost.REMOTE_REFRESH_SECS,
            "err": None, "thread": None}
        roost._fetch_remote = lambda host: self.release.wait(30)
        row = roost.collect_remote()[0]
        self.assertIs(row["data"], old)
        self.assertGreater(row["age_secs"], roost.REMOTE_REFRESH_SECS)


class TestFetchRemote(unittest.TestCase):
    """`ssh host roost --json` is the whole transport. A failure has to degrade
    to an error string on a row, never an exception out of a render thread."""

    class FakeSubprocess(object):
        TimeoutExpired = subprocess.TimeoutExpired

        def __init__(self, result=None, raises=None):
            self.result, self.raises, self.calls = result, raises, []

        def run(self, cmd, **kw):
            self.calls.append((cmd, kw))
            if self.raises is not None:
                raise self.raises
            return self.result

    class FakeCompleted(object):
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self._subprocess = roost.subprocess
        self._cmd_env = os.environ.get(roost.REMOTE_CMD_ENV)
        roost._REMOTE.clear()

    def tearDown(self):
        roost.subprocess = self._subprocess
        roost._REMOTE.clear()
        if self._cmd_env is None:
            os.environ.pop(roost.REMOTE_CMD_ENV, None)
        else:
            os.environ[roost.REMOTE_CMD_ENV] = self._cmd_env

    def test_a_good_fetch_stores_the_payload_and_clears_the_error(self):
        roost._REMOTE["h"] = {"err": "old failure", "thread": object()}
        roost.subprocess = self.FakeSubprocess(
            self.FakeCompleted(stdout=json.dumps({"workers": []})))
        roost._fetch_remote("h")
        st = roost._REMOTE["h"]
        self.assertEqual(st["data"], {"workers": []})
        self.assertIsNone(st["err"])
        self.assertIsNone(st["thread"])

    def test_a_failed_fetch_keeps_the_last_good_data(self):
        # The closed-lid case: the row must not blank out, it must go stale.
        good = {"workers": [{"idle_secs": 2}]}
        roost._REMOTE["h"] = {"data": good, "t": 1000.0, "err": None,
                              "thread": object()}
        roost.subprocess = self.FakeSubprocess(
            self.FakeCompleted(returncode=255, stderr="ssh: connect: timed out\n"))
        roost._fetch_remote("h")
        st = roost._REMOTE["h"]
        self.assertIs(st["data"], good)
        self.assertEqual(st["t"], 1000.0)
        self.assertIn("timed out", st["err"])

    def test_a_timeout_becomes_an_error_string(self):
        roost.subprocess = self.FakeSubprocess(
            raises=subprocess.TimeoutExpired(cmd="ssh", timeout=15))
        roost._fetch_remote("h")
        self.assertIn("timeout", roost._REMOTE["h"]["err"])

    def test_unparseable_output_is_an_error_not_a_crash(self):
        roost.subprocess = self.FakeSubprocess(
            self.FakeCompleted(stdout="ssh banner, then not json"))
        roost._fetch_remote("h")
        self.assertIsNotNone(roost._REMOTE["h"]["err"])
        self.assertIsNone(roost._REMOTE["h"].get("data"))

    def test_ssh_is_never_allowed_to_prompt(self):
        # An interactive password prompt on a background thread hangs the fetch
        # forever with nothing on screen to explain it.
        roost.subprocess = self.FakeSubprocess(
            self.FakeCompleted(stdout=json.dumps({})))
        roost._fetch_remote("h")
        cmd, kw = roost.subprocess.calls[0]
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(kw["timeout"], roost.REMOTE_TIMEOUT_SECS)

    def test_the_remote_command_is_overridable(self):
        os.environ[roost.REMOTE_CMD_ENV] = "/opt/roost/roost --json"
        roost.subprocess = self.FakeSubprocess(
            self.FakeCompleted(stdout=json.dumps({})))
        roost._fetch_remote("h")
        self.assertEqual(roost.subprocess.calls[0][0][-1],
                         "/opt/roost/roost --json")


class TestRenderRemote(unittest.TestCase):
    def setUp(self):
        self._color = roost.COLOR
        roost.COLOR = False

    def tearDown(self):
        roost.COLOR = self._color

    def row(self, **kw):
        base = {"host": "hyrule", "data": None, "age_secs": None, "err": None,
                "fetching": False}
        base.update(kw)
        return base

    def payload(self, **kw):
        base = {
            "workers": [{"idle_secs": 5}, {"idle_secs": 9000},
                        {"idle_secs": None}],
            "local_models": [{"name": "qwen-coder-16k", "resident": True},
                             {"name": "gemma4-32k", "resident": False}],
            "gateway": {"runs": [{"name": "results-laneA", "done": 3,
                                  "total": 9, "active": True}],
                        "jobs": {"inbox": 0, "running": 1, "done": 12,
                                 "failed": 0}},
        }
        base.update(kw)
        return base

    def test_no_hosts_names_the_variable_that_sets_them(self):
        self.assertIn(roost.REMOTES_ENV, "\n".join(roost.render_remote([])))

    def test_a_host_summarises_its_fleet(self):
        out = "\n".join(roost.render_remote(
            [self.row(data=self.payload(), age_secs=12.0)]))
        self.assertIn("hyrule", out)
        self.assertIn("qwen-coder-16k", out)
        self.assertIn("results-laneA 3/9", out)
        self.assertIn("in 0 run 1 fail 0", out)

    def test_working_counts_only_the_sessions_moving_right_now(self):
        rows = roost.render_remote(
            [self.row(data=self.payload(), age_secs=12.0)])
        body = rows[-1].split()
        self.assertEqual((body[1], body[2]), ("3", "1"))

    def test_an_idle_worker_with_no_timestamp_is_not_counted_as_working(self):
        out = roost.render_remote([self.row(
            data=self.payload(workers=[{"idle_secs": None}]),
            age_secs=1.0)])[-1].split()
        self.assertEqual((out[1], out[2]), ("1", "0"))

    def test_a_host_still_being_fetched_says_so(self):
        self.assertIn("fetching...", "\n".join(
            roost.render_remote([self.row(fetching=True)])))

    def test_an_unreachable_host_shows_its_error(self):
        self.assertIn("timeout after 15s", "\n".join(
            roost.render_remote([self.row(err="timeout after 15s")])))

    def test_an_old_row_is_marked_stale_rather_than_shown_as_current(self):
        out = "\n".join(roost.render_remote([self.row(
            data=self.payload(), age_secs=4 * roost.REMOTE_REFRESH_SECS)]))
        self.assertIn("(stale)", out)

    def test_a_recent_row_is_not_marked_stale(self):
        out = "\n".join(roost.render_remote(
            [self.row(data=self.payload(), age_secs=5.0)]))
        self.assertNotIn("(stale)", out)

    def test_a_host_with_nothing_running_renders_dashes_not_blanks(self):
        out = "\n".join(roost.render_remote([self.row(
            data={"workers": []}, age_secs=1.0)]))
        self.assertIn("hyrule", out)
        self.assertIn("-", out)


class TestJsonContract(unittest.TestCase):
    """`--json` is not only a pipe for humans: the REMOTE panel renders other
    machines' roost from exactly this payload. Dropping a key here breaks that
    panel silently, on the other machine, with no error anywhere."""

    PATCHED = ("collect_workers", "collect_infra", "collect_usage_caps",
               "collect_local_models", "collect_gateway")

    def setUp(self):
        self._orig = {n: getattr(roost, n) for n in self.PATCHED}
        self._argv, self._stdout = sys.argv, sys.stdout
        self._color = roost.COLOR
        roost.COLOR = False
        roost.collect_workers = lambda: [worker(idle_secs=5),
                                         worker(name="demo-b2", idle_secs=9000)]
        roost.collect_infra = lambda: [{"name": "ollama", "port": 11434,
                                        "up": True, "detail": ""}]
        roost.collect_usage_caps = lambda: None
        roost.collect_local_models = lambda: [
            {"name": "qwen-coder-16k", "disk_gb": 9.2, "resident": True,
             "vram_gb": 9.2, "expires_secs": 300.0}]
        roost.collect_gateway = lambda: {
            "litellm_up": True, "runs": [], "jobs": None,
            "last_req_secs": None, "req_per_min": None}

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(roost, n, fn)
        sys.argv, sys.stdout = self._argv, self._stdout
        roost.COLOR = self._color

    def emit(self):
        sys.argv = ["roost", "--json"]
        sys.stdout = io.StringIO()
        try:
            roost.main()
            return json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = self._stdout

    def test_the_payload_carries_every_section(self):
        self.assertEqual(set(self.emit()),
                         {"workers", "infra", "usage_caps", "local_models",
                          "gateway"})

    def test_the_payload_renders_as_a_remote_row(self):
        out = "\n".join(roost.render_remote(
            [{"host": "hyrule", "data": self.emit(), "age_secs": 3.0,
              "err": None, "fetching": False}]))
        self.assertIn("qwen-coder-16k", out)

    def test_worker_rows_carry_the_idle_field_remote_counts_on(self):
        for w in self.emit()["workers"]:
            self.assertIn("idle_secs", w)

    def test_json_exits_before_touching_the_terminal(self):
        # --json is for pipes; enabling VT or arming the key reader here would
        # corrupt the output and leave a non-tty in cbreak.
        self.emit()  # must not raise on a StringIO stdout


class TestCursorAdapter(unittest.TestCase):
    """Cursor composers land through agent-transcript JSONL, not pid files."""

    FIXTURE = Path(__file__).parent / "fixtures" / "cursor" / "sample.jsonl"

    def test_cursor_project_slug_matches_cursor_layout(self):
        self.assertEqual(
            roost.cursor_project_slug(r"C:\Users\gmhow\dev\roost"),
            "c-Users-gmhow-dev-roost")

    def test_scan_cursor_transcript_reads_model_usage_and_task(self):
        info = roost.scan_cursor_transcript(str(self.FIXTURE))
        self.assertEqual(info["model"], "composer-2.5")
        self.assertEqual(info["ctx_tokens"], 23000)  # input + cache_read on newest turn
        self.assertEqual(info["prompt"], "Add cursor support to roost")
        self.assertEqual(len(info["ctx_history"]), 2)

    def test_collect_cursor_workers_respects_backends_and_idle_window(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name) / "projects" / "c-demo-roost" / "agent-transcripts"
        comp = root / "abc12345-aaaa-bbbb-cccc-ddddeeeeffff"
        comp.mkdir(parents=True)
        dest = comp / "abc12345-aaaa-bbbb-cccc-ddddeeeeffff.jsonl"
        shutil.copy(self.FIXTURE, dest)
        old_home = roost.CURSOR_HOME
        old_backends = os.environ.get(roost.BACKENDS_ENV)
        old_idle = roost.CURSOR_MAX_IDLE_SECS
        try:
            roost.CURSOR_HOME = Path(td.name)
            roost.CURSOR_PROJECTS_DIR = roost.CURSOR_HOME / "projects"
            roost.CURSOR_MAX_IDLE_SECS = 86400
            os.environ[roost.BACKENDS_ENV] = "cursor"
            rows = roost.collect_cursor_workers()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "cursor")
            self.assertEqual(rows[0]["session_id"], "abc12345-aaaa-bbbb-cccc-ddddeeeeffff")
            self.assertEqual(rows[0]["name"], "cursor/abc12345")
            self.assertIsNone(rows[0]["pid"])
        finally:
            roost.CURSOR_HOME = old_home
            roost.CURSOR_PROJECTS_DIR = old_home / "projects"
            roost.CURSOR_MAX_IDLE_SECS = old_idle
            if old_backends is None:
                os.environ.pop(roost.BACKENDS_ENV, None)
            else:
                os.environ[roost.BACKENDS_ENV] = old_backends
            td.cleanup()

    def test_mixed_fleet_shows_src_column(self):
        out = "\n".join(roost.render([
            worker(source="claude", idle_secs=5, ctx_pct=85.0),
            worker(source="cursor", name="cursor/abc", pid=None,
                   model="composer-2.5", idle_secs=5, ctx_pct=85.0),
        ]))
        self.assertIn("SRC", out)
        self.assertIn("cursor", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
