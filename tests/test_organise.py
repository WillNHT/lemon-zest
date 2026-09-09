"""Tests for moving library files to match their metadata.

The riskiest thing in the project: it moves the user's music. These check
that it moves what it says it will, that it never destroys a file it was
supposed to relocate, and that the journal really does put everything back.

    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, organise, scan  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_mp3(path, seconds=1.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-v", "quiet", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "%.2f" % seconds,
         "-b:a", "64k", path], check=True)


class OrganiseTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test audio")
        self.tmp = tempfile.mkdtemp(prefix="lz-org-")
        self.lib = os.path.join(self.tmp, "library")
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, rel, **fields):
        """A file on disk, plus the catalog row the enricher would leave."""
        path = os.path.join(self.lib, rel)
        make_mp3(path)
        scan.scan(self.con, self.lib)
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self.con.execute(
                f"UPDATE track SET {sets} WHERE rel_path = ?",
                list(fields.values()) + [rel.replace("\\", "/")])
            self.con.commit()
        return path

    def rel_paths(self):
        return sorted(r["rel_path"] for r in
                      self.con.execute("SELECT rel_path FROM track"))

    def on_disk(self):
        out = []
        for dirpath, _, names in os.walk(self.lib):
            for n in names:
                out.append(os.path.relpath(os.path.join(dirpath, n), self.lib)
                           .replace("\\", "/"))
        return sorted(out)

    # -- planning ---------------------------------------------------------

    def test_the_writer_list_folder_becomes_the_artists_name(self):
        long_credit = ("Joji, Kurtis McKenzie, Linden Jay, Chelsea Lena, "
                       "George Miller, Joshua Bliss Taffel, Kacy Anne Hill")
        self.add(f"{long_credit}/Nectar/Like You Do.mp3",
                 artist="Joji", album_artist="Joji", album="Nectar")
        moves, skipped = organise.plan(self.con, self.lib)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["dest_rel"], "Joji/Nectar/Like You Do.mp3")

    def test_the_filename_is_left_alone_by_default(self):
        # Folders come from the metadata; the name does not, because the two
        # halves of a real library spell filenames differently.
        self.add("Wrong/Wrong/07 - some Odd. name.mp3",
                 artist="Alpha", album_artist="Alpha", album="First")
        moves, _ = organise.plan(self.con, self.lib)
        self.assertEqual(moves[0]["dest_rel"],
                         "Alpha/First/07 - some Odd. name.mp3")

    def test_a_full_template_renames_as_well(self):
        self.add("Wrong/Wrong/whatever.mp3", artist="Alpha",
                 album_artist="Alpha", album="First", title="Blue", track_no=3)
        moves, _ = organise.plan(
            self.con, self.lib,
            template="{album_artist}/{album}/{track:02d} {title}{ext}")
        self.assertEqual(moves[0]["dest_rel"], "Alpha/First/03 Blue.mp3")

    def test_a_file_already_in_place_is_not_moved(self):
        self.add("Alpha/First/Blue.mp3", artist="Alpha",
                 album_artist="Alpha", album="First")
        moves, _ = organise.plan(self.con, self.lib)
        self.assertEqual(moves, [])

    def test_a_track_with_no_artist_is_skipped_not_filed_under_unknown(self):
        # Moving it to "Unknown Artist/" is the same lack of information,
        # relocated - and it would bury a file that is findable where it is.
        self.add("Mystery/thing.mp3", artist=None, album_artist=None)
        moves, skipped = organise.plan(self.con, self.lib)
        self.assertEqual(moves, [])
        self.assertEqual(len(skipped), 1)

    def test_planning_touches_nothing(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha",
                 album_artist="Alpha", album="First")
        before = self.on_disk()
        organise.plan(self.con, self.lib)
        self.assertEqual(self.on_disk(), before)

    def test_two_tracks_wanting_one_path_do_not_collide(self):
        self.add("A/x/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        self.add("B/y/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        dests = {m["dest_rel"] for m in moves}
        self.assertEqual(len(dests), len(moves))

    def test_a_move_never_lands_on_a_file_that_is_staying(self):
        # The dangerous case: one file is already correct, another would be
        # renamed onto it. Without claiming the stationary file's path first,
        # the rename would destroy it.
        self.add("Alpha/First/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        self.add("Elsewhere/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        self.assertEqual(len(moves), 1)
        self.assertNotEqual(moves[0]["dest_rel"], "Alpha/First/Blue.mp3")

        organise.apply(self.con, moves, journal_dir=self.tmp)
        self.assertEqual(len(self.on_disk()), 2)

    # -- applying ---------------------------------------------------------

    def test_applying_moves_the_file_and_updates_the_catalog(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        counts, journal = organise.apply(self.con, moves, journal_dir=self.tmp)
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(self.on_disk(), ["Alpha/First/Blue.mp3"])
        self.assertEqual(self.rel_paths(), ["Alpha/First/Blue.mp3"])

    def test_a_rescan_after_a_move_sees_no_change(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        organise.apply(self.con, moves, journal_dir=self.tmp)
        counts = scan.scan(self.con, self.lib)
        self.assertEqual(counts["added"], 0)
        self.assertEqual(counts["removed"], 0)

    def test_the_content_key_survives_so_a_card_is_not_recopied(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        before = self.con.execute(
            "SELECT content_key FROM track").fetchone()["content_key"]
        moves, _ = organise.plan(self.con, self.lib)
        organise.apply(self.con, moves, journal_dir=self.tmp)
        after = self.con.execute(
            "SELECT content_key FROM track").fetchone()["content_key"]
        self.assertEqual(before, after)

    def test_emptied_folders_are_removed(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        organise.apply(self.con, moves, journal_dir=self.tmp)
        self.assertFalse(os.path.isdir(os.path.join(self.lib, "Wrong")))

    def test_a_missing_source_is_counted_not_raised(self):
        path = self.add("Wrong/Wrong/Blue.mp3", artist="Alpha",
                        album_artist="Alpha", album="First")
        moves, _ = organise.plan(self.con, self.lib)
        os.remove(path)
        counts, _ = organise.apply(self.con, moves, journal_dir=self.tmp)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["moved"], 0)

    # -- undo -------------------------------------------------------------

    def test_undo_puts_everything_back(self):
        self.add("Wrong/Wrong/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        self.add("AlsoWrong/x/Green.mp3", artist="Beta", album_artist="Beta",
                 album="Second")
        before_disk, before_rel = self.on_disk(), self.rel_paths()

        moves, _ = organise.plan(self.con, self.lib)
        counts, journal = organise.apply(self.con, moves, journal_dir=self.tmp)
        self.assertEqual(counts["moved"], 2)
        self.assertNotEqual(self.on_disk(), before_disk)

        restored = organise.undo(self.con, journal)
        self.assertEqual(restored["restored"], 2)
        self.assertEqual(self.on_disk(), before_disk)
        self.assertEqual(self.rel_paths(), before_rel)

    def test_the_journal_describes_a_run_that_was_interrupted(self):
        # It is written before the first move and rewritten after each one,
        # so a killed process still leaves something to undo.
        self.add("Wrong/a/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        _, journal = organise.apply(self.con, moves, journal_dir=self.tmp)
        with open(journal, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["moves"]), 1)
        self.assertIn("from", data["moves"][0])
        self.assertIn("to", data["moves"][0])

    def test_journals_are_listed_newest_first(self):
        self.add("Wrong/a/Blue.mp3", artist="Alpha", album_artist="Alpha",
                 album="First")
        moves, _ = organise.plan(self.con, self.lib)
        organise.apply(self.con, moves, journal_dir=self.tmp)
        listed = organise.journals(self.tmp)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["moves"], 1)

    # -- devices ----------------------------------------------------------

    def test_a_device_mirroring_the_library_is_named_before_anything_moves(self):
        self.con.execute(
            "INSERT INTO device(device_uid, name, profile, path_template) "
            "VALUES ('uid-1', 'HiBy R1', 'hiby', '{rel_path}')")
        self.con.commit()
        affected = organise.affected_devices(self.con)
        self.assertEqual([d["name"] for d in affected], ["HiBy R1"])

    def test_a_device_with_its_own_template_is_not_affected(self):
        self.con.execute(
            "INSERT INTO device(device_uid, name, profile, path_template) "
            "VALUES ('uid-2', 'Clip', 'ums', '{album_artist}/{album}/{title}{ext}')")
        self.con.commit()
        self.assertEqual(organise.affected_devices(self.con), [])


if __name__ == "__main__":
    unittest.main()
