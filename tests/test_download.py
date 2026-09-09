"""Tests for the YouTube download path.

No network and no real yt-dlp: a stub script stands in for it, printing the
same two parsed markers the real one is asked to print and writing a file
where the real one would. What is being tested is everything Lemon Zest is
responsible for - the command it builds, the cookie source it picks, the
sense it makes of a failure, and the catalog and playlist state afterwards.

    python -m unittest discover -s tests -v
"""
import os

# Automatic enrichment follows every scan and download. Switched off for the
# suite: these tests are about the catalog and the sync, and none of them
# should be making rate-limited calls to somebody else's service.
os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, download, playlists  # noqa: E402

# Stands in for yt-dlp. Writes the files named in LZ_FAKE_FILES (relative to
# the -o template's folder), prints a progress line and the after_move line
# for each, and exits with LZ_FAKE_CODE.
FAKE = '''
import json, os, sys, time
args = sys.argv[1:]
names = [n for n in os.environ.get("LZ_FAKE_FILES", "").split(";") if n]

# Listing mode: what `probe` and the batch counter ask for. One entry per
# file this stub would write, which is what makes "3 of 5" testable.
if "-J" in args:
    if os.environ.get("LZ_FAKE_LIST_FAILS"):
        sys.stderr.write("ERROR: cannot list")
        sys.exit(1)
    entries = [{"title": os.path.basename(n),
                "url": "https://example.test/%d" % i, "duration": 1}
               for i, n in enumerate(names)]
    one = len(entries) == 1 and not os.environ.get("LZ_FAKE_PLAYLIST")
    print(json.dumps({"title": "stub", "uploader": "stub", "duration": 1}
                     if one else {"title": "stub list", "entries": entries}))
    sys.exit(0)

out = args[args.index("-o") + 1]
paths = [args[i + 1] for i, a in enumerate(args) if a == "-P"]
home = next((p[len("home:"):] for p in paths if p.startswith("home:")), "")
root = os.path.join(home, out.split("%(")[0])
# Never write relative to the caller's working directory: a stub that
# guesses its destination wrong once wrote test files into the repo.
assert os.path.isabs(root), "refusing to write to a relative root: " + root
archive = None
if "--download-archive" in args:
    archive = args[args.index("--download-archive") + 1]
seen = set()
if archive and os.path.exists(archive):
    seen = set(open(archive, encoding="utf-8").read().splitlines())
for name in names:
    if name in seen:
        print("[download] " + name + " has already been recorded in the archive")
        continue
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\\0" * 2048)
    idx = names.index(name) + 1
    print("[lz-progress]1024|2048|%d|%d|512000|3|%s"
          % (idx, len(names), os.path.basename(name)))
    print("[lz-progress]2048|2048|%d|%d|512000|0|%s"
          % (idx, len(names), os.path.basename(name)))
    print("[lz-file]" + path)
    if archive:
        with open(archive, "a", encoding="utf-8") as fh:
            fh.write(name + "\\n")
sys.stdout.write(os.environ.get("LZ_FAKE_STDERR", ""))
linger = float(os.environ.get("LZ_FAKE_LINGER", "0"))
if linger:
    # Deliberately unflushed: this is what yt-dlp does, and the point of
    # the test is that the parent asks the child not to buffer.
    print("[youtube] still working")
    time.sleep(linger)
sys.exit(int(os.environ.get("LZ_FAKE_CODE", "0")))
'''


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-dl-")
        self.root = os.path.join(self.tmp, "library")
        os.makedirs(self.root)
        self.con = db.connect(os.path.join(self.tmp, "catalog.db"))
        self.fake = os.path.join(self.tmp, "fake_ytdlp.py")
        with open(self.fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE)
        self._real_command = download.ytdlp_command
        download.ytdlp_command = lambda: [sys.executable, self.fake]
        self._env = dict(os.environ)
        os.environ["LZ_FAKE_FILES"] = "Radiohead/Pablo Honey/Creep.m4a"
        os.environ.pop("LZ_FAKE_CODE", None)
        os.environ.pop("LZ_FAKE_STDERR", None)
        os.environ.pop("LZ_FAKE_LINGER", None)

    def tearDown(self):
        download.ytdlp_command = self._real_command
        os.environ.clear()
        os.environ.update(self._env)
        self.con.close()

    def cfg(self, **over):
        base = dict(download.CONFIG_DEFAULTS)
        base["cookies_mode"] = "none"
        base.update(over)
        return base

    # ------------------------------------------------------- the round trip

    def test_download_indexes_what_arrives(self):
        """A downloaded file is in the catalog without a rescan."""
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["added"], 1)
        row = self.con.execute("SELECT * FROM track").fetchone()
        self.assertTrue(row["path"].endswith("Radiohead/Pablo Honey/Creep.m4a"))
        self.assertEqual(row["rel_path"], "Radiohead/Pablo Honey/Creep.m4a")
        self.assertEqual(row["root"], download.norm(os.path.abspath(self.root)))

    def test_download_can_fill_a_playlist(self):
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, playlist="fresh",
                                    cfg=self.cfg())
        self.assertEqual(summary["playlist"]["added"], 1)
        rows = self.con.execute(
            "SELECT p.name, e.track_id, e.title_hint FROM playlist_entry e "
            "JOIN playlist p ON p.id = e.playlist_id").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "fresh")
        self.assertIsNotNone(rows[0]["track_id"])

    def test_a_playlist_does_not_grow_on_a_second_download(self):
        """The bug the project exists to fix, on the download path too."""
        for _ in range(3):
            download.download(self.con, ["https://example.test/v"],
                              root=self.root, playlist="fresh",
                              cfg=self.cfg(), archive=False)
        n = self.con.execute("SELECT COUNT(*) FROM playlist_entry").fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_archive_stops_a_second_fetch(self):
        first = download.download(self.con, ["https://example.test/v"],
                                  root=self.root, cfg=self.cfg())
        second = download.download(self.con, ["https://example.test/v"],
                                   root=self.root, cfg=self.cfg())
        self.assertEqual(first["downloaded"], 1)
        self.assertEqual(second["downloaded"], 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, download.ARCHIVE_NAME)))

    def test_the_log_holds_the_whole_run(self):
        """What gets pasted into a bug report: the command, then the run."""
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, playlist="fresh",
                                    cfg=self.cfg())
        log = summary["log"]
        self.assertTrue(log[0].startswith("$ "))
        self.assertIn("--audio-format", log[0])
        self.assertTrue(any(l.startswith("wrote ") for l in log))
        self.assertTrue(any("indexed 1 added" in l for l in log))
        self.assertTrue(any("playlist fresh: 1 added" in l for l in log))

    def test_a_failure_carries_its_log(self):
        os.environ["LZ_FAKE_FILES"] = ""
        os.environ["LZ_FAKE_CODE"] = "1"
        os.environ["LZ_FAKE_STDERR"] = "ERROR: [youtube] xyz: Private video\n"
        with self.assertRaises(download.DownloadError) as caught:
            download.download(self.con, ["https://example.test/v"],
                              root=self.root, cfg=self.cfg())
        log = caught.exception.log
        self.assertTrue(log[0].startswith("$ "))
        self.assertTrue(any("Private video" in l for l in log))
        self.assertTrue(any("exited with status 1" in l for l in log))

    def test_every_line_is_reported_as_it_arrives(self):
        os.environ["LZ_FAKE_STDERR"] = "WARNING: something worth reading\n"
        seen = []
        download.download(self.con, ["https://example.test/v"], root=self.root,
                          cfg=self.cfg(),
                          on_event=lambda k, d, done, tot: seen.append((k, d)))
        kinds = [k for k, _ in seen]
        self.assertEqual(kinds[0], "start")
        # The URLs are listed before anything is fetched, which is where the
        # batch gets its denominator from.
        self.assertEqual(kinds[1], "listing")
        self.assertEqual(kinds[2], "command")
        self.assertIn("output", kinds)
        self.assertTrue(any("something worth reading" in d
                            for k, d in seen if k == "output"))

    def test_a_resumed_run_catalogs_what_an_earlier_one_left_behind(self):
        """Indexing happens after yt-dlp exits, so an interrupted run leaves
        audio on disk, a line in the archive, and no catalog row. The next
        run skips those files and does not name them, so only a rescan can
        find them - without it they stay invisible for good."""
        # An earlier run: the file and the archive line, but no catalog row.
        name = "Ghost/Album/Left Behind.m4a"
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\0" * 2048)
        with open(os.path.join(self.root, download.ARCHIVE_NAME), "a",
                  encoding="utf-8") as fh:
            fh.write(name + "\n")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM track").fetchone()[0], 0)

        os.environ["LZ_FAKE_FILES"] = name + ";New/Album/Fresh.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 1)   # only the new one
        self.assertEqual(summary["skipped"], 1)
        paths = [r["rel_path"] for r in
                 self.con.execute("SELECT rel_path FROM track ORDER BY rel_path")]
        self.assertIn(name, paths)
        self.assertIn("New/Album/Fresh.m4a", paths)
        self.assertTrue(any("rescan" in l for l in summary["log"]))

    def test_a_video_id_is_read_out_of_whatever_shape_the_url_has(self):
        self.assertEqual(
            download.video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ")
        self.assertEqual(
            download.video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=X"),
            "dQw4w9WgXcQ")
        self.assertEqual(download.video_id("https://youtu.be/dQw4w9WgXcQ"),
                         "dQw4w9WgXcQ")
        self.assertEqual(download.video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(download.video_id("not a video"))
        self.assertIsNone(download.video_id(""))

    # ------------------------------------------------------------ batch

    def test_the_batch_knows_how_many_items_it_set_out_to_fetch(self):
        """The number a progress bar needs and the old one could not give:
        counted by listing the URLs before anything is downloaded."""
        os.environ["LZ_FAKE_FILES"] = ("A/Album/One.m4a;A/Album/Two.m4a;"
                                       "A/Album/Three.m4a")
        seen = []
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg(),
                                    on_batch=seen.append)
        batch = summary["batch"]
        self.assertEqual(batch["total"], 3)
        self.assertEqual(batch["done"], 3)
        self.assertEqual(batch["downloaded"], 3)
        self.assertEqual(batch["failed"], 0)
        self.assertIsNone(batch["current"])
        self.assertIsNotNone(batch["finished"])

        # The caller was told as it happened, not only at the end - and the
        # denominator was there before the first file arrived.
        self.assertGreater(len(seen), 3)
        self.assertTrue(any(b["total"] == 3 and b["done"] == 0 for b in seen))
        self.assertEqual([b["done"] for b in seen][-1], 3)
        # Each report is a snapshot; a live dict would make them all equal.
        self.assertEqual(sorted({b["done"] for b in seen}), [0, 1, 2, 3])

    def test_the_batch_counts_what_the_archive_already_had(self):
        os.environ["LZ_FAKE_FILES"] = "A/Album/One.m4a;A/Album/Two.m4a"
        first = download.download(self.con, ["https://example.test/list"],
                                  root=self.root, cfg=self.cfg())
        self.assertEqual(first["batch"]["downloaded"], 2)

        again = download.download(self.con, ["https://example.test/list"],
                                  root=self.root, cfg=self.cfg())
        batch = again["batch"]
        self.assertEqual(batch["total"], 2)
        self.assertEqual(batch["skipped"], 2)
        self.assertEqual(batch["downloaded"], 0)
        # Everything the run set out to do is accounted for, whichever way
        # each item ended.
        self.assertEqual(batch["done"], batch["total"])

    def test_a_url_that_cannot_be_listed_still_counts_as_one_item(self):
        os.environ["LZ_FAKE_LIST_FAILS"] = "1"
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["batch"]["total"], 1)
        self.assertEqual(summary["batch"]["downloaded"], 1)
        self.assertTrue(any("could not list" in line
                            for line in summary["log"]))

    def test_the_current_item_carries_its_bytes_and_position(self):
        os.environ["LZ_FAKE_FILES"] = "A/Album/One.m4a;A/Album/Two.m4a"
        seen = []
        download.download(self.con, ["https://example.test/list"],
                          root=self.root, cfg=self.cfg(),
                          on_batch=seen.append)
        current = [b["current"] for b in seen if b["current"]]
        self.assertTrue(current)
        self.assertEqual(current[-1]["count"], 2)
        self.assertEqual(current[-1]["bytes_total"], 2048)
        self.assertEqual(current[-1]["speed"], 512000)
        self.assertIn("m4a", current[-1]["title"])

    def test_a_short_progress_line_from_an_older_yt_dlp_still_parses(self):
        p = download._parse_progress("[lz-progress]1024|2048|Some Title")
        self.assertEqual(p["bytes"], 1024)
        self.assertEqual(p["bytes_total"], 2048)
        self.assertEqual(p["title"], "Some Title")
        self.assertIsNone(p["index"])

    def test_a_title_with_a_pipe_in_it_survives_the_split(self):
        p = download._parse_progress("[lz-progress]1|2|3|4|5|6|A | B")
        self.assertEqual(p["title"], "A | B")
        self.assertEqual(p["index"], 3)

    def test_progress_counts_items_rather_than_bytes(self):
        """What a progress event carries is the batch, not the file.

        Bytes describe whichever file yt-dlp happens to be fetching and
        reset to zero at every track, so a bar drawn from them says nothing
        about a playlist. The bytes are still reported - on the current item,
        through on_batch, where they describe what they actually describe."""
        events, batches = [], []
        download.download(self.con, ["https://example.test/v"], root=self.root,
                          cfg=self.cfg(),
                          on_event=lambda k, d, done, tot: events.append(
                              (k, done, tot)),
                          on_batch=batches.append)
        kinds = [e[0] for e in events]
        self.assertIn("progress", kinds)
        self.assertIn("file", kinds)
        self.assertIn("done", kinds)
        # One item, fetched: the last word on the batch is 1 of 1.
        self.assertEqual(("file", 1, 1), events[[e[0] for e in events]
                                                .index("file")])
        for kind, done, total in events:
            if kind == "progress":
                self.assertLessEqual(done, total)
                self.assertEqual(total, 1)

        current = [b["current"] for b in batches if b["current"]]
        self.assertEqual(current[-1]["bytes"], 2048)
        self.assertEqual(current[-1]["bytes_total"], 2048)

    def test_output_arrives_while_the_download_is_still_running(self):
        """A log that only appears at the end is not a progress report.

        Iterating a text pipe reads ahead until its buffer fills, which on a
        real download meant minutes of silence while files landed on disk.
        The stub holds the pipe open after its first line to catch that.
        """
        os.environ["LZ_FAKE_FILES"] = ""
        os.environ["LZ_FAKE_LINGER"] = "2"
        seen = []
        started = time.time()
        download.download(self.con, ["https://example.test/v"], root=self.root,
                          cfg=self.cfg(),
                          on_event=lambda k, d, done, tot: seen.append(
                              (k, time.time() - started)))
        first_output = next(t for k, t in seen if k == "output")
        elapsed = time.time() - started
        self.assertGreater(elapsed, 1.5, "the stub should have lingered")
        self.assertLess(first_output, 1.0,
                        "output was withheld until the process exited")

    def test_several_files_from_one_url(self):
        os.environ["LZ_FAKE_FILES"] = "A/Album/one.m4a;A/Album/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, playlist="set",
                                    cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["playlist"]["added"], 2)

    # ------------------------------------------------------------- failures

    def test_a_failure_that_downloaded_nothing_raises(self):
        os.environ["LZ_FAKE_FILES"] = ""
        os.environ["LZ_FAKE_CODE"] = "1"
        os.environ["LZ_FAKE_STDERR"] = (
            "ERROR: [youtube] xyz: Sign in to confirm you are not a bot\n")
        with self.assertRaises(download.DownloadError) as caught:
            download.download(self.con, ["https://example.test/v"],
                              root=self.root, cfg=self.cfg())
        self.assertIn("cookies", str(caught.exception))

    def test_a_partial_failure_keeps_what_arrived(self):
        """One dead video in a playlist must not discard the rest."""
        os.environ["LZ_FAKE_CODE"] = "1"
        os.environ["LZ_FAKE_STDERR"] = "ERROR: [youtube] zzz: Private video\n"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["exit_code"], 1)
        self.assertTrue(summary["errors"])

    def test_an_empty_scanned_folder_is_somewhere_to_download_into(self):
        """A new library starts empty; downloading is how it fills.

        Before library_root existed, a folder was known only through the
        tracks in it, so scanning an empty one registered nothing and the
        first download had no destination to offer.
        """
        from lemonzest import scan as scan_mod
        scan_mod.scan(self.con, self.root)
        self.assertEqual(db.roots(self.con),
                         [download.norm(os.path.abspath(self.root))])

        summary = download.download(self.con, ["https://example.test/v"],
                                    cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["root"],
                         download.norm(os.path.abspath(self.root)))

    def test_a_folder_downloaded_into_is_a_library_folder(self):
        download.download(self.con, ["https://example.test/v"],
                          root=self.root, cfg=self.cfg())
        self.assertIn(download.norm(os.path.abspath(self.root)),
                      db.roots(self.con))

    def test_an_unknown_folder_is_refused_before_anything_runs(self):
        with self.assertRaises(download.DownloadError):
            download.download(self.con, ["https://example.test/v"],
                              root=os.path.join(self.tmp, "nope"),
                              cfg=self.cfg())

    def test_a_file_written_outside_the_library_is_not_catalogued(self):
        """A stale row pointing out of the root would be deleted by the next
        scan of that root, so it is never written in the first place."""
        os.environ["LZ_FAKE_FILES"] = "../elsewhere/stray.m4a"
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["failed_index"], 1)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM track").fetchone()[0], 0)

    def test_hints_name_the_fix(self):
        self.assertIn("cookies.txt",
                      download.explain("ERROR: Sign in to confirm you are "
                                       "not a bot"))
        self.assertIn("age-restricted",
                      download.explain("ERROR: Sign in to confirm your age. "
                                       "This video may be inappropriate for "
                                       "some users."))
        self.assertIn("ffmpeg", download.explain("ERROR: ffprobe not found"))
        self.assertIsNone(download.explain("ERROR: something else entirely"))

    # -------------------------------------------------------------- cookies

    def test_cookies_off_passes_no_cookie_arguments(self):
        args = download.build_args(self.cfg(), ["u"], self.root)
        self.assertNotIn("--cookies-from-browser", args)
        self.assertNotIn("--cookies", args)

    def test_a_cookies_file_is_used_when_it_exists(self):
        path = os.path.join(self.tmp, "cookies.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
        args = download.build_args(
            self.cfg(cookies_mode="file", cookies_file=path), ["u"], self.root)
        self.assertIn("--cookies", args)
        self.assertEqual(args[args.index("--cookies") + 1], path)

    def test_a_missing_cookies_file_does_not_stop_a_download(self):
        """Better a public video than a refusal to start."""
        cfg = self.cfg(cookies_mode="file",
                       cookies_file=os.path.join(self.tmp, "absent.txt"))
        status = download.cookie_status(cfg)
        self.assertEqual(status["source"], "none")
        self.assertIn("not on disk", status["detail"])
        self.assertEqual(download.cookie_args(cfg), [])

    def test_firefox_is_preferred_and_names_its_profile(self):
        profile = os.path.join(self.tmp, "ff", "Profiles", "abc.default")
        os.makedirs(profile)
        open(os.path.join(profile, "cookies.sqlite"), "wb").close()
        real = download._firefox_dirs
        download._firefox_dirs = lambda: [os.path.join(self.tmp, "ff")]
        try:
            cfg = self.cfg(cookies_mode="auto")
            status = download.cookie_status(cfg)
            self.assertEqual(status["source"], "firefox")
            self.assertEqual(status["profile"]["name"], "abc.default")
            args = download.cookie_args(cfg)
            self.assertEqual(args[0], "--cookies-from-browser")
            self.assertTrue(args[1].startswith("firefox:"))
            self.assertTrue(args[1].endswith("abc.default"))
        finally:
            download._firefox_dirs = real

    def test_auto_falls_back_to_the_file_when_firefox_is_absent(self):
        path = os.path.join(self.tmp, "cookies.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
        real = download._firefox_dirs
        download._firefox_dirs = lambda: [os.path.join(self.tmp, "no-firefox")]
        try:
            status = download.cookie_status(
                self.cfg(cookies_mode="auto", cookies_file=path))
            self.assertEqual(status["source"], "file")
        finally:
            download._firefox_dirs = real

    # --------------------------------------------------------- the command

    def test_the_command_keeps_files_inside_the_library(self):
        args = download.build_args(self.cfg(), ["u"], self.root)
        # The template stays relative: yt-dlp ignores --paths entirely when
        # the output template is absolute, which would put the part files
        # beside the finished audio instead of out of the scanner's way.
        out = args[args.index("-o") + 1]
        self.assertFalse(os.path.isabs(out))
        paths = [args[i + 1] for i, a in enumerate(args) if a == "-P"]
        self.assertIn("home:" + self.root, paths)
        self.assertTrue(any(p.startswith("temp:")
                            and download.INCOMPLETE_DIR in p for p in paths))
        # A user's own yt-dlp.conf must not redirect the output.
        self.assertIn("--ignore-config", args)
        # --print implies --quiet, which the WHEN prefix does not undo, and
        # a quiet yt-dlp emits no progress at all: the bar never moved and
        # the log held only warnings. A stub cannot catch this, since it
        # prints whatever it likes regardless.
        self.assertIn("--no-quiet", args)
        self.assertLess(args.index("--print"), args.index("--no-quiet"))
        self.assertEqual(args[-1], "u")

    # --------------------------------------------------- javascript runtime

    def test_extra_js_runtimes_are_enabled_when_yt_dlp_knows_the_option(self):
        """YouTube needs a JS runtime, and yt-dlp enables only deno itself.

        Lemon Zest passes --ignore-config, so a user with Node but no Deno
        cannot fix this in their own yt-dlp.conf. It has to happen here.
        """
        key = (tuple(download.ytdlp_command()), "--js-runtimes")
        download._OPTION_SUPPORT[key] = True
        args = download.build_args(self.cfg(), ["u"], self.root)
        pairs = [(args[i], args[i + 1]) for i, a in enumerate(args)
                 if a == "--js-runtimes"]
        self.assertEqual([p[1] for p in pairs], ["node", "bun", "quickjs"])

    def test_an_older_yt_dlp_is_not_handed_an_option_it_lacks(self):
        key = (tuple(download.ytdlp_command()), "--js-runtimes")
        download._OPTION_SUPPORT[key] = False
        args = download.build_args(self.cfg(), ["u"], self.root)
        self.assertNotIn("--js-runtimes", args)

    def test_runtimes_can_be_turned_off(self):
        key = (tuple(download.ytdlp_command()), "--js-runtimes")
        download._OPTION_SUPPORT[key] = True
        args = download.build_args(self.cfg(js_runtimes=""), ["u"], self.root)
        self.assertNotIn("--js-runtimes", args)

    def test_the_runtime_yt_dlp_would_pick_is_reported(self):
        real = download.shutil.which
        download.shutil.which = lambda n: (r"C:/bin/node.exe"
                                           if n == "node" else None)
        try:
            status = download.js_runtime_status(self.cfg())
            self.assertEqual(status["chosen"]["name"], "node")
            # Deno is enabled whatever the config says, being yt-dlp's own
            # default, so its absence must not be reported as a choice.
            self.assertIn("deno", status["enabled"])
            status = download.js_runtime_status(self.cfg(js_runtimes=""))
            self.assertIsNone(status["chosen"])
            self.assertIn("not enabled", status["detail"])
        finally:
            download.shutil.which = real

    def test_a_missing_runtime_is_named_as_the_cause(self):
        hint = download.explain(
            "ERROR: [youtube] abc: The page needs to be reloaded.")
        self.assertIn("JavaScript runtime", hint)
        self.assertIn("Deno", hint)
        self.assertIn("JavaScript runtime",
                      download.explain("WARNING: n challenge solving failed"))

    def test_config_round_trips(self):
        download.set_config(self.con, cookies_mode="firefox",
                            audio_format="opus", nonsense="x")
        cfg = download.get_config(self.con)
        self.assertEqual(cfg["cookies_mode"], "firefox")
        self.assertEqual(cfg["audio_format"], "opus")
        self.assertNotIn("nonsense", cfg)
        args = download.build_args(cfg, ["u"], self.root)
        self.assertEqual(args[args.index("--audio-format") + 1], "opus")

    def test_the_busiest_library_folder_is_the_default_destination(self):
        download.download(self.con, ["https://example.test/v"], root=self.root,
                          cfg=self.cfg())
        os.environ["LZ_FAKE_FILES"] = "B/Album/next.m4a"
        summary = download.download(self.con, ["https://example.test/w"],
                                    cfg=self.cfg())
        self.assertEqual(summary["root"], download.norm(os.path.abspath(self.root)))

    # ------------------------------------------------------------ playlists

    def test_append_tracks_is_idempotent(self):
        download.download(self.con, ["https://example.test/v"], root=self.root,
                          cfg=self.cfg())
        ids = [r["id"] for r in self.con.execute("SELECT id FROM track")]
        first = playlists.append_tracks(self.con, "mix", ids)
        again = playlists.append_tracks(self.con, "mix", ids)
        self.assertEqual(first["added"], 1)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(again["entries"], 1)


if __name__ == "__main__":
    unittest.main()
