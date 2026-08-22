# Changelog

All notable changes to `roost` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are published to [PyPI](https://pypi.org/project/roost-top/) and
[npm](https://www.npmjs.com/package/roost-top) as `roost-top`, to Homebrew via
[gmhoward9289-ops/tap](https://github.com/gmhoward9289-ops/homebrew-tap), and to
a signed apt repository — so a version number here is the same version on all
four.

Releases are cut by [release-please](.github/workflows/release-please.yml),
which prepends a generated section here when the release PR merges. There is
deliberately no `Unreleased` heading: unreleased work lives in the open release
PR, and a hand-maintained section would sort below each new generated one and
drift out of date.

## [0.12.0](https://github.com/gmhoward9289-ops/roost/compare/v0.11.1...v0.12.0) (2026-08-22)


### Added

* show cursor worker branch name instead of composer hash ([#95](https://github.com/gmhoward9289-ops/roost/issues/95)) ([1756ce2](https://github.com/gmhoward9289-ops/roost/commit/1756ce25d97dfdd1c37afdf3b22d9c8c53405d4b))

## [0.11.1](https://github.com/gmhoward9289-ops/roost/compare/v0.11.0...v0.11.1) (2026-08-22)


### Fixed

* keep the swamplink roost version line aligned with GitHub/PyPI ([#94](https://github.com/gmhoward9289-ops/roost/issues/94)) ([2ec8660](https://github.com/gmhoward9289-ops/roost/commit/2ec8660f46212eeb68965f95bb416aa40c925898))
* stop cold first paint from scanning finished sidechains ([#91](https://github.com/gmhoward9289-ops/roost/issues/91)) ([934a305](https://github.com/gmhoward9289-ops/roost/commit/934a30523dee92bb6bc46d4c95270a5ea09a7205))

## [0.11.0](https://github.com/gmhoward9289-ops/roost/compare/v0.10.1...v0.11.0) (2026-08-21)


### Added

* shell tab completion and full CLI flag docs ([#76](https://github.com/gmhoward9289-ops/roost/issues/76), [#78](https://github.com/gmhoward9289-ops/roost/issues/78)) ([2d410e6](https://github.com/gmhoward9289-ops/roost/commit/2d410e651951277efd0235b8c0235f71d165c99b))

## [0.10.1](https://github.com/gmhoward9289-ops/roost/compare/v0.10.0...v0.10.1) (2026-08-21)


### Fixed

* harden channel publish jobs and add recover-channels workflow ([#86](https://github.com/gmhoward9289-ops/roost/issues/86)) ([0ed7a49](https://github.com/gmhoward9289-ops/roost/commit/0ed7a4947b625554579e8ce8e734070ba59cb82e))


### Documentation

* regenerate roost-demo.gif from vhs tape at v0.9.0 ([#85](https://github.com/gmhoward9289-ops/roost/issues/85)) ([0a3fb10](https://github.com/gmhoward9289-ops/roost/commit/0a3fb10d4f103ac98f4fb2f7b27fdbcf0e96797d))

## [0.10.0](https://github.com/gmhoward9289-ops/roost/compare/v0.9.0...v0.10.0) (2026-08-20)


### Added

* canary, gateway aliases, TUI detail, json subagents, window env ([#82](https://github.com/gmhoward9289-ops/roost/issues/82)) ([2f65269](https://github.com/gmhoward9289-ops/roost/commit/2f6526975f41f28ee61f19c2b49a8a183184368a))


### Fixed

* correct stale MIT license references and widen the CI Python matrix ([#75](https://github.com/gmhoward9289-ops/roost/issues/75)) ([b0e71e0](https://github.com/gmhoward9289-ops/roost/commit/b0e71e0aeebb96637c99559053b6e0bfbff027cb))

## [0.9.0](https://github.com/gmhoward9289-ops/roost/compare/v0.8.1...v0.9.0) (2026-08-20)


### Added

* carry the version in --json output ([#72](https://github.com/gmhoward9289-ops/roost/issues/72)) ([cd5ccd3](https://github.com/gmhoward9289-ops/roost/commit/cd5ccd3769d4dbd61c2795464f5828cf958e0c85))
* roost.snapshot.v1 schema on --json output ([bc26903](https://github.com/gmhoward9289-ops/roost/commit/bc26903857f3cde1b9482a99d8b17e539cfed818))


### Documentation

* align license text with Apache-2.0 relicense ([0b5d3e0](https://github.com/gmhoward9289-ops/roost/commit/0b5d3e080b7387b08523993cc8188fc34fa499f9))

## [0.8.1](https://github.com/gmhoward9289-ops/roost/compare/v0.8.0...v0.8.1) (2026-08-16)


### Fixed

* **test:** stop the Cursor header fixture aging out of the idle window ([#65](https://github.com/gmhoward9289-ops/roost/issues/65)) ([2fafbd1](https://github.com/gmhoward9289-ops/roost/commit/2fafbd1b8b40c164943cfe6f5249b7a6a17d1afa))


### Documentation

* add Discussions badge and pointer to README ([#62](https://github.com/gmhoward9289-ops/roost/issues/62)) ([9af2483](https://github.com/gmhoward9289-ops/roost/commit/9af24833e4a4044d2265c9be43caa8c00fdc8952))
* record roost-top as the settled published name ([#64](https://github.com/gmhoward9289-ops/roost/issues/64)) ([615e7e5](https://github.com/gmhoward9289-ops/roost/commit/615e7e53e6e53c7ec83522abba4d95ecaf5f1e85))

## [0.8.0](https://github.com/gmhoward9289-ops/roost/compare/v0.7.0...v0.8.0) (2026-08-05)


### Added

* Cursor IDE support (draft — major revision, not ready to merge) ([#52](https://github.com/gmhoward9289-ops/roost/issues/52)) ([c88db41](https://github.com/gmhoward9289-ops/roost/commit/c88db416d729c0a5a0c7d98e4cd56c07b0c2daa5))

## [0.7.0](https://github.com/gmhoward9289-ops/roost/compare/v0.6.1...v0.7.0) (2026-08-04)


### Added

* lay the group labels and HELP panel out in rows ([#41](https://github.com/gmhoward9289-ops/roost/issues/41)) ([bb5b18b](https://github.com/gmhoward9289-ops/roost/commit/bb5b18bbb61c30d776908907f4721791a144056b))
* make INFRA panel ports configurable ([#47](https://github.com/gmhoward9289-ops/roost/issues/47)) ([49cbfca](https://github.com/gmhoward9289-ops/roost/commit/49cbfca022238770fe5813ca71485427231967fb))

## [0.6.1](https://github.com/gmhoward9289-ops/roost/compare/v0.6.0...v0.6.1) (2026-08-02)


### Fixed

* state the formula version exactly once so release-please cannot half-update it ([#39](https://github.com/gmhoward9289-ops/roost/issues/39)) ([877096c](https://github.com/gmhoward9289-ops/roost/commit/877096cb93fc62751592c4d8c7505e56a2d1ee31))

## [0.6.0](https://github.com/gmhoward9289-ops/roost/compare/v0.5.0...v0.6.0) (2026-08-02)


### Added

* add winget packaging (Windows exe build + winget-pkgs PR) ([#24](https://github.com/gmhoward9289-ops/roost/issues/24)) ([fa7a740](https://github.com/gmhoward9289-ops/roost/commit/fa7a7405b82f0905caa5cf1a5d0c7045fd03938d))
* agent types, absolute CTX, FLOW sparkline, and a USAGE panel ([#26](https://github.com/gmhoward9289-ops/roost/issues/26)) ([391269c](https://github.com/gmhoward9289-ops/roost/commit/391269c7505d36ed01d12a16fc8946bfa626c746))
* GATEWAY and REMOTE panels, model-name window resolution ([#36](https://github.com/gmhoward9289-ops/roost/issues/36)) ([84ba4cd](https://github.com/gmhoward9289-ops/roost/commit/84ba4cdbda947754cc08933074640c2339141ecc))
* total the fleet, and show what each session is adding ([#37](https://github.com/gmhoward9289-ops/roost/issues/37)) ([9c286f7](https://github.com/gmhoward9289-ops/roost/commit/9c286f78c456f8fdd4a03830431b292c92108642))


### Fixed

* restore the terminal fully on exit and stop the cache leaks ([#29](https://github.com/gmhoward9289-ops/roost/issues/29)) ([8e2b0db](https://github.com/gmhoward9289-ops/roost/commit/8e2b0db0616a03d30a8e5cb64214b781b1215b09))
* stop the render loop blocking on infra socket probes ([#33](https://github.com/gmhoward9289-ops/roost/issues/33)) ([8fb25c0](https://github.com/gmhoward9289-ops/roost/commit/8fb25c01e44414a59104cec26e4b87cc3127f97d))


### Documentation

* repo structure, changelog catch-up, contributing/security, comment fixes ([#32](https://github.com/gmhoward9289-ops/roost/issues/32)) ([8ffdca6](https://github.com/gmhoward9289-ops/roost/commit/8ffdca6a8562d5157d9993eebb6e1a6fe7255c56))

## [0.5.0](https://github.com/gmhoward9289-ops/roost/compare/v0.4...v0.5.0) (2026-08-02)

### Added

- **LOCAL MODELS panel** ([#16](https://github.com/gmhoward9289-ops/roost/pull/16))
  — surfaces the models on the box, and stops hiding the ones that aren't
  currently loaded.
- **winget install and a Windows exe** — `winget install gmhoward9289-ops.roost`
  installs a frozen executable, no Python required. The same build is attached
  to every tagged release as `roost-<version>-windows-x64.zip` for anyone who
  wants the zip directly.
- **npm distribution** — `roost-top` is now installable with `npx roost-top` or
  `npm install -g roost-top`, alongside the existing PyPI, Homebrew and apt
  channels ([#12](https://github.com/gmhoward9289-ops/roost/pull/12)). The
  package still ships the same single-file, stdlib-only Python program; the npm
  wrapper only locates an interpreter.
- **ADVICE names the task, not just the pid**
  ([#1](https://github.com/gmhoward9289-ops/roost/pull/1)) — advice lines
  identify a session by what it is working on, so acting on them doesn't require
  cross-referencing the table first.
- **`llms.txt`**, so AI crawlers get a plain-language summary of what roost is.

### Fixed

- **Homebrew formula points at a tracked release asset**
  ([`1a52b2f`](https://github.com/gmhoward9289-ops/roost/commit/1a52b2f99405d56c5f1d70d28fb1feb195d46208))
  rather than a tarball whose checksum moved.

### Changed

- **Releases run through release-please**
  ([#19](https://github.com/gmhoward9289-ops/roost/pull/19),
  [#20](https://github.com/gmhoward9289-ops/roost/pull/20)) — the version bump
  and tag are cut by merging a release PR instead of by hand, and npm and
  Homebrew tap publishing are automated on release
  ([#18](https://github.com/gmhoward9289-ops/roost/pull/18)).
- This changelog was written and backfilled to cover v0.01 through v0.4
  ([#13](https://github.com/gmhoward9289-ops/roost/pull/13)).
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

[0.4]: https://github.com/gmhoward9289-ops/roost/compare/v0.3...v0.4
[0.3]: https://github.com/gmhoward9289-ops/roost/compare/v0.2...v0.3
[0.2]: https://github.com/gmhoward9289-ops/roost/compare/v0.01...v0.2
[0.01]: https://github.com/gmhoward9289-ops/roost/releases/tag/v0.01
