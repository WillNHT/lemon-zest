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

import contextlib
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
                     if one else {"title": os.environ.get("LZ_FAKE_TITLE",
                                                          "stub list"),
                                  "entries": entries}))
    sys.exit(0)

# One entry of the listing, as the parallel workers ask for it: write only
# that entry's file. Anything else is the whole URL, and writes them all.
every = names
import re
entry = re.search(r"example\\.test/(\\d+)$", args[-1])
if entry:
    names = [every[int(entry.group(1))]]

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
        # Different bytes per file, as real downloads have: identical ones
        # share a content key, and the queue rightly looks that up once.
        fh.write(name.encode("utf-8") + b"\\0" * 2048)
    idx = every.index(name) + 1
    print("[lz-progress]1024|2048|%d|%d|512000|3|%s"
          % (idx, len(every), os.path.basename(name)))
    print("[lz-progress]2048|2048|%d|%d|512000|0|%s"
          % (idx, len(every), os.path.basename(name)))
    print("[lz-file]" + path)
    if archive:
        with open(archive, "a", encoding="utf-8") as fh:
            fh.write(name + "\\n")
    # A gate, when the test asks for one: the file is written and then
    # nothing more happens until whoever is watching says so. It is how a
    # test can insist that the work following a file happened before the
    # next file was fetched, rather than hoping the timing lands that way.
    gate = os.environ.get("LZ_FAKE_GATE")
    if gate:
        marker = os.path.join(gate, str(idx) + ".go")
        waited = 0.0
        while not os.path.exists(marker) and waited < 10.0:
            time.sleep(0.02)
            waited += 0.02
sys.stdout.write(os.environ.get("LZ_FAKE_STDERR", ""))
linger = float(os.environ.get("LZ_FAKE_LINGER", "0"))
if linger:
    # Deliberately unflushed: this is what yt-dlp does, and the point of
    # the test is that the parent asks the child not to buffer.
    print("[youtube] still working")
    time.sleep(linger)
