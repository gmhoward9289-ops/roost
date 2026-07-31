#!/usr/bin/env python3
"""Stage a demo fleet for the roost GIFs.

Creates a fake HOME (ROOST_DEMO_HOME, default /tmp/roost-demo-home) with
~/.claude session files backed by live pids, transcripts with realistic usage,
subagent transcripts, and localhost stubs for the INFRA line. A background
updater keeps two sessions and one subagent visibly working, walks the NEAR
LIMIT session upward, and spawns a third subagent 9s after /tmp/roost-go
appears -- which the tapes touch at the moment recording begins.

Everything is synthetic; roost itself runs unmodified against this state.
Recording: python3 setup_fleet.py &  then  vhs hero.tape  (needs vhs + ffmpeg).
"""
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEMO_HOME = Path(os.environ.get("ROOST_DEMO_HOME", "/tmp/roost-demo-home"))
CLAUDE = DEMO_HOME / ".claude"
SESSIONS = CLAUDE / "sessions"
PROJECTS = CLAUDE / "projects"

NOW = time.time()


def spawn_sleeper():
    p = subprocess.Popen(["sleep", "3600"], stdout=subprocess.DEVNULL)
    return p.pid


def usage_line(model, tokens):
    return json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {
            "input_tokens": 40,
            "cache_read_input_tokens": tokens - 240,
            "cache_creation_input_tokens": 200,
        }},
    }) + "\n"


def title_line(title):
    return json.dumps({"customTitle": title}) + "\n"


def agent_result_line(agent_id, desc, model):
    return json.dumps({"toolUseResult": {
        "agentId": agent_id, "description": desc,
        "status": "running", "resolvedModel": model,
    }}) + "\n"


def make_session(name, cwd_leaf, pid, sid, started_ago):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / ("%d.json" % pid)).write_text(json.dumps({
        "pid": pid, "sessionId": sid, "cwd": "/home/g/dev/" + cwd_leaf,
        "name": name, "startedAt": int((NOW - started_ago) * 1000),
    }))


