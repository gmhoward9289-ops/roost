#!/usr/bin/env bash
# One command that answers "is roost actually listed everywhere, and if not,
# what exactly is left to do?" Safe to run any time: read-only against every
# channel, prints one PASS/PENDING line per channel and the exact command or
# URL for anything pending.
#
# The one-time setups it checks for (npm trusted publisher, PyPI pending
# publisher, the tap PAT) live in web UIs and cannot be scripted; this script
# exists so nobody has to remember which of them happened.
#
# Ported from leghorn's copy on 2026-08-11, after roost's npm channel was found
# frozen at 0.6.1 while PyPI served 0.8.0 -- two releases where `npm i -g
# roost-top` handed people old software. The npm job had been failing with a
# 404-on-PUT (npm's way of saying unauthorized) since v0.7.0, and because
# release.yml lets each publish job fail alone so the rest still land, the red
# job scrolled away unread.
#
# Note the name split: the distribution is roost-top on both npm and PyPI
# because `roost` was taken on both (on npm it is an unrelated "System
# provisioning toolkit"). The command, module, and repo stay roost.
set -u

OWNER=gmhoward9289-ops
REPO=$OWNER/roost
DIST=roost-top          # npm + PyPI name; see note above
WINGET_ID=$OWNER.roost                  # winget package identifier
WINGET_ID_PATH=${WINGET_ID//./\/}       # ...as its path under manifests/g/
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$(dirname "$0")/../roost.py")
PAGES=https://$OWNER.github.io/roost
fail=0

say()  { printf '  %-9s %-14s %s\n' "$1" "$2" "$3"; }
pend() { say PENDING "$1" "$2"; fail=1; }

echo "roost publish doctor -- version $VERSION"

# --- GitHub release ----------------------------------------------------------
assets=$(gh release view "v$VERSION" --repo "$REPO" --json assets --jq '.assets[].name' 2>/dev/null)
case "$assets" in
  *whl*) say PASS "gh release" "v$VERSION with $(wc -w <<<"$assets" | tr -d ' ') assets" ;;
  *) pend "gh release" "cut the tag: git tag -a v$VERSION && git push github v$VERSION" ;;
esac

# --- Homebrew ----------------------------------------------------------------
# Match the release-asset URL, not GitHub's auto-generated archive/refs/tags/
# one: the formula points at releases/download/ so `brew install` counts toward
# the release download stats. leghorn's copy of this check grepped for the old
# shape and reported a stale tap on every release for weeks.
rb=$(curl -sf "https://raw.githubusercontent.com/$OWNER/homebrew-tap/master/Formula/roost.rb")
if grep -q "download/v$VERSION/" <<<"$rb"; then
  say PASS brew "brew install $OWNER/tap/roost"
else
  pend brew "formula missing or stale in the tap; set TAP_PUSH_TOKEN and rerun release, or copy packaging/roost.rb by hand"
fi

# --- npm ---------------------------------------------------------------------
npm_ver=$(curl -sf "https://registry.npmjs.org/$DIST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist-tags"]["latest"])' 2>/dev/null)
if [ "${npm_ver:-}" = "$VERSION" ] || [ "${npm_ver:-}" = "$VERSION.0" ]; then
  say PASS npm "npm i -g $DIST ($npm_ver)"
elif [ -n "${npm_ver:-}" ]; then
  pend npm "registry has $npm_ver, want $VERSION -- register the Trusted Publisher at https://www.npmjs.com/package/$DIST/access (repo $REPO, workflow release.yml, environment npm, allow npm publish), then rerun the release's npm job"
else
  pend npm "nothing published as $DIST -- first publish is manual: npm publish (browser auth), then register the Trusted Publisher at https://www.npmjs.com/package/$DIST/access"
fi

