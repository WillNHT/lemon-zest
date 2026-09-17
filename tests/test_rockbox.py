"""Tags and covers a Rockbox player can show as the desktop does."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import artwork, db, scan, tags  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def jpeg(path, size, pix_fmt):
    subprocess.run([FFMPEG, "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", f"testsrc=size={size}", "-frames:v", "1",
                    "-pix_fmt", pix_fmt, path], check=True)
    with open(path, "rb") as fh:
        return fh.read()


class YearTests(unittest.TestCase):
    def test_full_dates_become_the_year(self):
        self.assertEqual(artwork.year_only("20180201"), "2018")
        self.assertEqual(artwork.year_only("2018-02-01"), "2018")
        self.assertEqual(artwork.year_only("2018"), "2018")
        self.assertEqual(artwork.year_only("unknown"), "unknown")
        self.assertIsNone(artwork.year_only(None))


class CoverTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test images")
        self.tmp = tempfile.mkdtemp(prefix="lz-art-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_letterboxed_444_cover_is_squared_and_coloured(self):
        raw = jpeg(os.path.join(self.tmp, "wide.jpg"), "1280x720", "yuvj444p")
        self.assertFalse(artwork.is_player_safe(raw))
        data, mime = artwork.normalise(raw)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(artwork.is_player_safe(data))

    def test_square_420_cover_is_already_safe(self):
        raw = jpeg(os.path.join(self.tmp, "sq.jpg"), "500x500", "yuvj420p")
        self.assertTrue(artwork.is_player_safe(raw))

    def test_not_a_jpeg_is_not_safe(self):
        self.assertFalse(artwork.is_player_safe(b"\x89PNG\r\n"))
        self.assertFalse(artwork.is_player_safe(b""))

    def test_without_ffmpeg_the_cover_is_left_alone(self):
        def broken(*a, **kw):
            raise OSError("no ffmpeg")
        self.assertEqual(artwork.normalise(b"x", runner=broken),
                         (b"x", "image/jpeg"))

    def test_fix_for_players_rewrites_an_old_download(self):
        lib = os.path.join(self.tmp, "lib")
        os.makedirs(lib)
        song = os.path.join(lib, "song.m4a")
        subprocess.run([FFMPEG, "-y", "-v", "quiet", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
                        "-c:a", "aac", song], check=True)
        from mutagen.mp4 import MP4, MP4Cover
        raw = jpeg(os.path.join(self.tmp, "wide.jpg"), "1280x720", "yuvj444p")
        audio = MP4(song)
        audio["\xa9day"] = ["20180201"]
        audio["\xa9nam"] = ["Song"]
        audio["covr"] = [MP4Cover(raw, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()

        con = db.connect(os.path.join(self.tmp, "lz.db"))
        try:
            scan.scan(con, lib)
            row = con.execute(
                "SELECT path, content_key, year FROM track").fetchone()
            self.assertEqual(sorted(tags.fix_for_players(con, row)),
                             ["cover", "year"])
            audio = MP4(song)
            self.assertEqual(audio["\xa9day"], ["2018"])
            self.assertTrue(artwork.is_player_safe(bytes(audio["covr"][0])))
            new = con.execute("SELECT content_key, year FROM track").fetchone()
            self.assertNotEqual(new["content_key"], row["content_key"])
            self.assertEqual(new["year"], "2018")
            # A second pass finds nothing left to do.
            row = con.execute(
                "SELECT path, content_key, year FROM track").fetchone()
            self.assertEqual(tags.fix_for_players(con, row), [])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
