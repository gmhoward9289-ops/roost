# The demo recordings

The GIFs are recorded against a staged fleet: real roost, unmodified, reading
synthetic `~/.claude` state under `/tmp/roost-demo-home` — session files backed
by live pids, transcripts that keep ticking, subagents that appear
mid-recording, and localhost stubs so the INFRA line has something to probe.
Staged data, real reads.

To re-record (Linux/macOS; needs [vhs](https://github.com/charmbracelet/vhs)
and ffmpeg):

```bash
python3 setup_fleet.py &   # stages the fleet, keeps it alive ~5 minutes
vhs hero.tape              # the full tour
vhs loop.tape              # the short ambient loop
```

The stager caps every session's token growth so nothing drifts out of its
bucket while vhs warms up, and spawns the third subagent 9 seconds after the
tape touches `/tmp/roost-go` — so the spawn always lands on camera.
