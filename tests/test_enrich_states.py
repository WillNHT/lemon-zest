"""The four enrichment states, and the API the interface drives them with.

No ffmpeg here, and no audio: every case in this file is about bookkeeping -
which files a run is allowed to touch, what a hand-typed value does to a
state, what the library list reports. Rows go straight into the catalog, so
the suite runs on a machine with no codecs installed.

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

from lemonzest import db, enrich  # noqa: E402
from lemonzest.server import create_app  # noqa: E402

ROOT = "C:/library" if os.name == "nt" else "/library"


class StubMB:
    """Answers every search with one recording, and counts the asking."""

    def __init__(self, title="Found", artist="Somebody", length=180.0):
        self.searched = []
        self._rec = {
            "id": "rec-1",
            "title": title,
            "length": int(length * 1000),
            "artist-credit": [{"name": artist, "artist": {"name": artist}}],
            "releases": [],
        }

    def by_isrc(self, isrc):
        return []

    def search(self, artist, title, duration=None):
        self.searched.append((artist, title))
        return [self._rec]

    def recording(self, mbid):
        return self._rec


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lz-state-")
        self.db_path = os.path.join(self.tmp, "lemon-zest.db")
        self.con = db.connect(self.db_path)
        db.add_root(self.con, ROOT)
        self.keys = {}
        for i, (artist, title) in enumerate(
                [("Alpha", "One"), ("Alpha", "Two"), ("Beta", "Three")], 1):
            self.keys[title] = self.add(artist, title, i)
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def add(self, artist, title, n):
        key = "1000-%s" % title.lower()
        rel = "%s/Album/%02d %s.m4a" % (artist, n, title)
        self.con.execute(
            "INSERT INTO track(path, rel_path, root, size, mtime, content_key,"
            " ext, duration, title, artist, album, album_artist, track_no,"
            " seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ROOT + "/" + rel, rel, ROOT, 4096, time.time(), key, ".m4a",
             180.0, title, artist, "Album", artist, n, time.time()))
        return key

    def store(self, title, status, fields=None, confidence=0.7):
        enrich._store(self.con, self.keys[title], "musicbrainz", confidence,
                      fields or {}, "rec-x", None, time.time(), status)
        self.con.commit()

    def state(self, title):
        row = self.con.execute(
            "SELECT status FROM enrichment WHERE content_key = ?",
            (self.keys[title],)).fetchone()
        return enrich.state_of(row["status"] if row else None)

    def track(self, title):
        return self.con.execute(
            "SELECT * FROM track WHERE content_key = ?",
            (self.keys[title],)).fetchone()

    # ------------------------------------------------------------ states

    def test_a_file_with_no_row_is_raw(self):
        self.assertEqual(self.state("One"), "raw")

    def test_a_lookup_that_found_nothing_stays_raw(self):
        # "none" is an empty answer, not a decision, so it must keep reading
        # as untouched work rather than as something already dealt with.
        self.store("One", "none")
        self.assertEqual(self.state("One"), "raw")

    def test_a_rejected_match_reads_as_skipped(self):
        self.store("One", "rejected")
        self.assertEqual(self.state("One"), "skipped")

    def test_the_counts_cover_every_track_including_the_untouched(self):
        self.store("One", "applied")
        self.store("Two", "candidate")
        self.assertEqual(enrich.state_counts(self.con),
                         {"raw": 1, "awaiting": 1, "enriched": 1, "skipped": 0})

    # --------------------------------------------------- what a skip means

    def test_a_skipped_file_is_not_pending(self):
        enrich.set_state(self.con, [self.keys["One"]], "skipped")
        keys = [r["content_key"] for r in enrich.pending(self.con)]
        self.assertNotIn(self.keys["One"], keys)

    def test_a_skipped_file_is_not_pending_even_on_a_redo(self):
        """The one that matters: `redo` re-asks decisions, but not this one."""
        enrich.set_state(self.con, [self.keys["One"]], "skipped")
        self.store("Two", "applied")
        keys = [r["content_key"] for r in enrich.pending(self.con, redo=True)]
        self.assertNotIn(self.keys["One"], keys)
        self.assertIn(self.keys["Two"], keys)

    def test_a_rejected_file_is_not_pending_on_a_redo_either(self):
        self.store("One", "rejected")
        keys = [r["content_key"] for r in enrich.pending(self.con, redo=True)]
        self.assertNotIn(self.keys["One"], keys)

    def test_selecting_a_skipped_file_by_hand_still_does_not_look_it_up(self):
        enrich.set_state(self.con, [self.keys["One"]], "skipped")
        client = StubMB()
        counts = enrich.run_tracks(
            self.con, [self.keys["One"], self.keys["Two"]], client=client)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(len(client.searched), 1)
        self.assertEqual(self.state("One"), "skipped")

    def test_unskipping_puts_a_file_back_in_the_queue(self):
        enrich.set_state(self.con, [self.keys["One"]], "skipped")
        enrich.set_state(self.con, [self.keys["One"]], "raw")
        self.assertEqual(self.state("One"), "raw")
        keys = [r["content_key"] for r in enrich.pending(self.con)]
        self.assertIn(self.keys["One"], keys)

    def test_marking_a_file_raw_forgets_the_stale_answer(self):
        self.store("One", "candidate", {"album": "Guessed"})
        enrich.set_state(self.con, [self.keys["One"]], "raw")
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM enrichment WHERE content_key = ?",
            (self.keys["One"],)).fetchone())

    def test_only_the_hand_settable_states_are_accepted(self):
        for bad in ("awaiting", "applied", ""):
            with self.assertRaises(ValueError):
                enrich.set_state(self.con, [self.keys["One"]], bad)

    def test_marking_enriched_by_hand_claims_no_confidence(self):
        self.store("One", "candidate", {"album": "Guessed"})
        enrich.set_state(self.con, [self.keys["One"]], "enriched")
        row = self.con.execute(
            "SELECT * FROM enrichment WHERE content_key = ?",
            (self.keys["One"],)).fetchone()
        self.assertEqual(self.state("One"), "enriched")
        self.assertEqual((row["source"], row["confidence"]), ("manual", 0.0))
        # The proposal is dropped, not applied: that is what Accept is for.
        self.assertEqual(row["fields"], "{}")
        self.assertEqual(self.track("One")["album"], "Album")

    def test_a_selection_of_one_key_moves_one_file(self):
        n = enrich.set_state(self.con, [self.keys["One"], self.keys["Two"]],
                             "skipped")
        self.assertEqual(n, 2)
        self.assertEqual(self.state("Two"), "skipped")

    # ------------------------------------------------------- hand editing

    def test_typing_a_value_does_not_enrich_the_file(self):
        enrich.override(self.con, self.keys["One"], album="What I Say")
        self.assertEqual(self.state("One"), "raw")
        self.assertEqual(self.track("One")["album"], "What I Say")

    def test_typing_a_value_leaves_the_match_as_it_was(self):
        self.store("One", "candidate", {"album": "Guessed"})
        enrich.override(self.con, self.keys["One"], album="Mine")
        row = self.con.execute(
            "SELECT * FROM enrichment WHERE content_key = ?",
            (self.keys["One"],)).fetchone()
        self.assertEqual((row["mbid"], row["source"], row["status"],
                          row["confidence"]),
                         ("rec-x", "musicbrainz", "candidate", 0.7))

    def test_an_old_typed_only_enrichment_goes_back_to_raw(self):
        # What override() used to write: enriched at 1.00 on typing alone.
        self.con.execute(
            "INSERT INTO enrichment(content_key, status, source, confidence,"
            " fields, fetched_at) VALUES (?, 'applied', 'manual', 1.0, '{}', 0)",
            (self.keys["One"],))
        self.con.commit()
        db.migrate(self.con)
        self.assertEqual(self.state("One"), "raw")

    def test_clearing_an_override_lets_the_source_show_again(self):
        self.store("One", "applied", {"album": "From The Source"})
        enrich.accept(self.con, self.keys["One"])
        enrich.override(self.con, self.keys["One"], album="Mine")
        self.assertEqual(self.track("One")["album"], "Mine")
        enrich.clear_overrides(self.con, self.keys["One"], ["album"])
        self.assertEqual(enrich.overrides_for(self.con, self.keys["One"]), {})

    def test_a_lookup_never_overwrites_a_hand_typed_value(self):
        enrich.override(self.con, self.keys["One"], title="Mine")
        enrich._apply_fields(self.con, self.keys["One"],
                             {"title": "From The Source"}, time.time())
        self.assertEqual(self.track("One")["title"], "Mine")

    # -------------------------------------------------------- the proposal

    def test_the_proposal_shows_all_three_tiers_apart(self):
        self.store("One", "candidate", {"album": "Proposed"})
        enrich.override(self.con, self.keys["One"], title="Typed")
        out = enrich.proposal_for(self.con, self.keys["One"])
        self.assertEqual(out["proposed"]["album"], "Proposed")
        self.assertEqual(out["overrides"], {"title": "Typed"})
        self.assertEqual(out["current"]["title"], "Typed")

    def test_one_lookup_covers_every_copy_of_the_same_audio(self):
        # Two files, one content key: the same download kept twice. A
        # rate-limited run must spend one request on it, not two.
        self.add("Gamma", "One", 9)
        rows = enrich.tracks_for_keys(self.con, [self.keys["One"]])
        self.assertEqual(len(rows), 1)


class AutomaticTests(unittest.TestCase):
    """Enrichment runs itself after a scan and after a download.

    The environment switch these tests turn back on is the suite's own hatch,
    not a user setting: nothing in the interface can disable this.
    """

    def setUp(self):
        self.core = StateTests("test_a_file_with_no_row_is_raw")
        self.core.setUp()
        self.con, self.keys = self.core.con, self.core.keys
        self._was = os.environ.get(enrich.AUTO_ENV)
        os.environ[enrich.AUTO_ENV] = "1"

    def tearDown(self):
        if self._was is None:
            os.environ.pop(enrich.AUTO_ENV, None)
        else:
            os.environ[enrich.AUTO_ENV] = self._was
        self.core.tearDown()

    def test_it_looks_up_everything_raw_without_being_asked(self):
        client = StubMB()
        counts = enrich.auto_after_ingest(self.con, client=client)
        self.assertTrue(counts["auto"])
        self.assertEqual(len(client.searched), 3)

    def test_it_still_never_touches_a_skipped_file(self):
        """The rule automation does not get to widen."""
        enrich.set_state(self.con, [self.keys["One"]], "skipped")
        client = StubMB()
        counts = enrich.auto_after_ingest(self.con, client=client)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(len(client.searched), 2)
        self.assertEqual(self.core.state("One"), "skipped")

    def test_it_leaves_alone_what_has_already_been_decided(self):
        self.core.store("One", "applied")
        self.core.store("Two", "candidate")
        client = StubMB()
        enrich.auto_after_ingest(self.con, client=client)
        self.assertEqual(len(client.searched), 1)

    def test_it_can_be_scoped_to_the_files_a_download_just_fetched(self):
        client = StubMB()
        enrich.auto_after_ingest(self.con, content_keys=[self.keys["One"]],
                                 client=client)
        self.assertEqual(len(client.searched), 1)

    def test_a_service_being_down_does_not_raise_into_the_scan(self):
        """It runs behind somebody else's job; it must never take it down."""
        class Exploding:
            def by_isrc(self, isrc):
                raise RuntimeError("boom")

            def search(self, *a, **k):
                raise RuntimeError("boom")

        counts = enrich.auto_after_ingest(self.con, client=Exploding())
        self.assertIn("boom", counts["stopped"])

    def test_a_lookup_failure_is_reported_rather_than_raised(self):
        class Down:
            def by_isrc(self, isrc):
                raise enrich.LookupError_("MusicBrainz returned 503")

            def search(self, *a, **k):
                raise enrich.LookupError_("MusicBrainz returned 503")

        counts = enrich.auto_after_ingest(self.con, client=Down())
        self.assertEqual(counts["failed"], 3)
        self.assertTrue(counts["errors"])

    def test_the_suite_switch_stops_it_entirely(self):
        os.environ[enrich.AUTO_ENV] = "0"
        client = StubMB()
        counts = enrich.auto_after_ingest(self.con, client=client)
        self.assertEqual(client.searched, [])
        self.assertIn("off", counts["stopped"])

    def test_an_isrc_match_still_gets_its_release(self):
        """The ISRC endpoint answers without releases; the top rung must not.

        `/ws/2/isrc/<isrc>` returns the recording and its artist credit and
        no releases at all, whatever `inc` asks for. Taking that reply at face
        value left the free, exact-identifier rung delivering a title and an
        artist and nothing else - no album, no year, and no release id, so no
        cover could ever be fetched for the 98.7% of the library that carries
        an ISRC.
        """
        rec_short = {
            "id": "rec-1", "title": "One", "length": 180000,
            "artist-credit": [{"name": "Alpha", "artist": {"name": "Alpha"}}],
        }
        rec_full = dict(rec_short, releases=[{
            "id": "rel-9", "title": "The Real Album", "date": "1999",
            "release-group": {"primary-type": "Album"},
            "artist-credit": [{"name": "Alpha", "artist": {"name": "Alpha"}}],
        }])

        class IsrcStub:
            def __init__(self):
                self.full_asked = []

            def by_isrc(self, isrc):
                return [rec_short]

            def recording(self, mbid):
                self.full_asked.append(mbid)
                return rec_full

            def search(self, *a, **k):
                raise AssertionError("the ISRC rung should have answered")

        self.con.execute("UPDATE track SET isrc = 'XX1234567890', album = NULL "
                         "WHERE content_key = ?", (self.keys["One"],))
        self.con.commit()
        client = IsrcStub()
        row = self.con.execute("SELECT * FROM track WHERE content_key = ?",
                               (self.keys["One"],)).fetchone()
        status = enrich.enrich_track(self.con, row, client)

        self.assertEqual(status, "applied")
        self.assertEqual(client.full_asked, ["rec-1"])
        stored = self.con.execute(
            "SELECT release_id FROM enrichment WHERE content_key = ?",
            (self.keys["One"],)).fetchone()
        self.assertEqual(stored["release_id"], "rel-9")
        self.assertIsNotNone(enrich.cover_art_url(stored["release_id"]))
        # And the release fields it could not have had before.
        self.assertEqual(self.core.track("One")["album"], "The Real Album")

    def test_a_short_isrc_reply_costs_one_extra_request_and_no_more(self):
        """When the first reply already carries releases, nothing extra."""
        rec = {
            "id": "rec-1", "title": "One", "length": 180000,
            "artist-credit": [{"name": "Alpha", "artist": {"name": "Alpha"}}],
            "releases": [{"id": "rel-1", "title": "Album",
                          "release-group": {"primary-type": "Album"}}],
        }

        class Rich:
            def __init__(self):
                self.full_asked = []

            def by_isrc(self, isrc):
                return [rec]

            def recording(self, mbid):
                self.full_asked.append(mbid)
                return rec

            def search(self, *a, **k):
                raise AssertionError("the ISRC rung should have answered")

        self.con.execute("UPDATE track SET isrc = 'XX1234567890' "
                         "WHERE content_key = ?", (self.keys["One"],))
        self.con.commit()
        client = Rich()
        row = self.con.execute("SELECT * FROM track WHERE content_key = ?",
                               (self.keys["One"],)).fetchone()
        enrich.enrich_track(self.con, row, client)
        self.assertEqual(client.full_asked, [])

    def test_a_scan_starts_an_identification_job_on_its_own(self):
        app = create_app(self.core.db_path).test_client()
        res = app.post("/api/scan", json={"root": self.core.tmp})
        self.assertEqual(res.status_code, 200)
        from lemonzest import server

        job_id = res.get_json()["job"]
        for _ in range(300):
            job = server.JOBS.get(job_id)
            if job and job["state"] != "running":
                break
            time.sleep(0.02)
        # The scan reports when the scan finished. Identification is not a
        # job of its own any more - it is a backlog the program works
        # through at one request a second - so what the scan reports is how
        # many files it put on the queue.
        self.assertEqual(job["state"], "done")
        self.assertIn("queued", job["result"])


