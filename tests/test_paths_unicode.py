"""Resolving a stored path back to the spelling the filesystem has.

The catalog stores NFC, because a path needs one spelling to be comparable.
The disk keeps whatever bytes wrote the file, and a yt-dlp download of a
Japanese title arrives as NFD - "か" plus a combining dakuten where the
catalog holds the single character "が". They read identically and open
differently, so a stored path can point at a file that is plainly there and
still fail with "no such file".

This was not hypothetical: it is why writing tags to
``KIRINJI - 時間がない (Jikanga Nai).m4a`` did nothing at all.
"""
import os

# Automatic enrichment follows every scan and download. Switched off for the
# suite: these tests are about the catalog and the sync, and none of them
# should be making rate-limited calls to somebody else's service.
os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import sys
import tempfile
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest.paths import norm, resolve_existing  # noqa: E402

# The real filename from the library that exposed this.
NFC_NAME = unicodedata.normalize("NFC", "KIRINJI - 時間がない (Jikanga Nai).m4a")
NFD_NAME = unicodedata.normalize("NFD", NFC_NAME)


class UnicodePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-nfc-")

    def write(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")
        return path

    def test_the_two_spellings_really_do_differ(self):
        """Guards the premise: on a filesystem that folds them, skip."""
        self.assertNotEqual(NFC_NAME, NFD_NAME)
        self.write("d", NFD_NAME)
        if os.path.exists(os.path.join(self.tmp, "d", NFC_NAME)):
            self.skipTest("this filesystem normalises names for us")

    def test_an_nfd_file_is_found_from_its_nfc_path(self):
        self.write("d", NFD_NAME)
        stored = norm(os.path.join(self.tmp, "d", NFC_NAME))
        if os.path.exists(stored):
            self.skipTest("this filesystem normalises names for us")
        found = resolve_existing(stored)
        self.assertIsNotNone(found, "the NFC path did not resolve to the file")
        self.assertTrue(os.path.isfile(found))

    def test_an_nfc_file_is_found_from_an_nfd_path(self):
        self.write("d", NFC_NAME)
        stored = os.path.join(self.tmp, "d", NFD_NAME)
        if os.path.exists(stored):
            self.skipTest("this filesystem normalises names for us")
        self.assertIsNotNone(resolve_existing(stored))

    def test_a_folder_and_a_file_in_different_normalisations_resolve(self):
        """The case whole-path NFC and whole-path NFD both miss.

        A folder made by hand (NFC) holding a file written by yt-dlp (NFD).
        Normalising the whole path either way fixes one component and breaks
        the other, so only a component-wise walk finds it.
        """
        folder = unicodedata.normalize("NFC", "ニュース")
        self.write(folder, NFD_NAME)
        stored = norm(os.path.join(self.tmp, folder, NFC_NAME))
        if os.path.exists(stored):
            self.skipTest("this filesystem normalises names for us")
        found = resolve_existing(stored)
        self.assertIsNotNone(found)
        self.assertTrue(os.path.isfile(found))

    def test_a_path_that_is_simply_absent_still_resolves_to_nothing(self):
        # The walk must not turn "missing" into a false positive by matching
        # some other file in the folder.
        self.write("d", "something else.m4a")
        gone = norm(os.path.join(self.tmp, "d", NFC_NAME))
        self.assertIsNone(resolve_existing(gone))

    def test_an_ordinary_path_costs_no_walking(self):
        p = self.write("d", "plain.m4a")
        self.assertEqual(resolve_existing(p), p)

    def test_a_missing_folder_is_not_an_error(self):
        self.assertIsNone(resolve_existing(
            os.path.join(self.tmp, "nope", "nothing.m4a")))
        self.assertIsNone(resolve_existing(""))
        self.assertIsNone(resolve_existing(None))


if __name__ == "__main__":
    unittest.main()