def make_transcript(sid, title, model, tokens, idle_secs, extra_lines=()):
    slug = PROJECTS / "-home-g-dev"
    slug.mkdir(parents=True, exist_ok=True)
    path = slug / (sid + ".jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
        fh.write(title_line(title))
        for ln in extra_lines:
            fh.write(ln)
        fh.write(usage_line(model, tokens))
    t = NOW - idle_secs
    os.utime(path, (t, t))
    return path


def make_subagent(parent_sid, agent_id, first_words, model, tokens, idle_secs):
    d = PROJECTS / "-home-g-dev" / parent_sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("agent-%s.jsonl" % agent_id)
    with open(path, "w") as fh:
        fh.write(json.dumps({
            "type": "user", "isSidechain": True, "agentId": agent_id,
            "message": {"role": "user", "content": first_words},
        }) + "\n")
        fh.write(usage_line(model, tokens))
    t = NOW - idle_secs
    os.utime(path, (t, t))
    return path


class OllamaStub(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"models": [
            {"name": "qwen2.5-coder:14b", "size_vram": 9200000000}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(port, handler=None):
    if handler:
        s = HTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=s.serve_forever, daemon=True).start()
    else:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(5)

        def accept_loop():
            while True:
                try:
                    c, _ = s.accept()
                    c.close()
                except OSError:
                    return
        threading.Thread(target=accept_loop, daemon=True).start()


def main():
    if DEMO_HOME.exists():
        subprocess.run(["rm", "-rf", str(DEMO_HOME)])
    DEMO_HOME.mkdir(parents=True)

    serve(11434, OllamaStub)
    serve(4000)
    serve(8080)

    near_sid = "sid-near-1111"
    parked_sid = "sid-park-2222"
    web_sid = "sid-web-3333"
    test_sid = "sid-test-4444"

    pid = spawn_sleeper()
    make_session("parser-a1", "parser", pid, near_sid, 4 * 3600)
    near = make_transcript(near_sid, "Refactor the config parser", "claude-opus-5",
                           171000, 14)

    pid = spawn_sleeper()
    make_session("migrate-b2", "migrate", pid, parked_sid, 9 * 3600)
    make_transcript(parked_sid, "Plan the schema migration", "claude-opus-5",
                    421000, 4 * 3600 + 600)

    pid = spawn_sleeper()
    make_session("webapp-c3", "webapp", pid, web_sid, 50 * 60)
    agent_lines = (
        agent_result_line("a1b2c3d4e5", "survey the config loaders", "claude-opus-5"),
        agent_result_line("b6c7d8e9f0", "draft the migration notes", "claude-sonnet-5"),
    )
    web = make_transcript(web_sid, "Wire up the billing webhooks", "claude-fable-5",
                          46000, 3, extra_lines=agent_lines)

    pid = spawn_sleeper()
    make_session("tests-h8", "tests", pid, test_sid, 2 * 3600)
    tests = make_transcript(test_sid, "Backfill integration tests", "claude-sonnet-5",
                            112000, 8)

    pid = spawn_sleeper()
    make_session("fresh-i9", "fresh", pid, "sid-fresh-5555", 20)
    # no transcript: STARTING

    for i, (nm, leaf, tok, idle_h, title) in enumerate([
            ("docs-d4", "docs", 22000, 2.6, "Rewrite the onboarding docs"),
            ("bench-e5", "bench", 9000, 3.1, "Benchmark the tail reader"),
            ("infra-f6", "infra", 31000, 4.4, "Terraform drift check"),
            ("notes-g7", "notes", 12000, 5.2, "Weekly notes cleanup")]):
        pid = spawn_sleeper()
        sid = "sid-quiet-%d" % i
        make_session(nm, leaf, pid, sid, 8 * 3600)
        make_transcript(sid, title, "claude-sonnet-5", tok, idle_h * 3600)

    sub_working = make_subagent(web_sid, "a1b2c3d4e5",
                                "Survey the config loaders and list every call site",
                                "claude-opus-5", 68000, 2)
    make_subagent(web_sid, "b6c7d8e9f0",
                  "Draft the migration notes for the 0.3 schema",
                  "claude-sonnet-5", 134000, 1 * 3600 + 22 * 60)

    # Values are capped so nothing drifts out of its bucket while vhs takes
    # its time starting up. The third subagent spawns 9s after /tmp/roost-go
    # appears, which the tapes touch at the moment recording begins.
    marker = Path("/tmp/roost-go")
    if marker.exists():
        marker.unlink()

    def updater():
        near_tok = 171000
        web_tok = 46000
        test_tok = 112000
        sub_tok = 68000
        t0 = time.time()
        spawned = False
        go_at = None
        while time.time() - t0 < 280:
            time.sleep(2.0)
            near_tok = min(near_tok + 1400, 189000)
            web_tok = min(web_tok + 1100, 152000)
            test_tok = min(test_tok + 900, 149000)
            sub_tok = min(sub_tok + 1300, 178000)
            with open(near, "a") as fh:
                fh.write(usage_line("claude-opus-5", near_tok))
            with open(web, "a") as fh:
                fh.write(usage_line("claude-fable-5", web_tok))
            with open(tests, "a") as fh:
                fh.write(usage_line("claude-sonnet-5", test_tok))
            with open(sub_working, "a") as fh:
                fh.write(usage_line("claude-opus-5", sub_tok))
            if go_at is None and marker.exists():
                go_at = time.time()
            if not spawned and go_at and time.time() - go_at > 9:
                spawned = True
                with open(web, "a") as fh:
                    fh.write(agent_result_line(
                        "c9d0e1f2a3", "scout flaky tests in ci", "claude-fable-5"))
                make_subagent(web_sid, "c9d0e1f2a3",
                              "Scout the flaky tests in CI and rank by failure rate",
                              "claude-fable-5", 4200, 0)

    threading.Thread(target=updater, daemon=True).start()
    print("fleet staged at", DEMO_HOME)
    time.sleep(300)


if __name__ == "__main__":
    main()
