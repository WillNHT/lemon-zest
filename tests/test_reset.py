"""The library reset: what it forgets, what it deletes, what it keeps."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, download, playlists, scan  # noqa: E402
from lemonzest.server import create_app  # noqa: E402


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-reset-")
        self.lib = os.path.join(self.tmp, "library")
        self.song = os.path.join(self.lib, "Artist", "Album", "01 Song.mp3")
        os.makedirs(os.path.dirname(self.song))
        with open(self.song, "wb") as f:
            f.write(b"\xff\xfb" + b"\0" * 4000)
        self.archive = os.path.join(self.lib, download.ARCHIVE_NAME)
        with open(self.archive, "w") as f:
            f.write("youtube abc\n")
        self.db_path = os.path.join(self.tmp, "lz.db")
        con = db.connect(self.db_path)
        scan.scan(con, self.lib)
        tid = con.execute("SELECT id FROM track").fetchone()[0]
        playlists.append_tracks(con, "mix", [tid])
        playlists.write_local(con, "mix", self.lib)
        download.remember_url(con, "https://youtu.be/abc")
        con.close()
        self.c = create_app(self.db_path).test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_without_confirmation(self):
        r = self.c.post("/api/reset", json={"delete_files": True})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.exists(self.song))
        self.assertEqual(self.c.get("/api/reset").json["tracks"], 1)

    def test_forget_only_keeps_files(self):
        r = self.c.post("/api/reset", json={"confirm": "RESET"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["tracks_forgotten"], 1)
        self.assertEqual(r.json["files_deleted"], 0)
        self.assertTrue(os.path.exists(self.song))
        self.assertFalse(os.path.exists(self.archive))
        s = self.c.get("/api/reset").json
        self.assertEqual((s["tracks"], s["playlists"], s["urls"]), (0, 0, 1))

    def test_delete_files(self):
        r = self.c.post("/api/reset",
                        json={"confirm": "RESET", "delete_files": True})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["files_deleted"], 1)
        self.assertEqual(r.json["playlist_files_deleted"], 1)
        self.assertFalse(os.path.exists(self.song))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Artist")))
        self.assertFalse(os.path.exists(self.archive))
        # The folder itself stays, registered, so downloads can land there.
        self.assertTrue(os.path.isdir(self.lib))
        s = self.c.get("/api/reset").json
        self.assertEqual(s["roots"], [db.roots(db.connect(self.db_path))[0]])
        self.assertEqual(s["urls"], 1)


if __name__ == "__main__":
    unittest.main()
