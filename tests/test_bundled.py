"""What the bundle reports about itself, and in which spelling.

The interface marks a JavaScript runtime as "bundled" by comparing the path
``shutil.which`` found against the path the bundle reports. ``which`` is
reported through ``paths.norm`` - forward slashes, NFC - so the bundle has
to report the same spelling or the two never match on Windows, where
``os.path.join`` yields backslashes.
"""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import bundled  # noqa: E402
from lemonzest.download import js_runtime_status  # noqa: E402
from lemonzest.paths import norm  # noqa: E402


class BundledStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-tools-")
        self.tools = os.path.join(self.tmp, bundled.TOOLS_DIRNAME)
        os.makedirs(self.tools)
        self._dirs = bundled.tools_dirs
        bundled.tools_dirs = lambda: [self.tools]

    def tearDown(self):
        bundled.tools_dirs = self._dirs

    def fake(self, name):
        path = os.path.join(self.tools, name + (".exe" if os.name == "nt" else ""))
        with open(path, "w") as fh:
            fh.write("")
        os.chmod(path, 0o755)
        return path

    def test_reported_paths_are_normalised(self):
        self.fake("deno")
        st = bundled.status()
        self.assertEqual(st["tools"]["deno"], norm(bundled.tool("deno")))
        self.assertNotIn("\\", st["tools"]["deno"])
        self.assertNotIn("\\", st["dirs"][0])

    def test_runtime_not_from_the_bundle_is_not_marked(self):
        """A copy on PATH that is not ours keeps no bundled tag."""
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        name = "deno" + (".exe" if os.name == "nt" else "")
        with open(os.path.join(other, name), "w") as fh:
            fh.write("")
        os.chmod(os.path.join(other, name), 0o755)
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = other + os.pathsep + old
        try:
            if not shutil.which("deno"):
                self.skipTest("no executable bit on this filesystem")
            deno = [f for f in js_runtime_status({})["found"]
                    if f["name"] == "deno"]
            self.assertTrue(deno)
            self.assertFalse(deno[0]["bundled"])
        finally:
            os.environ["PATH"] = old

    def test_missing_tool_stays_none(self):
        self.assertIsNone(bundled.status()["tools"]["fpcalc"])

    def test_bundled_runtime_matches_what_which_finds(self):
        """The comparison the Download page actually makes."""
        self.fake("deno")
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tools + os.pathsep + old
        try:
            if not shutil.which("deno"):
                self.skipTest("no executable bit on this filesystem")
            found = js_runtime_status({})["found"]
            deno = [f for f in found if f["name"] == "deno"]
            self.assertTrue(deno, "deno should be found on the doctored PATH")
            self.assertTrue(deno[0]["bundled"],
                            "the bundled copy should be recognised as ours "
                            "whatever case which() spelled the extension in")
        finally:
            os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main()
