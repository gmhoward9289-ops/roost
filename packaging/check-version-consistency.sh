#!/usr/bin/env bash
# Assert every version-bearing artifact agrees with __version__ in roost.py.
#
# roost.py is the single source of truth, and most consumers already derive from
# it: build-deb.sh seds it, hatch reads it via [tool.hatch.version]. Two do not,
# because they embed the version as literal text, and both silently drifted on
# the v0.3 bump -- which touched only README.md and roost.py:
#
#   packaging/roost.rb  pinned to the v0.2 tarball. Homebrew derives `version`
#                       from the URL, so the formula's own
#                       `assert_match "roost #{version}"` interpolated 0.2, the
#                       v0.2 tarball really does print 0.2, and the test PASSED.
#                       `brew install` shipped 0.2 with nothing able to notice.
#   roost.1             .TH header said "roost 0.2" while --version said 0.3.
#                       build-deb.sh gzips it verbatim and pyproject ships it as
#                       wheel shared-data, so every channel carried the mismatch.
#
# release.yml printed "version / tag / deb / man : $code_version" while checking
# only the wheel. Nothing read roost.1 or roost.rb. This script is what makes
# that line true, and it runs in ci.yml so the drift fails on the bump PR rather
# than at release time.

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' roost.py)
if [ -z "$VERSION" ]; then
  echo "FATAL: could not read __version__ from roost.py" >&2
  exit 2
fi

echo "roost.py __version__ = $VERSION"
fail=0

report() { # <artifact> <found> <want>
  if [ "$2" = "$3" ]; then
    printf '  ok    %-22s %s\n' "$1" "$2"
  else
    printf '  DRIFT %-22s found %-10s want %s\n' "$1" "${2:-<unparseable>}" "$3" >&2
    fail=1
  fi
}

# --- man page: .TH ROOST 1 "<date>" "roost <version>" "User Commands" --------
man_version=$(sed -n '1s/.*"roost \([^"]*\)".*/\1/p' roost.1)
report "roost.1 .TH header" "$man_version" "$VERSION"

# --- Homebrew formula ---------------------------------------------------------
# The version appears in the formula EXACTLY ONCE, on the marked `version`
# line; the url interpolates it. The old shape embedded it twice on the url
# line and release-please rewrote only the first occurrence -- v0.6.0 failed
# this very gate on .../download/v0.6.0/roost_top-0.5.0.tar.gz. So the url
# check is structural now: assert the interpolation is present, so a literal
# version can never sneak back into a line release-please half-updates.
rb_version=$(sed -n 's/^\s*version "\([^"]*\)".*/\1/p' packaging/roost.rb)
report "roost.rb version" "$rb_version" "$VERSION"

if grep -qE '^\s*url ".*download/v#\{version\}/roost_top-#\{version\}\.tar\.gz"' \
     packaging/roost.rb; then
  printf '  ok    %-22s interpolates #{version}\n' "roost.rb url"
else
  echo "  DRIFT roost.rb url          must interpolate #{version} for BOTH the tag and the filename" >&2
  fail=1
fi

# No other version literal may appear anywhere in the formula -- one copy, on
# the marked line, is the whole design.
# (python@3.x interpreter pins are not release versions; exclude them.)
rb_literals=$(grep -oE '\b[0-9]+\.[0-9]+(\.[0-9]+)?\b' packaging/roost.rb | grep -vE '^3\.[0-9]+$' | sort -u)
if [ "$rb_literals" = "$VERSION" ]; then
  printf '  ok    %-22s single version literal\n' "roost.rb literals"
else
  echo "  DRIFT roost.rb literals      found: $(echo $rb_literals | tr '\n' ' ') want only: $VERSION" >&2
  fail=1
fi

# --- pyproject: version must be sourced from roost.py, not restated -----------
# A literal `version = "..."` here would be a third copy to drift.
if grep -qE '^\s*version\s*=\s*"' pyproject.toml; then
  echo "  DRIFT pyproject.toml         has a literal version=; it must stay dynamic" >&2
  echo "        (keep [tool.hatch.version] path = \"roost.py\" as the only source)" >&2
  fail=1
else
  printf '  ok    %-22s dynamic (from roost.py)\n' "pyproject.toml"
fi

# --- npm package: a literal version, and the one artifact that cannot match ----
# npm rejects a two-component version, so package.json carries 0.4.0 where
# roost.py says 0.4. Compare against the padded form rather than exempting it --
# padding is a known transform, and "exempt" would mean 0.4.0 could sit there
# forever after roost.py moved to 0.5.
case $VERSION in
  *.*.*) NPM_WANT=$VERSION ;;
  *.*)   NPM_WANT=$VERSION.0 ;;
  *)     NPM_WANT=$VERSION.0.0 ;;
esac
npm_version=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
              package.json | head -1)
report "package.json version" "$npm_version" "$NPM_WANT"

# --- --version output ---------------------------------------------------------
cli_version=$(python3 roost.py --version 2>&1 | sed -n 's/^roost \(.*\)$/\1/p')
report "roost --version" "$cli_version" "$VERSION"

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

Version drift. Every artifact above must say $VERSION.

  roost.1            .TH ROOST 1 "<date>" "roost $VERSION" "User Commands"
  package.json       "version": "$NPM_WANT"   (npm needs three components)
  packaging/roost.rb url     ...releases/download/v$VERSION/roost_top-$VERSION.tar.gz
                     version "$VERSION"
                     and refresh sha256:
                       curl -sL https://github.com/gmhoward9289-ops/roost/releases/download/v$VERSION/roost_top-$VERSION.tar.gz | shasum -a 256

A stale formula does not fail loudly: with an explicit \`version\` line, Homebrew's
built-in version assertion checks that stale number against a tarball that still
agrees with it, and passes.
EOF
  exit 1
fi

echo "all version-bearing artifacts agree on $VERSION"
