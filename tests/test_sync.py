"""Invariant tests for the sync engine.

These run against synthetic files in a temporary directory, so they are safe
to run anywhere and do not need a real card or the user's library.

    python -m unittest discover -s tests -v
"""
import os

# Automatic enrichment follows every scan and download. Switched off for the
# suite: these tests are about the catalog and the sync, and none of them
# should be making rate-limited calls to somebody else's service.
os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import io
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, devices, executor, planner, playlists, scan  # noqa: E402
from lemonzest.paths import dedupe, safe_component  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_mp3(path, frames=40, artist=None, album=None, title=None, track=None):
    """Write a real, decodable MP3 of roughly ``frames`` tenths of a second.

    Generated with ffmpeg rather than hand-assembled, so mutagen parses it
    the same way it parses the user's actual library. ``frames`` only varies
    the duration, which is how the tests produce a changed source file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-v", "quiet",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "%.2f" % (frames / 40.0), "-b:a", "64k", path],
        check=True,
    )
    if artist or album or title:
        from mutagen.easyid3 import EasyID3
        from mutagen.mp3 import MP3
        audio = MP3(path)
        try:
            audio.add_tags()
        except Exception:
            pass
        tags = EasyID3()
        if title:
            tags["title"] = title
        if artist:
            tags["artist"] = artist
            tags["albumartist"] = artist
        if album:
            tags["album"] = album
        if track:
            tags["tracknumber"] = str(track)
        tags.save(path)


class SyncTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test audio")
        self.tmp = tempfile.mkdtemp(prefix="lz-test-")
        self.lib = os.path.join(self.tmp, "library")
        self.card = os.path.join(self.tmp, "card")
        os.makedirs(self.card)
        self.files = []
        for artist, album, titles in [
            ("Alpha", "First", ["One", "Two", "Three"]),
            ("Beta", "Second", ["Four", "Five"]),
        ]:
            for i, title in enumerate(titles, 1):
                p = os.path.join(self.lib, artist, album, f"{i:02d} {title}.mp3")
                make_mp3(p, frames=40 + i, artist=artist, album=album,
                         title=title, track=i)
                self.files.append(p)

        self.con = db.connect(os.path.join(self.tmp, "lemon-zest.db"))
        scan.scan(self.con, self.lib)

        # A playlist over three of the five tracks.
        pl_dir = os.path.join(self.tmp, "playlists")
        os.makedirs(pl_dir)
        rows = [
            (os.path.relpath(self.files[0], pl_dir).replace("\\", "/"), "Alpha - One", 12, "test:1"),
            (os.path.relpath(self.files[1], pl_dir).replace("\\", "/"), "Alpha - Two", 13, "test:2"),
            (os.path.relpath(self.files[3], pl_dir).replace("\\", "/"), "Beta - Four", 14, None),
        ]
        playlists.write(os.path.join(pl_dir, "mix.m3u8"), "mix", rows,
                        source_uri="https://example.test/mix")
        playlists.import_dir(self.con, pl_dir)

        self.device = devices.register(self.con, self.card, name="Card",
                                       profile="ums")
        self.con.execute(
            "INSERT INTO device_set(device_id, kind, ref, added_at) "
            "VALUES (?,?,?,0)", (self.device["id"], "playlist", "mix"))
        self.con.commit()
        self.device = devices.find(self.con, "Card")

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, **kw):
        return planner.plan(self.con, self.device, self.card, **kw)

    def _sync(self, **kw):
        return executor.execute(self.con, self._plan(**kw), **kw)

    # -- the invariants ------------------------------------------------

    def test_playlist_imported_with_uris(self):
        n = self.con.execute("SELECT COUNT(*) FROM playlist_entry").fetchone()[0]
        matched = self.con.execute(
            "SELECT COUNT(*) FROM playlist_entry WHERE track_id IS NOT NULL"
        ).fetchone()[0]
        uris = self.con.execute(
            "SELECT COUNT(*) FROM playlist_entry WHERE source_uri IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual((n, matched, uris), (3, 3, 2))

    def test_first_sync_copies_everything(self):
        summary = self._sync()
        self.assertEqual(summary["copied"], 3)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["playlists"], 1)
        for rel in ("Alpha/First/01 One.mp3", "Beta/Second/01 Four.mp3"):
            self.assertTrue(os.path.exists(os.path.join(self.card, "Music", rel)), rel)

    def test_resync_is_a_noop(self):
        """AC2: replugging a correct card must copy nothing."""
        self._sync()
        p = self._plan()
        self.assertEqual(p["copies"], [])
        self.assertEqual(p["deletes"], [])
        self.assertEqual(len(p["unchanged"]), 3)

    def test_a_playlist_cover_goes_beside_the_playlist(self):
        # What save_cover leaves beside the library: Playlists/<name>.jpg.
        jpg = subprocess.run(
            [FFMPEG, "-v", "quiet", "-f", "lavfi", "-i", "testsrc=size=64x64",
             "-frames:v", "1", "-pix_fmt", "yuvj420p", "-f", "mjpeg", "pipe:1"],
            capture_output=True, check=True).stdout
        saved = playlists.save_cover(
            "mix", self.lib, "https://example.test/cover.jpg",
            opener=lambda req, timeout=None: io.BytesIO(jpg))
        self.assertEqual(os.path.normcase(os.path.dirname(saved)),
                         os.path.normcase(playlists.local_dir(self.lib)
                                          .replace("\\", "/")))
        self.assertEqual(playlists.find_cover(self.con, "mix"),
                         playlists.cover_path(self.lib, "mix"))
        self._sync()
        cover = os.path.join(self.card, "Music", "mix.jpg")
        self.assertTrue(os.path.isfile(cover))
        # Unticking the playlist takes its cover off the card with it.
        self.con.execute("DELETE FROM device_set")
        self.con.commit()
        self._sync()
        self.assertFalse(os.path.exists(cover))

    def test_rockbox_playlists_use_absolute_music_paths(self):
        # Rockbox wants playlists in their own folder and entries written
        # from the card's root, not relative to the playlist.
        self.con.execute("UPDATE device SET profile='rockbox', "
                         "playlist_dir='Playlists' WHERE id=?",
                         (self.device["id"],))
        self.con.commit()
        self.device = devices.find(self.con, "Card")
        summary = self._sync()
        self.assertEqual(summary["errors"], [])
        written = os.path.join(self.card, "Playlists", "mix.m3u8")
        self.assertTrue(os.path.isfile(written))
        with open(written, encoding="utf-8") as fh:
            paths = [ln for ln in fh.read().splitlines()
                     if ln and not ln.startswith("#")]
        self.assertEqual(len(paths), 3)
        for p in paths:
            self.assertTrue(p.startswith("/Music/"), p)
            self.assertNotIn("..", p)
            self.assertTrue(os.path.isfile(
                os.path.join(self.card, p.lstrip("/"))), p)

    def test_playlist_is_rewritten_not_appended(self):
        """AC3: the bug found on the real card cannot recur."""
        for _ in range(5):
            self._sync()
        path = os.path.join(self.card, "Music", "mix.m3u8")
        with open(path, "rb") as fh:
            body = fh.read().decode("utf-8")
        entries = [ln for ln in body.splitlines()
                   if ln and not ln.startswith("#")]
        self.assertEqual(len(entries), 3)
        self.assertEqual(len(entries), len(set(entries)))
        self.assertIn("#Collection URI: https://example.test/mix", body)
        self.assertIn("#Source URI: test:1", body)
        self.assertTrue(body.endswith("\r\n"))

    def test_written_playlist_reads_back_clean(self):
        self._sync()
        pl = playlists.read(os.path.join(self.card, "Music", "mix.m3u8"))
        self.assertEqual(len(pl["entries"]), 3)
        self.assertTrue(all(e["abs_path"] for e in pl["entries"]),
                        "every written entry must resolve on the card")

    def test_source_change_triggers_recopy(self):
        self._sync()
        make_mp3(self.files[0], frames=90, artist="Alpha", album="First",
                 title="One", track=1)  # re-encode the source
        scan.scan(self.con, self.lib)
        p = self._plan()
        self.assertEqual(len(p["copies"]), 1)
        self.assertEqual(p["copies"][0]["reason"], "source changed")

    def test_file_deleted_from_card_is_restored(self):
        self._sync()
        victim = os.path.join(self.card, "Music", "Alpha", "First", "01 One.mp3")
        os.remove(victim)
        p = self._plan()
        self.assertEqual(len(p["copies"]), 1)
        self.assertEqual(p["copies"][0]["reason"], "gone from card")
        self._sync()
        self.assertTrue(os.path.exists(victim))

    def test_untick_removes_files_and_playlist(self):
        """AC5: unticking removes exactly those files, and its playlist file."""
        self._sync()
        self.con.execute("DELETE FROM device_set WHERE device_id=?",
                         (self.device["id"],))
        self.con.commit()
        p = self._plan()
        self.assertEqual(len(p["deletes"]), 3)
        self.assertEqual(len(p["playlist_deletes"]), 1)
        self._sync()
        self.assertFalse(os.path.exists(os.path.join(self.card, "Music", "mix.m3u8")))
        left = [f for _, _, fs in os.walk(os.path.join(self.card, "Music"))
                for f in fs]
        self.assertEqual(left, [])

    def test_interrupted_copy_leaves_no_partial(self):
        """AC4: an interrupted sync leaves nothing half-written, and resumes."""
        real_copy = executor._copy_one
        calls = {"n": 0}

        def flaky(src, dst, expect_size=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise executor.Aborted("simulated unplug")
            return real_copy(src, dst, expect_size=expect_size)

        executor._copy_one = flaky
        try:
            with self.assertRaises(executor.Aborted):
                self._sync()
        finally:
            executor._copy_one = real_copy

        stray = [f for _, _, fs in os.walk(self.card) for f in fs
                 if f.endswith(".lz-tmp")]
        self.assertEqual(stray, [], "a partial file was left on the card")

        # Resume: the one file that landed is kept, the rest are copied.
        p = self._plan()
        self.assertEqual(len(p["unchanged"]), 1)
        self.assertEqual(len(p["copies"]), 2)
        summary = self._sync()
        self.assertEqual(summary["copied"], 2)
        self.assertEqual(summary["failed"], 0)

    def test_adopt_recognises_files_already_on_the_card(self):
        """A card filled by another tool must not be recopied wholesale."""
        self._sync()
        # Forget everything: the files stay, the manifest goes.
        self.con.execute("DELETE FROM device_manifest WHERE device_id=?",
                         (self.device["id"],))
        self.con.commit()
        self.assertEqual(len(self._plan()["copies"]), 3)

        res = planner.adopt(self.con, self.device, self.card, verify="content")
        self.assertEqual(res["adopted"], 3)
        self.assertEqual(res["mismatched"], 0)

        p = self._plan()
        self.assertEqual(p["copies"], [])
        self.assertEqual(len(p["unchanged"]), 3)

    def test_adopt_rejects_a_file_that_differs(self):
        self._sync()
        self.con.execute("DELETE FROM device_manifest WHERE device_id=?",
                         (self.device["id"],))
        self.con.commit()
        victim = os.path.join(self.card, "Music", "Alpha", "First", "01 One.mp3")
        with open(victim, "ab") as fh:
            fh.write(bytes(4096))   # same name, different bytes
        res = planner.adopt(self.con, self.device, self.card, verify="content")
        self.assertEqual(res["adopted"], 2)
        self.assertEqual(res["mismatched"], 1)
        self.assertEqual(len(self._plan()["copies"]), 1)

    def test_space_guard_refuses_before_writing(self):
        """AC6: a set that will not fit is refused at plan time."""
        p = self._plan()
        tiny = planner.check_space(p, free_bytes=1024)
        self.assertFalse(tiny["fits"])
        self.assertGreater(tiny["shortfall"], 0)
        roomy = planner.check_space(p, free_bytes=10 * 2**30)
        self.assertTrue(roomy["fits"])
        # Nothing was written by planning.
        self.assertFalse(os.path.exists(os.path.join(self.card, "Music")))

    def test_writes_stay_inside_the_device(self):
        self._sync()
        p = self._plan()
        p["copies"] = [{"rel": "../../escape.mp3", "track": p["unchanged"] and
                        self.con.execute("SELECT * FROM track LIMIT 1").fetchone(),
                        "src": self.files[0], "size": os.path.getsize(self.files[0]),
                        "reason": "test"}]
        summary = executor.execute(self.con, p)
        self.assertEqual(summary["copied"], 0)
        self.assertTrue(any("outside device" in e for e in summary["errors"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.mp3")))

    # -- the device's own playlist naming -------------------------------

    def _set_template(self, template):
        self.con.execute("UPDATE device SET playlist_template=? WHERE id=?",
                         (template, self.device["id"]))
        self.con.commit()
        self.device = devices.find(self.con, "Card")

    def _card_playlists(self):
        return playlists.list_playlist_files(os.path.join(self.card, "Music"))

    def test_sync_replaces_the_players_own_playlist_file(self):
        """The whole point: one playlist in, one playlist file out.

        The card arrives with a playlist the player itself wrote, under the
        player's own naming. Syncing must land on that file, not beside it.
        """
        self._set_template("{name}-Owner.m3u8")
        os.makedirs(os.path.join(self.card, "Music"), exist_ok=True)
        stale = os.path.join(self.card, "Music", "mix-Owner.m3u8")
        with open(stale, "w", encoding="utf-8", newline="") as fh:
            fh.write("#EXTM3U\r\n" + "Alpha/First/01 One.mp3\r\n" * 40)

        p = self._plan()
        self.assertEqual([x["filename"] for x in p["playlists"]],
                         ["mix-Owner.m3u8"])
        self.assertEqual(p["playlists"][0]["replaces"], "mix-Owner.m3u8")
        self.assertEqual(p["playlist_strays"], [])

        self._sync()
        self.assertEqual(self._card_playlists(), ["mix-Owner.m3u8"])
        parsed = playlists.read(stale)
        self.assertEqual(len(parsed["entries"]), 3)

    def test_plan_reports_the_playlist_it_would_leave_behind(self):
        """The defect the first dry run could not see.

        With the default naming against a card that spells playlists its own
        way, a sync adds a second file rather than replacing the first. The
        plan has to say so - reporting only what it will write is what made
        this invisible.
        """
        os.makedirs(os.path.join(self.card, "Music"), exist_ok=True)
        theirs = os.path.join(self.card, "Music", "mix-Owner.m3u8")
        with open(theirs, "w", encoding="utf-8", newline="") as fh:
            fh.write("#EXTM3U\r\n")

        p = self._plan()
        self.assertEqual(p["playlists"][0]["filename"], "mix.m3u8")
        self.assertIsNone(p["playlists"][0]["replaces"])
        self.assertEqual([(x["filename"], x["shadows"])
                          for x in p["playlist_strays"]],
                         [("mix-Owner.m3u8", "mix")])

        self._sync()
        self.assertEqual(self._card_playlists(),
                         ["mix-Owner.m3u8", "mix.m3u8"])

    def test_sync_replaces_a_differently_normalised_filename(self):
        """The same name in NFD and NFC is one file to a reader, two to disk.

        The card's playlists were written by the player, whose spelling of
        a Vietnamese name need not match the catalog's. Everything that
        compares normalises, so the plan correctly reports a replacement -
        and then the writer opens the other byte sequence and leaves the
        original sitting beside a clean copy. Caught on the real card,
        where a sync turned 23 playlists into 46.
        """
        import unicodedata
        owner = "Ti\u1ebfn"                      # precomposed
        self._set_template("{name}-" + owner + ".m3u8")
        music = os.path.join(self.card, "Music")
        os.makedirs(music, exist_ok=True)
        nfd = unicodedata.normalize("NFD", "mix-" + owner + ".m3u8")
        theirs = os.path.join(music, nfd)
        with open(theirs, "w", encoding="utf-8", newline="") as fh:
            fh.write("#EXTM3U\r\n" + "Alpha/First/01 One.mp3\r\n" * 40)
        # Precondition: the two spellings really are different filenames.
        self.assertFalse(os.path.exists(
            os.path.join(music, unicodedata.normalize("NFC", nfd))))

        p = self._plan()
        self.assertEqual(len(p["playlists"]), 1)
        self.assertIsNotNone(p["playlists"][0]["replaces"])
        self.assertEqual(p["playlist_strays"], [])

        self._sync()
        self.assertEqual(len(self._card_playlists()), 1)
        self.assertEqual(len(playlists.read(theirs)["entries"]), 3)

    def test_detecting_the_template_from_the_card(self):
        files = ["chill-Owner.m3u8", "mix-Owner.m3u8", "unrelated.m3u8"]
        template, matched, total = playlists.infer_template(
            files, ["mix", "chill", "absent"])
        self.assertEqual((template, matched, total), ("{name}-Owner.m3u8", 2, 3))

    def test_detection_prefers_the_longer_playlist_name(self):
        """"chill" must not claim a file that belongs to "chill winter"."""
        template, matched, _ = playlists.infer_template(
            ["chill winter-Owner.m3u8"], ["chill", "chill winter"])
        self.assertEqual((template, matched), ("{name}-Owner.m3u8", 1))

    def test_detection_declines_to_guess(self):
        self.assertEqual(playlists.infer_template(["something.m3u8"], ["mix"]),
                         (None, 0, 1))

    def test_marker_identifies_the_device(self):
        self.assertTrue(os.path.exists(os.path.join(self.card, ".lemon-zest-id")))
        self.assertEqual(devices.read_marker(self.card), self.device["device_uid"])
        found = devices.locate(self.con, self.device)
        self.assertEqual(os.path.abspath(found), os.path.abspath(self.card))


class PathTests(unittest.TestCase):
    def test_fat_illegal_characters(self):
        # Substituted with their full-width twins rather than underscored:
        # those are ordinary letters to every filesystem, they read as what
        # the title said, and they are what yt-dlp writes - so a downloaded
        # file and a file named from its tags agree.
        self.assertEqual(safe_component('AC/DC: Back?'), "AC⧸DC： Back？")
        # Whatever the substitution, the point is that none of it is legal
        # in a FAT name and none of it splits a component.
        for ch in '<>:"/\|?*':
            self.assertNotIn(ch, safe_component(f"a{ch}b"))
        self.assertEqual(safe_component("trailing. "), "trailing")
        self.assertEqual(safe_component("CON.mp3"), "CON_.mp3")

    def test_case_insensitive_collisions(self):
        taken = set()
        self.assertEqual(dedupe("a/b.mp3", taken), "a/b.mp3")
        self.assertEqual(dedupe("A/B.mp3", taken), "A/B (2).mp3")

    def test_a_slash_in_a_tag_does_not_create_a_folder(self):
        """An artist called AC/DC lands in one folder, never two."""
        from lemonzest.paths import render_template
        track = {"artist": "AC/DC", "album_artist": "AC/DC",
                 "album": "Back In Black", "title": "Shoot to Thrill",
                 "track_no": 2, "disc_no": 1, "year": "1980", "genre": "Rock",
                 "path": "/lib/AC⧸DC/Back In Black/02 Shoot to Thrill.m4a",
                 "rel_path": "AC⧸DC/Back In Black/02 Shoot to Thrill.m4a"}
        rel = render_template(
            "{album_artist}/{album}/{track:02d} {title}{ext}", track)
        self.assertEqual(rel,
                         "AC⧸DC/Back In Black/02 Shoot to Thrill.m4a")
        # The invariant this test exists for: three components, not four.
        self.assertEqual(len(rel.split("/")), 3)
        self.assertEqual(rel.count("/"), 2)

    def test_rel_path_template_mirrors_the_library(self):
        from lemonzest.paths import render_template
        track = {"rel_path": "Some Artist/An Album/03 Track.mp3",
                 "path": "/lib/Some Artist/An Album/03 Track.mp3"}
        self.assertEqual(render_template("{rel_path}", track),
                         "Some Artist/An Album/03 Track.mp3")

    def test_playlist_filename_follows_the_device_template(self):
        from lemonzest.playlists import filename_for
        self.assertEqual(filename_for("mix"), "mix.m3u8")
        self.assertEqual(filename_for("mix", "{name}-Owner.m3u8"),
                         "mix-Owner.m3u8")
        # A template without an extension still produces a playlist file.
        self.assertEqual(filename_for("mix", "{name}"), "mix.m3u8")
        # A slash in a playlist name is a character, never a path separator.
        self.assertEqual(filename_for("rock/pop", "{name}.m3u8"),
                         "rock⧸pop.m3u8")

    def test_unicode_normalisation(self):
        from lemonzest.playlists import norm_name
        nfc = "Tiến"          # precomposed
        nfd = "Tiến"  # decomposed
        self.assertEqual(norm_name(nfc), norm_name(nfd))


if __name__ == "__main__":
    unittest.main(verbosity=2)
