"""Versions of one song grouped under a master."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, dedupe, tags  # noqa: E402


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-dedupe-")
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))
        self.ids = {k: self.add(k) for k in ("album", "holiday", "best", "other")}

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, key):
        path = "/m/%s.m4a" % key
        cur = self.con.execute(
            "INSERT INTO track(path, rel_path, root, size, mtime, content_key,"
            " ext, title, seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (path, key + ".m4a", "/m", 1, 0, key, ".m4a", "Song", time.time()))
        self.con.commit()
        return cur.lastrowid

    def canon(self, key):
        return dedupe.canon_ids(self.con, [self.ids[key]])[0]

    def test_variants_resolve_to_the_master_and_stay_in_the_library(self):
        dedupe.merge(self.con, "album", ["holiday", "best"])
        for k in ("album", "holiday", "best"):
            self.assertEqual(self.canon(k), self.ids["album"])
        self.assertEqual(self.canon("other"), self.ids["other"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM track").fetchone()[0], 4)
        ids = [self.ids[k] for k in ("holiday", "other", "album", "best")]
        self.assertEqual(dedupe.canon_ids(self.con, ids),
                         [self.ids["album"], self.ids["other"]])

    def test_merging_into_a_work_joins_the_works(self):
        dedupe.merge(self.con, "holiday", ["best"])
        work = dedupe.merge(self.con, "album", ["holiday"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM work").fetchone()[0], 1)
        got = dedupe.members(self.con, work)
        self.assertEqual([m["content_key"] for m in got if m["is_master"]],
                         ["album"])
        self.assertEqual(len(got), 3)

    def test_set_master_and_unmerge(self):
        work = dedupe.merge(self.con, "album", ["holiday", "best"])
        dedupe.set_master(self.con, "best")
        self.assertEqual(self.canon("album"), self.ids["best"])
        dedupe.unmerge(self.con, "best")         # the master leaves
        self.assertEqual(self.canon("best"), self.ids["best"])
        masters = [m for m in dedupe.members(self.con, work) if m["is_master"]]
        self.assertEqual(len(masters), 1)
        dedupe.unmerge(self.con, "holiday")      # one left: no work
        self.assertIsNone(dedupe.work_of(self.con, "album"))
        self.assertEqual(self.canon("album"), self.ids["album"])

    def test_a_tag_write_keeps_the_file_in_its_work(self):
        dedupe.merge(self.con, "album", ["holiday"])
        tags.rekey(self.con, "holiday", "/m/holiday.m4a", "holiday2")
        self.assertEqual(self.canon("holiday"), self.ids["album"])

    def test_merging_nothing_or_an_unknown_file_is_refused(self):
        with self.assertRaises(dedupe.DedupeError):
            dedupe.merge(self.con, "album", ["album"])
        with self.assertRaises(dedupe.DedupeError):
            dedupe.merge(self.con, "album", ["nope"])


if __name__ == "__main__":
    unittest.main()
