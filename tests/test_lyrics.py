"""Lyrics from LRCLIB, written into the file. No network: a fake opener."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, lyrics, scan, tags  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
SYNCED = "[00:01.28] I declare I don't care no more"


class FakeLRCLIB:
    """Answers from a dict keyed by (artist, title, duration or None)."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def __call__(self, req, timeout=None):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        key = (q["artist_name"], q["track_name"], q.get("duration"))
        self.asked.append(key)
        if key not in self.answers:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return io.BytesIO(json.dumps(self.answers[key]).encode())


class FetchTests(unittest.TestCase):
    def test_synced_lyrics_are_preferred(self):
        fake = FakeLRCLIB({("Green Day", "Burnout", "128"): {
            "plainLyrics": "plain", "syncedLyrics": SYNCED}})
        self.assertEqual(lyrics.fetch("Green Day", "Burnout", None, 128.2,
                                      opener=fake, sleep=lambda s: None),
                         SYNCED)

    def test_a_length_mismatch_is_asked_again_without_it(self):
        fake = FakeLRCLIB({("A", "T", None): {"plainLyrics": "words"}})
        self.assertEqual(lyrics.fetch("A", "T", None, 100, opener=fake,
                                      sleep=lambda s: None), "words")
        self.assertEqual(fake.asked, [("A", "T", "100"), ("A", "T", None)])

    def test_nothing_found_is_none_and_an_instrumental_has_no_words(self):
        fake = FakeLRCLIB({("A", "Inst", None): {"instrumental": True}})
        self.assertIsNone(lyrics.fetch("A", "Nope", None, None, opener=fake,
                                       sleep=lambda s: None))
        self.assertIsNone(lyrics.fetch("A", "Inst", None, None, opener=fake,
                                       sleep=lambda s: None))


class FillTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test audio")
        self.tmp = tempfile.mkdtemp(prefix="lz-lyr-")
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)
        self.song = os.path.join(self.lib, "song.m4a")
        subprocess.run([FFMPEG, "-y", "-v", "quiet", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
                        "-metadata", "title=Burnout",
                        "-metadata", "artist=Green Day",
                        "-c:a", "aac", self.song], check=True)
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))
        scan.scan(self.con, self.lib)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lyrics_go_into_the_file_and_the_catalog_follows(self):
        before = self.con.execute("SELECT content_key FROM track").fetchone()[0]
        fake = FakeLRCLIB({("Green Day", "Burnout", None): {
            "syncedLyrics": SYNCED}})
        counts = lyrics.fill(self.con, opener=fake, sleep=lambda s: None)
        self.assertEqual(counts["written"], 1)
        self.assertTrue(tags.has_lyrics(self.song))
        from mutagen.mp4 import MP4
        self.assertEqual(MP4(self.song)["\xa9lyr"], [SYNCED])
        # The file changed, so its key did, and the catalog moved with it.
        after = self.con.execute("SELECT content_key FROM track").fetchone()[0]
        self.assertNotEqual(before, after)
        # A second run finds the file already has them and asks nothing.
        fake.asked.clear()
        counts = lyrics.fill(self.con, opener=fake, sleep=lambda s: None)
        self.assertEqual((counts["had"], fake.asked), (1, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
