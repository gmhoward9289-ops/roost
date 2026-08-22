"""The roost stanza this repo publishes for swamplink.com/tools/versions.json.

The live catalog is out of repo (swamplink-root on lynx). This repo still owns
the roost object, and the publisher must not clobber sibling tools.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load roost.py as the version authority, not an installed wheel.
spec = importlib.util.spec_from_file_location("roost", str(ROOT / "roost.py"))
roost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roost)


class SwamplinkRoostVersion(unittest.TestCase):
    def test_fragment_matches__version__(self):
        fragment = json.loads(
            (ROOT / "packaging" / "swamplink-roost.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fragment["version"], roost.__version__)
        self.assertRegex(fragment["date"], r"^\d{4}-\d{2}-\d{2}$")

    @unittest.skipIf(os.name == "nt", "publish-swamplink-roost.sh is bash")
    def test_publisher_rewrites_only_roost(self):
        catalog = {
            "roost": {"version": "0.10.1", "date": "2026-08-21"},
            "leghorn": {"version": "0.4.14", "date": "2026-08-15"},
            "legbar": {"version": "0.3.6", "date": "2026-08-20"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "versions.json"
            path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    str(ROOT / "packaging" / "publish-swamplink-roost.sh"),
                    "--catalog",
                    str(path),
                    "--date",
                    "2026-08-21",
                ],
                check=True,
                cwd=str(ROOT),
            )
            got = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            got["roost"],
            {"version": roost.__version__, "date": "2026-08-21"},
        )
        self.assertEqual(got["leghorn"], catalog["leghorn"])
        self.assertEqual(got["legbar"], catalog["legbar"])

    @unittest.skipIf(os.name == "nt", "publish-swamplink-roost.sh is bash")
    def test_publisher_refuses_catalog_without_roost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "versions.json"
            path.write_text(
                json.dumps({"leghorn": {"version": "1.0.0", "date": "2026-01-01"}})
                + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    str(ROOT / "packaging" / "publish-swamplink-roost.sh"),
                    "--catalog",
                    str(path),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no 'roost' key", proc.stderr)


if __name__ == "__main__":
    unittest.main()
