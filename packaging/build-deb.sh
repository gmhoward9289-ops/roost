#!/bin/sh
# Build a .deb for roost. Usage: packaging/build-deb.sh [version]
#
# Deliberately a plain dpkg-deb tree rather than a debian/ source package: roost
# is one architecture-independent script with no build step and no dependencies
# beyond python3 itself, so debhelper would add ceremony and no correctness.
# The result is meant to be attached to a GitHub release and installed with
#   sudo apt install ./roost_<version>_all.deb
# which resolves python3 the same way an apt repo would. There is no PPA.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/roost.py")}
[ -n "$VERSION" ] || { echo "could not determine version" >&2; exit 1; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
PKG="$BUILD/roost_${VERSION}_all"

mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/man/man1" \
         "$PKG/usr/share/doc/roost"

# Installed as `roost`, not `roost.py`: the shebang and the executable bit are
# what make it a command, and the .py suffix only matters on Windows.
install -m 0755 "$ROOT/roost.py" "$PKG/usr/bin/roost"
gzip -9nc "$ROOT/roost.1" > "$PKG/usr/share/man/man1/roost.1.gz"
chmod 0644 "$PKG/usr/share/man/man1/roost.1.gz"
install -m 0644 "$ROOT/LICENSE" "$PKG/usr/share/doc/roost/copyright"

cat > "$PKG/DEBIAN/control" <<EOF
Package: roost
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9)
Maintainer: George M. Howard <dev@swamplink.com>
Homepage: https://github.com/gmhoward9289-ops/roost
Description: top for Claude Code
 Shows every live Claude Code session on the machine, the model each is
 running, how much of its context window it has consumed, and the subagents
 it has spawned -- which have no process of their own and are invisible to
 any pid-based view.
 .
 Reads only local Claude Code state and probes localhost inference ports.
 It sends nothing anywhere.
EOF

dpkg-deb --build --root-owner-group "$PKG" > /dev/null
mkdir -p "$ROOT/dist"
mv "$BUILD/roost_${VERSION}_all.deb" "$ROOT/dist/"
echo "dist/roost_${VERSION}_all.deb"
