"""Versions of one song grouped under a master."""
import os

os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, dedupe, download, tags  # noqa: E402


class Catalog(unittest.TestCase):
    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, key, title="Song", artist=None, duration=None, **extra):
        path = "/m/%s.m4a" % key
        cols = dict(path=path, rel_path=key + ".m4a", root="/m", size=1,
                    mtime=0, content_key=key, ext=".m4a", title=title,
                    artist=artist, duration=duration, seen_at=time.time(),
                    added_at=time.time(), **extra)
        cur = self.con.execute(
            "INSERT INTO track(%s) VALUES (%s)"
            % (",".join(cols), ",".join("?" * len(cols))), list(cols.values()))
        self.con.commit()
        return cur.lastrowid


class DedupeTests(Catalog):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-dedupe-")
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))
        self.ids = {k: self.add(k) for k in ("album", "holiday", "best", "other")}

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


class SuggestTests(Catalog):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-dedupe-")
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))
        add = self.add
        self.ids = {
            "album": add("album", "Snow (Remastered)", "Band", 200.0,
                         album="Album", isrc="USX1"),
            "holiday": add("holiday", "Snow", "Band", 201.0,
                           album="Holiday", isrc="usx1"),
            "alias": add("alias", "Snow", "Pseudonym", 199.0),
            "short": add("short", "Snow", "Band", 120.0),
            "other": add("other", "Rain", "Band", 200.0),
        }

    def groups(self, **kw):
        return [sorted(t["content_key"] for t in g["tracks"])
                for g in dedupe.suggest(self.con, **kw)]

    def test_isrc_title_and_pseudonym_are_found_but_not_a_different_length(self):
        g = dedupe.suggest(self.con)
        self.assertEqual(self.groups(), [["album", "alias", "holiday"]])
        self.assertEqual(g[0]["master"], "album")
        self.assertIn("isrc", g[0]["reasons"])
        self.assertIn("title, other artist", g[0]["reasons"])

    def test_a_dismissed_pair_and_a_merged_work_stop_being_suggested(self):
        dedupe.dismiss(self.con, ["album", "alias", "holiday"])
        self.assertEqual(self.groups(), [])
        self.con.execute("DELETE FROM dup_dismissed")
        dedupe.merge(self.con, "album", ["holiday", "alias"])
        self.assertEqual(self.groups(), [])

    def test_narrowed_to_what_just_arrived(self):
        self.assertEqual(self.groups(keys=["other"]), [])
        self.assertEqual(len(self.groups(keys=["alias"])), 1)

    def test_picking_a_field_from_a_version_and_taking_it_back(self):
        dedupe.merge(self.con, "album", ["holiday"])
        dedupe.pick(self.con, "album", "holiday")
        row = self.con.execute("SELECT album FROM track WHERE content_key = "
                               "'album'").fetchone()
        self.assertEqual(row[0], "Holiday")
        v = dedupe.versions_of(self.con, "holiday")
        self.assertEqual(v["picks"], {"album": "holiday"})
        dedupe.pick(self.con, "album", "album")
        self.assertEqual(dedupe.versions_of(self.con, "album")["picks"], {})
        with self.assertRaises(dedupe.DedupeError):
            dedupe.pick(self.con, "path", "holiday")

    def test_a_video_once_fetched_is_never_fetched_again(self):
        self.con.execute(
            "INSERT INTO media(source_id, url) VALUES ('youtube:bbbbbbbbbbb', "
            "'https://www.youtube.com/watch?v=bbbbbbbbbbb')")
        self.assertIn("bbbbbbbbbbb", download._already_have(self.con, self.tmp))


if __name__ == "__main__":
    unittest.main()
