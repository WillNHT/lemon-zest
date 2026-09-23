"""Taking a library to another computer, or another folder."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import json
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, download, playlists, portable, scan  # noqa: E402
from lemonzest.paths import norm  # noqa: E402


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-port-")
        # Machine A keeps its library at one place...
        self.a = os.path.join(self.tmp, "Bob", "Music", "library")
        song = os.path.join(self.a, "Artist", "Album", "01 Song.m4a")
        os.makedirs(os.path.dirname(song))
        with open(song, "wb") as fh:
            fh.write(b"\0" * 4096)
        self.con = db.connect(os.path.join(self.tmp, "a.db"))
        scan.scan(self.con, self.a)
        tid = self.con.execute("SELECT id FROM track").fetchone()[0]
        playlists.append_tracks(self.con, "mix", [tid])
        path = self.con.execute("SELECT path FROM track").fetchone()[0]
        self.con.execute("INSERT INTO sync_list(kind, ref, added_at) "
                         "VALUES ('track', ?, 0)", (path,))
        download.remember_url(self.con, "https://youtu.be/abc",
                              root=norm(self.a))
        download.set_config(self.con, root=norm(self.a),
                            cookies_file="C:/Bob/cookies.txt")
        download.save_paused(self.con, {
            "id": "p1", "label": "list", "urls": ["https://x"],
            "remaining": ["https://x"], "root": norm(self.a),
            "state": "paused", "at": time.time()})
        self.con.execute("INSERT INTO meta(key, value) VALUES "
                         "('enrich.acoustid_key', 'bobs-key')")
        self.con.commit()
        # ...machine B at another, under another user.
        self.b = os.path.join(self.tmp, "Remote", "Music", "library", "master")

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_to_b(self):
        portable.pack(self.con, self.a)
        shutil.copytree(self.a, self.b)
        other = db.connect(os.path.join(self.tmp, "b.db"))
        other.execute("INSERT INTO meta(key, value) VALUES "
                      "('enrich.acoustid_key', 'remotes-key')")
        other.commit()
        return other

    def test_the_folder_is_the_thing_to_copy(self):
        con = self.copy_to_b()
        try:
            out = portable.unpack(con, self.b)
            b = norm(os.path.abspath(self.b))
            self.assertEqual((out["from"], out["to"]),
                             (norm(os.path.abspath(self.a)), b))
            self.assertEqual(out["tracks"], 1)
            self.assertEqual(out["missing_roots"], [])
            row = con.execute("SELECT path, root FROM track").fetchone()
            self.assertEqual(row["root"], b)
            self.assertTrue(os.path.isfile(row["path"]), row["path"])
            # The playlist still holds the track, and the rules follow it.
            self.assertIsNotNone(con.execute(
                "SELECT track_id FROM playlist_entry").fetchone()[0])
            self.assertEqual(con.execute(
                "SELECT ref FROM sync_list").fetchone()[0], row["path"])
            self.assertEqual(download.get_config(con)["root"], b)
            self.assertEqual(con.execute(
                "SELECT root FROM download_url").fetchone()[0], b)
            self.assertEqual(download.paused(con)[0]["root"], b)
            # This machine's keys are its own; Bob's never left his.
            self.assertEqual(db.meta_get(con, "enrich.acoustid_key"),
                             "remotes-key")
            self.assertEqual(download.get_config(con)["cookies_file"], "")
        finally:
            con.close()

    def test_the_copy_carries_no_credentials(self):
        path = portable.pack(self.con, self.a)
        import sqlite3
        packed = sqlite3.connect(path)
        keys = {r[0] for r in packed.execute("SELECT key FROM meta")}
        packed.close()
        self.assertFalse(keys & set(portable.PRIVATE))
        self.assertIn("portable.root", keys)

    def test_a_catalog_with_tracks_is_not_replaced_unasked(self):
        con = self.copy_to_b()
        try:
            scan.scan(con, self.b)
            with self.assertRaises(ValueError):
                portable.unpack(con, self.b)
            self.assertEqual(portable.unpack(con, self.b, replace=True)
                             ["tracks"], 1)
        finally:
            con.close()

    def test_a_folder_moved_on_this_machine(self):
        shutil.move(self.a, self.b)
        self.assertFalse(db.roots_detail(self.con)[0]["exists"])
        portable.relocate(self.con, self.a, self.b)
        detail = db.roots_detail(self.con)
        self.assertEqual([r["root"] for r in detail],
                         [norm(os.path.abspath(self.b))])
        self.assertTrue(detail[0]["exists"])
        # A rescan of the new place finds nothing new: same rows, same ids.
        counts = scan.scan(self.con, self.b)
        self.assertEqual((counts["added"], counts["removed"]), (0, 0))

    def test_rows_scanned_at_the_new_place_first_give_way_to_the_old(self):
        shutil.copytree(self.a, self.b)
        scan.scan(self.con, self.b)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM track").fetchone()[0], 2)
        portable.relocate(self.con, self.a, self.b)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM track").fetchone()[0], 1)
        # The one that stayed is the one the playlist points at.
        self.assertIsNotNone(self.con.execute(
            "SELECT track_id FROM playlist_entry").fetchone()[0])


class ApiTests(PortableTests):
    def test_the_interface_moves_a_folder_and_opens_a_copy(self):
        from lemonzest.server import create_app
        self.con.close()
        shutil.move(self.a, self.b)
        c = create_app(os.path.join(self.tmp, "a.db")).test_client()
        roots = c.get("/api/stats").get_json()["root_detail"]
        self.assertFalse(roots[0]["exists"])
        out = c.post("/api/roots/relocate",
                     json={"old": roots[0]["root"], "new": self.b}).get_json()
        self.assertTrue(out["roots"][0]["exists"])
        packed = c.post("/api/portable/pack").get_json()["roots"]
        self.assertTrue(packed[0]["packed_at"])
        self.assertEqual(c.post("/api/portable/unpack",
                                json={"folder": self.b}).status_code, 400,
                         "a catalog with tracks is not replaced unasked")
        self.con = db.connect(os.path.join(self.tmp, "a.db"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