# --- PyPI --------------------------------------------------------------------
pypi_ver=$(curl -sf "https://pypi.org/pypi/$DIST/json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
if [ "${pypi_ver:-}" = "$VERSION" ]; then
  say PASS pypi "pipx install $DIST ($pypi_ver)"
else
  pend pypi "registry has ${pypi_ver:-nothing}, want $VERSION -- check the pending publisher at https://pypi.org/manage/account/publishing/ (project $DIST, repo $REPO, workflow release.yml, environment pypi), then rerun the release's pypi job"
fi

# --- apt ---------------------------------------------------------------------
if curl -sf "$PAGES/dists/stable/InRelease" | grep -q "Origin: roost"; then
  ver_in_pool=$(curl -sf "$PAGES/dists/stable/main/binary-all/Packages" | sed -n 's/^Version: //p' | sort -V | tail -1)
  if [ "$ver_in_pool" = "$VERSION" ]; then
    say PASS apt "signed repo serving $ver_in_pool"
  else
    pend apt "repo serves ${ver_in_pool:-nothing}, want $VERSION -- rerun the release's apt-repo job"
  fi
else
  pend apt "no signed InRelease at $PAGES -- set ROOST_APT_GPG_PRIVATE_KEY and rerun the release's apt-repo job"
fi

# --- winget ------------------------------------------------------------------
# The manifest directory is the registry: one subdirectory per published
# version. It only appeared at all once microsoft/winget-pkgs#411432 (the
# hand-reviewed bootstrap PR for a brand-new identifier) merged on 2026-08-11 --
# before that the release job's winget step skipped itself with a notice.
wg_ver=$(gh api "repos/microsoft/winget-pkgs/contents/manifests/g/$WINGET_ID_PATH" \
           --jq '.[] | select(.type == "dir") | .name' 2>/dev/null | sort -V | tail -1)
if [ "${wg_ver:-}" = "$VERSION" ]; then
  say PASS winget "winget install $WINGET_ID ($wg_ver)"
elif [ -n "${wg_ver:-}" ]; then
  pend winget "winget-pkgs has $wg_ver, want $VERSION -- winget-releaser only runs on a tag, so this clears on the next release; to catch up now, rerun the release's winget job"
else
  pend winget "$WINGET_ID is not in winget-pkgs -- a brand-new identifier needs a hand-written PR merged by Microsoft's moderators before winget-releaser can update it"
fi

# --- swamplink tools catalog -------------------------------------------------
# Live file is out of repo (swamplink-root). This check is how drift like
# issue #93 (catalog frozen at 0.10.1 while GitHub/PyPI served 0.11.0) shows
# up as PENDING instead of a stale landing page nobody diffs.
swamplink_json=$(curl -sf --max-time 20 "https://swamplink.com/tools/versions.json" 2>/dev/null || true)
if [ -n "$swamplink_json" ]; then
  swamplink_ver=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["roost"]["version"])' <<<"$swamplink_json" 2>/dev/null || true)
  if [ "${swamplink_ver:-}" = "$VERSION" ]; then
    say PASS swamplink "https://swamplink.com/tools/versions.json roost $swamplink_ver"
  else
    pend swamplink "catalog has ${swamplink_ver:-unparseable}, want $VERSION -- bump tools/versions.json in swamplink-root (see docs/swamplink.md), or set SWAMPLINK_* secrets and rerun the release's swamplink-tools job"
  fi
else
  say SKIP swamplink "https://swamplink.com/tools/versions.json unreachable from here"
fi

# --- repo secrets the automation depends on ----------------------------------
# Listing secrets needs admin, and GITHUB_TOKEN does not have it -- in CI this
# can only ever report a false PENDING, which is how leghorn's first scheduled
# run went red while every channel was actually live. A missing secret already
# announces itself as a failed publish job, so CI skips it and the local run
# keeps the check.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  say SKIP "secret" "needs admin to list; run this locally to check secrets"
else
  secrets=$(gh secret list --repo "$REPO" 2>/dev/null)
  for s in ROOST_APT_GPG_PRIVATE_KEY TAP_PUSH_TOKEN ROOST_RELEASE_PLEASE WINGET_PAT; do
    if grep -q "^$s" <<<"$secrets"; then
      say PASS "secret" "$s"
    else
      pend "secret" "$s missing: gh secret set $s --repo $REPO"
    fi
  done
fi

echo
if [ "$fail" = 0 ]; then
  echo "all channels live for $VERSION"
else
  echo "PENDING items above; rerun failed release jobs afterwards with:"
  echo "  gh run rerun \$(gh run list --repo $REPO --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId') --failed --repo $REPO"
fi