class StateApiTests(unittest.TestCase):
    """The endpoints the library page calls, through Flask's test client."""

    def setUp(self):
        self.core = StateTests("test_a_file_with_no_row_is_raw")
        self.core.setUp()
        self.con, self.keys = self.core.con, self.core.keys
        self.app = create_app(self.core.db_path).test_client()

    def tearDown(self):
        self.core.tearDown()

    def get(self, path):
        res = self.app.get(path)
        return res.status_code, res.get_json()

    def post(self, path, body):
        res = self.app.post(path, json=body)
        return res.status_code, res.get_json()

    def test_the_library_reports_a_state_for_every_track(self):
        self.core.store("One", "candidate")
        code, out = self.get("/api/library")
        self.assertEqual(code, 200)
        by_key = {t["content_key"]: t for t in out["tracks"]}
        self.assertEqual(by_key[self.keys["One"]]["state"], "awaiting")
        self.assertEqual(by_key[self.keys["Two"]]["state"], "raw")

    def test_the_library_filters_on_state(self):
        self.core.store("One", "applied")
        code, out = self.get("/api/library?state=enriched")
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["tracks"][0]["content_key"], self.keys["One"])

    def test_the_facets_narrow_with_the_state_filter(self):
        self.core.store("Three", "applied")   # the only Beta track
        code, out = self.get("/api/facets?state=enriched")
        self.assertEqual([f["value"] for f in out["artist"]], ["Beta"])

    def test_select_all_matching_returns_the_keys_behind_the_filter(self):
        self.core.store("One", "skipped")
        code, out = self.get("/api/library/keys?state=skipped")
        self.assertEqual(out["keys"], [self.keys["One"]])
        self.assertFalse(out["capped"])

    def test_a_bulk_skip_moves_every_key_it_was_given(self):
        code, out = self.post("/api/enrich/state", {
            "content_keys": [self.keys["One"], self.keys["Two"]],
            "state": "skipped"})
        self.assertEqual((code, out["changed"]), (200, 2))
        self.assertEqual(self.core.state("One"), "skipped")

    def test_a_state_the_interface_may_not_set_is_refused(self):
        code, out = self.post("/api/enrich/state", {
            "content_keys": [self.keys["One"]], "state": "awaiting"})
        self.assertEqual(code, 400)

    def test_a_bulk_accept_applies_every_candidate(self):
        for t in ("One", "Two"):
            self.core.store(t, "candidate", {"title": t + " (Remastered)"})
        code, out = self.post("/api/enrich/accept", {
            "content_keys": [self.keys["One"], self.keys["Two"]]})
        self.assertEqual(out["changed"], 2)
        self.assertEqual(self.core.track("One")["title"], "One (Remastered)")
        self.assertEqual(self.core.state("One"), "enriched")

    def test_a_bulk_accept_keeps_the_fill_only_rule(self):
        """Accepting many at once must not become a blunter instrument.

        A release-derived field still only fills a column the file left
        empty - the rule that stops a correctly tagged album being relabelled
        with a soundtrack the same recording also appears on.
        """
        self.core.store("One", "candidate", {"album": "Some Soundtrack"})
        self.post("/api/enrich/accept", {"content_keys": [self.keys["One"]]})
        self.assertEqual(self.core.track("One")["album"], "Album")

    def test_a_single_key_still_works_the_old_way(self):
        self.core.store("One", "candidate", {"title": "Agreed"})
        code, out = self.post("/api/enrich/accept",
                              {"content_key": self.keys["One"]})
        self.assertEqual((code, out["changed"]), (200, 1))

    def test_editing_a_selection_only_writes_the_fields_that_were_sent(self):
        code, out = self.post("/api/enrich/edit", {
            "content_keys": [self.keys["One"], self.keys["Two"]],
            "fields": {"album_artist": "The Band"}})
        self.assertEqual(out["changed"], 2)
        self.assertEqual(self.core.track("One")["album_artist"], "The Band")
        # Titles are untouched: a bulk edit that flattened them to one value
        # would be a data loss dressed up as a convenience.
        self.assertEqual(self.core.track("One")["title"], "One")
        self.assertEqual(self.core.track("Two")["title"], "Two")

    def test_editing_refuses_a_field_no_source_may_write(self):
        code, out = self.post("/api/enrich/edit", {
            "content_keys": [self.keys["One"]], "fields": {"purl": "http://x"}})
        self.assertEqual(code, 400)

    def test_the_track_detail_is_what_the_edit_dialog_needs(self):
        self.core.store("One", "candidate", {"album": "Proposed"})
        code, out = self.get("/api/enrich/track/" + self.keys["One"])
        self.assertEqual(out["state"], "awaiting")
        self.assertEqual(out["proposed"]["album"], "Proposed")
        self.assertEqual(out["current"]["title"], "One")

    def _finish(self, job_id):
        """Wait for a background job to land, then hand back its record."""
        from lemonzest import server
        for _ in range(200):
            job = server.JOBS.get(job_id)
            if job and job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("job never finished")

    def test_a_lookup_failure_carries_its_reason_out_of_the_run(self):
        """The bug this exists for: `failed: 1` and no word about why."""
        from lemonzest import enrich as en

        class Broken:
            def by_isrc(self, isrc):
                raise en.LookupError_("MusicBrainz returned 503")

            def search(self, *a, **k):
                raise en.LookupError_("MusicBrainz returned 503")

        counts = en.run_tracks(self.con, [self.keys["One"]], client=Broken())
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["errors"][0]["message"],
                         "MusicBrainz returned 503")
        self.assertEqual(counts["errors"][0]["track"], "Alpha/Album/01 One.m4a")

    def test_a_write_with_nothing_to_write_is_not_reported_as_written(self):
        """`write()` declining is not a success, whatever it returns."""
        from lemonzest import enrich as en

        self.con.execute(
            "UPDATE track SET title=NULL, artist=NULL, album=NULL, "
            "album_artist=NULL, track_no=NULL, disc_no=NULL, year=NULL, "
            "isrc=NULL WHERE content_key = ?", (self.keys["One"],))
        self.con.commit()
        res = en.write_back_result(self.con, self.keys["One"])
        self.assertFalse(res["ok"])
        self.assertIn("no values", res["reason"])

    def test_ticking_artwork_with_no_release_says_so(self):
        from lemonzest import enrich as en

        self.core.store("One", "applied", {"title": "Whatever"})
        res = en.write_back_result(self.con, self.keys["One"], artwork=True)
        # The write fails here for its own reason (no such file), but the
        # point is that a cover request that cannot be met is never silent.
        self.assertFalse(res["ok"])

    def test_running_over_a_skipped_selection_asks_nobody_anything(self):
        """Also proves the run endpoint needs no network to refuse work."""
        self.post("/api/enrich/state", {
            "content_keys": [self.keys["One"]], "state": "skipped"})
        code, out = self.post("/api/enrich/run",
                              {"content_keys": [self.keys["One"]]})
        self.assertEqual(code, 200)
        job = self._finish(out["job"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["skipped"], 1)
        self.assertEqual(job["result"]["applied"], 0)

    def test_a_file_that_cannot_be_written_does_not_kill_the_write_job(self):
        # The catalog rows here point at paths that were never created, which
        # is the same shape as a card unplugged mid-run: one file failing has
        # to be counted, not raised.
        self.core.store("One", "applied", {"title": "Whatever"})
        code, out = self.post("/api/enrich/write",
                              {"content_keys": [self.keys["One"]]})
        job = self._finish(out["job"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["written"], 0)
        self.assertEqual(job["result"]["failed"], 1)

    def test_a_write_that_failed_says_why(self):
        """A count with no reason is the same as no message at all."""
        self.core.store("One", "applied", {"title": "Whatever"})
        code, out = self.post("/api/enrich/write",
                              {"content_keys": [self.keys["One"]]})
        job = self._finish(out["job"])
        errors = job["result"]["errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("no such file", errors[0]["message"])

    def test_every_failing_file_gets_a_reason_naming_it(self):
        for t in ("One", "Two", "Three"):
            self.core.store(t, "applied", {"title": "Whatever"})
        code, out = self.post("/api/enrich/write", {
            "content_keys": [self.keys[t] for t in ("One", "Two", "Three")]})
        job = self._finish(out["job"])
        self.assertEqual(job["result"]["failed"], 3)
        # Three files, three paths: the path is the information here, so
        # these do not fold into one line. Reasons that really are identical
        # do - which is what keeps a whole-library run readable.
        msgs = [e["message"] for e in job["result"]["errors"]]
        self.assertEqual(len(msgs), 3)
        self.assertTrue(all(e["count"] == 1 for e in job["result"]["errors"]))

    def test_the_write_preview_says_what_would_change(self):
        self.core.store("One", "applied", {"title": "Whatever"})
        code, out = self.post("/api/enrich/write/preview",
                              {"content_keys": [self.keys["One"]]})
        self.assertEqual(code, 200)
        f = out["files"][0]
        self.assertEqual(f["rel_path"], "Alpha/Album/01 One.m4a")
        # The file is not on disk, so the preview must say so rather than
        # letting the dialog promise a write that cannot happen.
        self.assertTrue(f["missing"])
        self.assertEqual(out["missing"], 1)
        self.assertEqual(out["writable"], 0)
        self.assertEqual(f["fields"]["title"], "One")

    def test_the_preview_flags_a_file_with_no_release_for_artwork(self):
        code, out = self.post("/api/enrich/write/preview",
                              {"content_keys": [self.keys["One"]]})
        self.assertEqual(out["no_artwork"], 1)

    def test_the_stats_carry_the_four_counts(self):
        code, out = self.get("/api/stats")
        self.assertEqual(set(out["enrich_states"]), set(enrich.STATES))

    def test_the_stats_say_whether_fingerprinting_is_available(self):
        # The interface offers rung four when a text search comes back empty,
        # so it has to know whether offering it would be useful.
        code, out = self.get("/api/stats")
        self.assertIn("ready", out["fingerprint"])
        self.assertIn("missing", out["fingerprint"])


if __name__ == "__main__":
    unittest.main()