sys.exit(int(os.environ.get("LZ_FAKE_CODE", "0")))
'''


@contextlib.contextmanager
def _stub_stream(cls):
    """Put a stand-in in place of the identification pass.

    The queue builds its own on a thread of its own, so there is nothing to
    hand one to: it is swapped on the module instead. The queue itself is
    reset around this, because it is one per catalog for the life of the
    process and would otherwise carry a stream - and a backlog - from one
    test into the next.
    """
    from lemonzest import enrich as en
    from lemonzest import enrichq
    enrichq.reset_queues()
    was = en.IngestStream
    en.IngestStream = cls
    try:
        yield
    finally:
        en.IngestStream = was
        enrichq.reset_queues()


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
        os.environ.pop("LZ_FAKE_GATE", None)

    def tearDown(self):
        from lemonzest import enrichq
        enrichq.reset_queues()
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
                              (k, d, time.time() - started)))
        finished = time.time() - started
        self.assertGreater(finished, 1.5, "the stub should have lingered")
        # The child's own line - not one of ours written before it started -
        # and it arrives while the child is still lingering, not at its exit.
        arrived = next(t for k, d, t in seen
                       if k == "output" and "still working" in d)
        self.assertLess(arrived, finished - 1.0,
                        "output was withheld until the process exited")

    # ------------------------------------------------ workers, pause, stop

    def test_a_playlist_is_fetched_several_at_a_time(self):
        os.environ["LZ_FAKE_FILES"] = ";".join(
            "A/x/%d.m4a" % i for i in range(6))
        seen = []
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root,
                                    cfg=self.cfg(workers="3"),
                                    on_batch=seen.append)
        self.assertEqual(summary["downloaded"], 6)
        self.assertEqual(summary["batch"]["workers"], 3)
        self.assertEqual([i["n"] for i in summary["batch"]["items"]],
                         [1, 2, 3, 4, 5, 6])

    def test_what_the_catalog_has_is_not_fetched_again(self):
        """Renamed or re-tagged since, a video is still that video."""
        self.con.execute(
            "INSERT INTO track(path, rel_path, root, size, mtime, content_key,"
            " ext, purl, seen_at) VALUES ('x', 'x', 'r', 1, 0, 'k', '.m4a', "
            "'https://www.youtube.com/watch?v=aaaaaaaaaaa', 0)")
        units = download._units(
            ["L"], {"L": {"entries": [
                {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
                {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb"}]}},
            False)
        have = download._already_have(self.con, self.root)
        self.assertEqual([u["vid"] in have for u in units], [True, False])

    def _run_with(self, control, **cfg):
        out = {}

        def go():
            out["summary"] = download.download(
                self.con_for_thread(), ["https://example.test/list"],
                root=self.root, cfg=self.cfg(**cfg), control=control,
                on_event=lambda k, d, done, tot: (
                    out.setdefault("first", time.time()) if k == "file"
                    else None))
        import threading
        t = threading.Thread(target=go)
        t.start()
        return t, out

    def con_for_thread(self):
        return db.connect(os.path.join(self.tmp, "catalog.db"))

    def test_a_pause_finishes_the_item_in_hand_and_starts_no_more(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a;A/x/three.m4a"
        os.environ["LZ_FAKE_LINGER"] = "0.5"
        control = download.DownloadControl()
        t, out = self._run_with(control, workers="1")
        while "first" not in out and t.is_alive():
            time.sleep(0.02)
        control.pause()
        t.join(timeout=20)
        summary = out["summary"]
        self.assertEqual(summary["stopped"], "paused")
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["remaining"], ["https://example.test/1",
                                                "https://example.test/2"])

    def test_a_stop_kills_what_is_being_fetched(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        os.environ["LZ_FAKE_LINGER"] = "30"
        control = download.DownloadControl()
        t, out = self._run_with(control, workers="2")
        while "first" not in out and t.is_alive():
            time.sleep(0.02)
        started = time.time()
        control.stop()
        t.join(timeout=20)
        self.assertLess(time.time() - started, 10, "yt-dlp was not killed")
        summary = out["summary"]
        self.assertEqual(summary["stopped"], "stopped")
        # Each item either arrived or is owed to a resume - never both, never
        # lost. Which way the second went depends on whether its worker had
        # written the file before the stop reached it.
        owed = summary["remaining"]
        self.assertGreaterEqual(summary["downloaded"], 1)
        self.assertEqual(summary["downloaded"] + len(owed), 2)
        self.assertLessEqual(len(owed), 1)
        self.assertTrue(set(owed) <= {"https://example.test/0",
                                      "https://example.test/1"}, owed)

    def test_several_files_from_one_url(self):
        os.environ["LZ_FAKE_FILES"] = "A/Album/one.m4a;A/Album/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, playlist="set",
                                    cfg=self.cfg())
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["playlist"]["added"], 2)

    # -------------------------------------------------- the arrival pipeline

    def test_each_file_is_carried_the_rest_of_the_way_as_it_lands(self):
        """Track one is finished while track two is still being fetched.

        The gate is the whole test: the stub will not write the second file
        until the first has been identified, so a run that waited for the
        download to finish before identifying anything cannot complete. The
        batch form this replaced could not pass it.
        """
        gate = os.path.join(self.tmp, "gate")
        os.makedirs(gate)
        os.environ["LZ_FAKE_GATE"] = gate
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a;A/x/three.m4a"

        seen = []

        class Stream:
            """Stands in for the identification pass. Opens the next gate."""

            def __init__(self, con, **kw):
                self.counts = dict(applied=0, candidates=0, unmatched=0,
                                   written=0, failed=0, skipped=0,
                                   backfilled=0, write_failed=0, stopped=None,
                                   errors=[], auto=True)

            def add(self, content_key):
                seen.append(content_key)
                self.counts["applied"] += 1
                open(os.path.join(gate, "%d.go" % len(seen)), "w").close()
                return {"state": "applied", "detail": "identified: A - x"}

        started = time.time()
        with _stub_stream(Stream):
            summary = download.download(self.con, ["https://example.test/l"],
                                        root=self.root, cfg=self.cfg())
        elapsed = time.time() - started

        # The gate gives up after ten seconds so a regression fails rather
        # than hangs; three gates waiting it out is thirty. Finishing well
        # inside that is the proof that each gate was opened by the work
        # rather than by the timeout - the bound has room for a slow machine
        # without ever reaching a single gate's patience.
        self.assertLess(elapsed, 9.0,
                        "the arrivals were not identified as they landed")
        self.assertEqual(summary["downloaded"], 3)
        self.assertEqual(len(seen), 3, "every arrival should have been "
                                       "identified as it landed")
        self.assertEqual(summary["queued"], 3)

    def test_the_batch_says_what_each_arrival_is_doing(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"

        class Stream:
            def __init__(self, con, **kw):
                self.counts = dict(applied=0, candidates=0, unmatched=0,
                                   written=0, failed=0, skipped=0,
                                   backfilled=0, write_failed=0, stopped=None,
                                   errors=[], auto=True)

            def add(self, content_key):
                self.counts["applied"] += 1
                return {"state": "applied", "detail": "identified: A - x"}

        snapshots = []
        with _stub_stream(Stream):
            summary = download.download(
                self.con, ["https://example.test/l"], root=self.root,
                cfg=self.cfg(),
                on_batch=lambda b: snapshots.append(b["items"]))

        states = {i["state"] for snap in snapshots for i in snap}
        self.assertTrue({"indexing", "queued"} <= states, states)
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["queued"], 2)
        # The queue works on its own clock, so the last word on each item
        # arrives after the download itself is over - and the batch, which
        # the page is still watching, keeps taking it.
        from lemonzest import enrichq
        enrichq.get_queue(download.db.path_of(self.con)).drain(timeout=10)
        final = snapshots[-1]
        self.assertEqual([i["n"] for i in final], [1, 2])

    def test_a_snapshot_is_a_copy_the_caller_can_keep(self):
        """The batch keeps changing under a caller that holds one."""
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        snapshots = []
        download.download(self.con, ["https://example.test/l"],
                          root=self.root, cfg=self.cfg(),
                          on_batch=lambda b: snapshots.append(b["items"]))
        first = next(s for s in snapshots if s)
        self.assertEqual(len(first), 1,
                         "a snapshot taken at one file grew a second one")

    def test_the_pipeline_can_be_turned_off(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a"
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, cfg=self.cfg(),
                                    pipeline=False)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["queued"], 0)

    def test_a_file_that_cannot_be_catalogued_is_said_so_and_not_fatal(self):
        """Written outside the library: no catalog row, nothing to look up."""
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        os.environ["LZ_FAKE_FILES"] = "../elsewhere/stray.m4a;A/x/one.m4a"
        summary = download.download(self.con, ["https://example.test/l"],
                                    root=self.root, cfg=self.cfg())
        # The batch goes on changing after the download is over - the queue
        # is still identifying what it was handed, on its own thread - so
        # the end state is the only one worth asserting. Waiting for the
        # queue is what makes this a test rather than a coin toss.
        from lemonzest import enrichq
        enrichq.get_queue(download.db.path_of(self.con)).drain(timeout=10)
        items = {i["n"]: i for i in summary["batch"]["items"]}
        self.assertEqual(items[1]["state"], "failed")
        self.assertEqual(items[1]["detail"], "could not be catalogued")
        self.assertEqual(items[2]["state"], "done")
        self.assertEqual(summary["failed_index"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["queued"], 1)

    # ------------------------------------------------ playlists and memory

    def test_a_playlist_url_becomes_a_playlist_without_being_asked(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual([p["name"] for p in summary["playlists"]],
                         ["stub list"])
        self.assertEqual(summary["playlist"]["added"], 2)

    def test_an_album_does_not_become_a_playlist(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        os.environ["LZ_FAKE_TITLE"] = "Album - Dookie"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["playlists"], [])
        self.assertEqual(summary["downloaded"], 2)
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM playlist").fetchone())

    def test_an_album_url_is_known_by_its_list_id(self):
        url = ("https://music.youtube.com/playlist?"
               "list=OLAK5uy_mrF_EHJJul_9cUfE-snfFgdEY_nggl9c0")
        self.assertTrue(playlists.is_album(url))
        self.assertFalse(playlists.is_album(
            "https://www.youtube.com/playlist?list=PLabc", {"title": "chill"}))

    def test_album_playlists_made_before_are_dropped(self):
        url = "https://music.youtube.com/playlist?list=OLAK5uy_abc"
        playlists.append_tracks(self.con, "Album - Dookie", [],
                                origin="youtube", source_uri=url)
        playlists.append_tracks(self.con, "chill", [], origin="youtube",
                                source_uri="https://youtube.com/playlist?list=PLx")
        db.migrate(self.con)
        self.assertEqual([r["name"] for r in self.con.execute(
            "SELECT name FROM playlist")], ["chill"])
        # And a file one of them left beside the library does not come back.
        folder = playlists.local_dir(self.root)
        playlists.write(os.path.join(folder, "Album - Dookie.m3u8"),
                        "Album - Dookie", [], source_uri=url)
        playlists.import_library(self.con, self.root)
        self.assertFalse(os.path.exists(
            os.path.join(folder, "Album - Dookie.m3u8")))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM playlist").fetchone()[0], 1)

    def test_a_single_video_does_not_become_a_playlist_of_one(self):
        summary = download.download(self.con, ["https://example.test/v"],
                                    root=self.root, cfg=self.cfg())
        self.assertEqual(summary["playlists"], [])
        self.assertIsNone(summary["playlist"])

    def test_a_named_playlist_still_wins(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, playlist="mine",
                                    cfg=self.cfg())
        self.assertEqual([p["name"] for p in summary["playlists"]], ["mine"])

    def test_the_playlist_is_written_beside_the_library(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        path = summary["playlists"][0]["file"]
        # Beside the library folder, not inside it: /Music and /Playlists,
        # the way a Rockbox card lays them out.
        self.assertEqual(os.path.normcase(os.path.dirname(path)),
                         os.path.normcase(os.path.join(
                             os.path.dirname(self.root), "Playlists")
                             .replace("\\", "/")))
        body = open(path, encoding="utf-8").read()
        self.assertIn("#PLAYLIST: stub list", body)
        # From the card's root, so it reads the same on the card as here.
        top = os.path.basename(self.root)
        self.assertIn("/%s/A/x/one.m4a" % top, body)
        self.assertNotIn("../", body)
        self.assertNotIn(self.root, body)
        # And it reads back: a rescan finds the same tracks it names.
        again = playlists.read(path)
        self.assertTrue(all(e["abs_path"] for e in again["entries"]))

    def test_a_second_run_rewrites_the_file_rather_than_growing_it(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        for _ in range(3):
            summary = download.download(self.con, ["https://example.test/l"],
                                        root=self.root, cfg=self.cfg())
        body = open(summary["playlists"][0]["file"], encoding="utf-8").read()
        self.assertEqual(body.count("/A/x/one.m4a"), 1)

    def test_a_playlist_in_the_old_place_moves_on_the_next_scan(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        summary = download.download(self.con, ["https://example.test/list"],
                                    root=self.root, cfg=self.cfg())
        new = summary["playlists"][0]["file"]
        os.remove(new)
        old = os.path.join(self.root, "playlists", "stub list.m3u8")
        playlists.write(old, "stub list", [("../A/x/one.m4a", "t", 1, None)],
                        source_uri="https://example.test/list")
        playlists.import_library(self.con, self.root)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_the_urls_asked_for_are_remembered(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        download.download(self.con, ["https://example.test/list"],
                          root=self.root, cfg=self.cfg())
        recent = download.recent_urls(self.con)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["url"], "https://example.test/list")
        self.assertEqual(recent[0]["title"], "stub list")
        self.assertEqual(recent[0]["is_playlist"], 1)
        self.assertEqual(recent[0]["playlist_name"], "stub list")
        self.assertEqual(recent[0]["uses"], 1)

    def test_the_recent_list_is_the_last_few_newest_first(self):
        for i in range(7):
            download.remember_url(self.con, "https://example.test/%d" % i)
        recent = download.recent_urls(self.con, 5)
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0]["url"], "https://example.test/6")

    def test_asking_twice_moves_a_url_up_rather_than_repeating_it(self):
        download.remember_url(self.con, "https://example.test/a")
        download.remember_url(self.con, "https://example.test/b")
        download.remember_url(self.con, "https://example.test/a")
        recent = download.recent_urls(self.con)
        self.assertEqual([r["url"] for r in recent],
                         ["https://example.test/a", "https://example.test/b"])
        self.assertEqual(recent[0]["uses"], 2)

    def test_a_kept_url_carries_its_playlist_and_where_it_stands(self):
        os.environ["LZ_FAKE_FILES"] = "A/x/one.m4a;A/x/two.m4a"
        download.download(self.con, ["https://example.test/list"],
                          root=self.root, cfg=self.cfg())
        download.keep_url(self.con, "https://example.test/list",
                          playlist_name="stub list")
        kept = download.kept_urls(self.con)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["playlist_name"], "stub list")
        self.assertEqual(kept[0]["entries"], 2)
        download.keep_url(self.con, "https://example.test/list", kept=False)
        self.assertEqual(download.kept_urls(self.con), [])

    def test_keeping_a_url_does_not_count_as_asking_for_it(self):
        """Keeping is not downloading: the recent list must not reorder."""
        download.remember_url(self.con, "https://example.test/a")
        download.keep_url(self.con, "https://example.test/b",
                          playlist_name="b")
        self.assertEqual(download.recent_urls(self.con)[0]["url"],
                         "https://example.test/a")

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

    def test_downloads_keep_only_the_year_and_a_square_cover(self):
        args = download.build_args(self.cfg(), ["u"], self.root)
        meta = args[args.index("--parse-metadata") + 1]
        self.assertTrue(meta.endswith(":%(meta_date)s"), meta)
        self.assertIn("upload_date>%Y", meta)
        ppa = args[args.index("--postprocessor-args") + 1]
        self.assertTrue(ppa.startswith("ThumbnailsConvertor+ffmpeg_o:"), ppa)
        self.assertIn("yuvj420p", ppa)

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
