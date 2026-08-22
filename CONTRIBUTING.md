# Contributing

roost is one file, `roost.py`, stdlib only. Keep it that way — no new
dependencies.

## Running tests

```bash
python -m unittest discover -s tests -v
```

That's the same command `ci.yml` runs, across Python 3.9 and 3.13 on Linux,
macOS and Windows. CI also runs `packaging/check-version-consistency.sh` and a
semgrep scan; run the consistency check locally if you touch a version string.

## Pull requests

PRs merge squash-only, and the PR title becomes the squash commit message —
`pr-title-lint.yml` enforces a [conventional commit](https://www.conventionalcommits.org/)
title (`feat: ...`, `fix: ...`, `docs: ...`, etc.), so title it accordingly.

## Releases

Releases are cut by [release-please](.github/workflows/release-please.yml), not
by hand. It keeps an open release PR up to date as conventional commits land
on `main`; merging that PR is the release approval — it bumps the version
everywhere (`roost.py`, `roost.1`, `packaging/roost.rb`,
`packaging/swamplink-roost.json`), updates `CHANGELOG.md`, and tags. `release.yml` then
publishes the tag to PyPI, npm, the Homebrew tap, and (when the swamplink
SSH secrets are set) the roost stanza of `swamplink.com/tools/versions.json`.
That catalog file itself is not in this repo — see [`docs/swamplink.md`](docs/swamplink.md).
