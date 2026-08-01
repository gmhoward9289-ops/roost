# Changelog

All notable changes to `roost` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are published to [PyPI](https://pypi.org/project/roost-top/) and
[npm](https://www.npmjs.com/package/roost-top) as `roost-top`, to Homebrew via
[gmhoward9289-ops/tap](https://github.com/gmhoward9289-ops/homebrew-tap), and to
a signed apt repository — so a version number here is the same version on all
four.

## [Unreleased]

### Added

- **npm distribution** — `roost-top` is now installable with `npx roost-top` or
  `npm install -g roost-top`, alongside the existing PyPI, Homebrew and apt
  channels ([#12](https://github.com/gmhoward9289-ops/roost/pull/12)). The
  package still ships the same single-file, stdlib-only Python program; the npm
  wrapper only locates an interpreter.
- **ADVICE names the task, not just the pid**
  ([#1](https://github.com/gmhoward9289-ops/roost/pull/1)) — advice lines
  identify a session by what it is working on, so acting on them doesn't require
  cross-referencing the table first.

### Changed

- README points at the `help wanted` issues.
- Demo recording re-cut against 0.4.

## [0.4] - 2026-07-31

### Added

- **Interactive mode is now explicit, and off by default.** `i` arms and disarms
  it; the cursor, `x`, `y` and the `EXPERIMENTAL` tag all come alive together.
  Start armed with `--interactive`. Before this, a dashboard left running could
  end a session on a stray keypress
  ([#10](https://github.com/gmhoward9289-ops/roost/pull/10)).
- **Demo GIFs** in the README, plus the recording rig that produces them
  (`demo/`). The recordings are real `roost` reading a staged fleet — session
  files backed by live pids, transcripts that keep ticking, subagents appearing
  mid-recording — not mockups. `demo/README.md` documents re-recording.

## [0.3] - 2026-07-31

### Added

- **Signed apt repository**, alongside the standalone `.deb`
  ([#2](https://github.com/gmhoward9289-ops/roost/pull/2)). `apt install roost`
  now upgrades in place instead of being a one-shot local install. CI publishes
  every tagged `.deb` into the repo, hosted on GitHub Pages. Signed with a
  dedicated key held only in repository secrets; its public half is committed at
  `packaging/apt/pubkey.asc`.

### Fixed

- Corrected the recorded v0.2 tarball checksum in the Homebrew formula after the
  tag was re-cut.

## [0.2] - 2026-07-31

The release that made `roost` actionable rather than purely observational, and
gave it real install paths.

### Added

- **A cursor you can act on.** `j`/`k` move it, `x` stops the selected session
  behind a confirmation, `y` copies its session id for `claude --resume`.
  `QUIET` expands under the cursor, since long-idle sessions are what the sweep
  is for.
- **Three install paths** — Homebrew, `.deb`, and pipx-from-git — plus a `curl`
  one-liner. PyPI publication as `roost-top` landed at the end of this cycle.
- **Version stamped on the frame**, bottom-right, so a screenshot identifies
  itself.

### Fixed

- **The invisible kill confirmation.** `paint()` truncated silently, so on a
  frame taller than the terminal (24 sessions plus subagents is ~55 lines
  against a 40-row window) the confirmation prompt was clipped off-screen:
  pressing `x` looked like it did nothing, and the next keypress cancelled a
  prompt that had never been seen. The status line moved into the header, which
  is never clipped, and `paint()` now reports `... N more line(s) below` instead
  of truncating in silence.
- **`a` and `s` flip between ADVICE and SUBAGENTS** instead of stacking. Opening
  the second pushed it past the bottom of the window, so you had to close one
  panel to see the other you had just asked for.
- **Row ordering moved out of `render()` into `arrange()`**, so the screen and
  the cursor index are computed once and cannot drift apart. Two orderings that
  disagree is how you stop the wrong session. `frame()` returns the row list it
  actually rendered, and both keys act on that captured row rather than
  re-resolving an index that may have outlived its frame.
- Stopping a session is now logged.
- Test coverage extended to the platform kill itself, not just the guard
  around it.
- Packaging metadata uses the `swamplink.com` contact domain.

## [0.01] - 2026-07-30

Initial release.

### Added

- **`top` for Claude Code** — a live view of every session: model, context
  consumed, idle time, and the subagents it spawned. Single file, stdlib only,
  Python 3.9+, macOS/Linux/Windows.
- **Subagent visibility**, which pid-based tooling cannot provide: subagents run
  as sidechains inside the parent and have no process of their own. Each does
  get a transcript at
  `projects/<slug>/<sessionId>/subagents/agent-<id>.jsonl`, while the short task
  description lives only in the parent's `toolUseResult` keyed by `agentId`.
  `roost` joins the two.
- **Context accounting** from the last assistant turn's `input` +
  `cache_read` + `cache_creation`. Nothing on disk records the window size, so
  the tier is inferred — and printed, so the assumption stays visible rather
  than silently wrong.

[Unreleased]: https://github.com/gmhoward9289-ops/roost/compare/v0.4...HEAD
[0.4]: https://github.com/gmhoward9289-ops/roost/compare/v0.3...v0.4
[0.3]: https://github.com/gmhoward9289-ops/roost/compare/v0.2...v0.3
[0.2]: https://github.com/gmhoward9289-ops/roost/compare/v0.01...v0.2
[0.01]: https://github.com/gmhoward9289-ops/roost/releases/tag/v0.01
