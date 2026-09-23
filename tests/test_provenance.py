"""Where a downloaded file came from, and what it was when it arrived."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, download, enrich, meta  # noqa: E402

VID = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-prov-")
        self.root = download.norm(os.path.join(self.tmp, "Music"))
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, rel, title, artist, purl=VID, key="k1"):
        self.con.execute(
            "INSERT INTO track(path, rel_path, root, size, mtime, content_key,"
            " ext, title, artist, purl, source_id, seen_at, added_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.root + "/" + rel, rel, self.root, 100, 0, key, ".m4a",
             title, artist, purl, meta.source_id(purl), time.time(),
             time.time()))
        self.con.commit()

    def test_a_video_is_known_by_its_id_whatever_the_url_looks_like(self):
        for url in (VID, "https://music.youtube.com/watch?v=aaaaaaaaaaa&list=X",
                    "https://youtu.be/aaaaaaaaaaa",
                    "https://www.youtube.com/watch?feature=x&v=aaaaaaaaaaa"):
            self.assertEqual(meta.source_id(url), "youtube:aaaaaaaaaaa", url)
        self.assertIsNone(meta.source_id("https://example.test/x"))
        self.assertIsNone(meta.source_id(None))

    def test_the_file_as_it_arrived_outlives_every_change_to_it(self):
        self.add("Channel/Singles/Video Title.m4a", "Video Title", "Channel")
        download.record_arrival(self.con, self.root,
                                self.root + "/Channel/Singles/Video Title.m4a",
                                "https://www.youtube.com/playlist?list=PL1")
        # Identified, re-tagged and moved by organise since.
        self.con.execute("UPDATE track SET title = 'Real Title', "
                         "artist = 'Real Artist', rel_path = 'Real/x.m4a', "
                         "path = ? ", (self.root + "/Real/x.m4a",))
        self.con.commit()
        out = download.provenance(self.con, "k1")
        self.assertEqual(out["source_id"], "youtube:aaaaaaaaaaa")
        self.assertEqual(out["initial_tags"]["title"], "Video Title")
        self.assertEqual(out["initial_tags"]["artist"], "Channel")
        self.assertTrue(out["initial_path"].endswith(
            "Channel/Singles/Video Title.m4a"))
        self.assertFalse(out["backfilled"])

    def test_a_second_arrival_does_not_rewrite_the_first(self):
        self.add("a.m4a", "First", "A")
        download.record_arrival(self.con, self.root, self.root + "/a.m4a")
        self.con.execute("UPDATE track SET title = 'Second'")
        download.record_arrival(self.con, self.root, self.root + "/a.m4a")
        self.assertEqual(download.provenance(
            self.con, "k1")["initial_tags"]["title"], "First")

    def test_one_video_in_two_playlists_has_two_sources(self):
        self.add("a.m4a", "Song", "A")
        for url in ("https://youtube.com/playlist?list=PL1",
                    "https://youtube.com/playlist?list=PL2",
                    "https://youtube.com/playlist?list=PL1"):
            download.note_source(self.con, "youtube:aaaaaaaaaaa", url)
        self.con.commit()
        self.assertEqual(download.provenance(self.con, "k1")["sources"],
                         ["https://youtube.com/playlist?list=PL1",
                          "https://youtube.com/playlist?list=PL2"])

    def test_a_renamed_retagged_file_is_not_downloaded_again(self):
        self.add("Renamed/Retagged.m4a", "Anything", "Anyone")
        have = download._already_have(self.con, self.root)
        self.assertIn("aaaaaaaaaaa", have)

    def test_earlier_downloads_get_a_record_from_what_the_catalog_has(self):
        self.add("old.m4a", "Old", "A")
        self.con.execute("DELETE FROM meta WHERE key = 'media_backfilled'")
        self.con.commit()
        self.con.close()
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))
        out = download.provenance(self.con, "k1")
        self.assertTrue(out["backfilled"])
        self.assertTrue(out["initial_path"].endswith("old.m4a"))

    def test_the_source_id_is_filled_for_a_catalog_from_before(self):
        # A catalog from before the column existed.
        self.con.execute("DROP INDEX ix_track_source")
        self.con.execute("ALTER TABLE track DROP COLUMN source_id")
        self.con.execute(
            "INSERT INTO track(path, rel_path, root, size, mtime, content_key,"
            " ext, purl, seen_at) VALUES ('p', 'p', 'r', 1, 0, 'k', '.m4a', ?, 0)",
            (VID,))
        self.con.commit()
        db.migrate(self.con)
        self.assertEqual(self.con.execute(
            "SELECT source_id FROM track").fetchone()[0], "youtube:aaaaaaaaaaa")


if __name__ == "__main__":
    unittest.main(verbosity=2)
