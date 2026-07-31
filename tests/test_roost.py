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
import subprocess
import sys
import tempfile
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
        "name": "demo-a1", "pid": 1234, "session_id": "sid-1",
        "cwd": "/tmp/demo", "project": "demo", "model": "claude-opus-5",
        "ctx_tokens": 50000, "ctx_pct": 25.0, "window": "200k",
        "idle_secs": 120.0, "age_secs": 600.0, "task": "a task", "task_src": "title",
    }
    base.update(kw)
    return base


class TestWindowInference(unittest.TestCase):
    """A 1M-window session reads 480k+ cache tokens in one call; scoring that
    against 200k is what produced a nonsense '242%' reading."""

    def test_small_usage_picks_200k(self):
        self.assertEqual(roost.window_for(100000), (200000, "200k"))

    def test_usage_over_200k_escalates_to_1m(self):
        self.assertEqual(roost.window_for(484030), (1000000, "1M"))

    def test_boundary_is_inclusive(self):
        self.assertEqual(roost.window_for(200000)[1], "200k")

    def test_absurd_usage_clamps_to_largest_tier(self):
        self.assertEqual(roost.window_for(9_000_000)[1], "1M")

    def test_no_percentage_exceeds_100_for_known_tiers(self):
        for tokens in (1, 199_999, 200_000, 200_001, 999_999, 1_000_000):
            size, _ = roost.window_for(tokens)
            self.assertLessEqual(100.0 * tokens / size, 100.0)


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
        self.assertIn("no live Claude Code sessions", "\n".join(roost.render([])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
