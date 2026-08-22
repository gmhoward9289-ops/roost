#!/usr/bin/env bash
# Rewrite the roost stanza in a swamplink tools/versions.json catalog.
#
# https://swamplink.com/tools/versions.json is NOT in this GitHub repo. It
# lives in swamplink-root (lynx:/srv/git/swamplink-root.git) alongside every
# other page on the site, the same repo xycalc splices its calculator into.
# The landing page at /tools/roost/ reads the roost.version field from that
# catalog (GitHub latest and PyPI can move while the catalog stays behind --
# that is issue #93).
#
# This script is the roost-owned half of the update: it takes a catalog file
# and replaces only the "roost" object from packaging/swamplink-roost.json.
# leghorn, legbar, and any later keys are left alone. release.yml clones
# swamplink-root and runs this; tests run it against a fixture.
#
# Usage:
#   packaging/publish-swamplink-roost.sh --catalog PATH [--date YYYY-MM-DD]
set -euo pipefail
cd "$(dirname "$0")/.."

CATALOG=""
DATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --catalog) CATALOG=${2:?}; shift 2 ;;
    --date)    DATE=${2:?}; shift 2 ;;
    *) echo "usage: $0 --catalog PATH [--date YYYY-MM-DD]" >&2; exit 2 ;;
  esac
done
if [ -z "$CATALOG" ]; then
  echo "usage: $0 --catalog PATH [--date YYYY-MM-DD]" >&2
  exit 2
fi

export ROOST_SWAMPLINK_CATALOG=$CATALOG
export ROOST_SWAMPLINK_DATE=$DATE
python3 - <<'PY'
import json, os, sys
from pathlib import Path

root = Path(".").resolve()
fragment_path = root / "packaging" / "swamplink-roost.json"
catalog_path = Path(os.environ["ROOST_SWAMPLINK_CATALOG"]).resolve()
date_override = os.environ.get("ROOST_SWAMPLINK_DATE") or ""

fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
version = fragment.get("version")
if not isinstance(version, str) or not version:
    sys.exit(f"FATAL: {fragment_path} has no string 'version'")

date = date_override or fragment.get("date")
if not date:
    sys.exit("FATAL: no date: pass --date or set packaging/swamplink-roost.json date")

if not catalog_path.is_file():
    sys.exit(f"FATAL: catalog not found: {catalog_path}")

catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
if not isinstance(catalog, dict):
    sys.exit(f"FATAL: {catalog_path} is not a JSON object")
if "roost" not in catalog:
    sys.exit(f"FATAL: {catalog_path} has no 'roost' key -- refusing to invent the catalog shape")

catalog["roost"] = {"version": version, "date": date}
catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
print(f"updated {catalog_path} roost {version} ({date})")
PY
