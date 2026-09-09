"""Tests for the metadata enrichment ladder.

No network. MusicBrainz is replaced by a stub that returns canned recordings,
because the point of these tests is the matching and the bookkeeping, not
whether the service is up - and a test suite that needs a rate-limited
external API is a test suite nobody runs.

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

from lemonzest import db, enrich, scan, tags  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_mp3(path, seconds=2.0, **tagvals):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-v", "quiet", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "%.2f" % seconds,
         "-b:a", "64k", path], check=True)
    if tagvals:
        from mutagen.easyid3 import EasyID3
        t = EasyID3()
        for k, v in tagvals.items():
            if v is not None:
                t[k] = str(v)
        t.save(path)


def recording(rid, title, artists, album=None, date=None, length=None,
              isrc=None, secondary=None, primary="Album"):
    """A MusicBrainz recording, shaped the way the web service returns one."""
    rec = {
        "id": rid, "title": title,
        "artist-credit": [{"artist": {"id": f"a-{n}", "name": n},
                           "joinphrase": ", " if i < len(artists) - 1 else ""}
                          for i, n in enumerate(artists)],
    }
    if length is not None:
        rec["length"] = int(length * 1000)
    if isrc:
        rec["isrcs"] = [isrc]
    if album:
        rec["releases"] = [{
            "id": f"rel-{rid}", "title": album, "date": date,
            "release-group": {"primary-type": primary,
                              "secondary-types": secondary or []},
            "artist-credit": [{"artist": {"id": "a-0", "name": artists[0]}}],
        }]
    return rec


class StubMB:
    """Stands in for the MusicBrainz client. Counts what was asked for."""

    def __init__(self, by_isrc=None, search=None):
        self._isrc = by_isrc or {}
        self._search = search or []
        self.isrc_calls, self.search_calls = [], []

    def by_isrc(self, isrc):
        self.isrc_calls.append(isrc)
        return self._isrc.get(isrc, [])

    def search(self, artist, title, duration=None, limit=5):
        self.search_calls.append((artist, title, duration))
        return list(self._search)


# ------------------------------------------------------------ pure functions

class FoldTests(unittest.TestCase):
    def test_accents_and_case_fold_together(self):
        self.assertEqual(enrich.fold("Björk"), enrich.fold("bjork"))
        self.assertEqual(enrich.fold("Tiến"), enrich.fold("TIEN"))

    def test_punctuation_is_not_a_difference(self):
        self.assertEqual(enrich.fold("Don't Stop!"), enrich.fold("dont stop"))

    def test_video_decoration_is_stripped_from_a_title(self):
        self.assertEqual(enrich.clean_title("Blue (Official Video)"), "Blue")
        self.assertEqual(enrich.clean_title("Blue [Official Audio]"), "Blue")
        self.assertEqual(enrich.clean_title("Blue feat. Someone"), "Blue")

    def test_a_title_that_is_only_a_qualifier_is_left_alone(self):
        # Stripping everything would leave nothing to match on, which is a
        # worse outcome than matching a slightly noisy string.
        self.assertTrue(enrich.clean_title("Official Video"))


class CreditTests(unittest.TestCase):
    def test_a_writer_list_splits_into_names(self):
        names = enrich.credits(
            "Joji, Alexis Kesselman, Connor McDonough, Riley McDonough")
        self.assertIn("Joji", names)
        self.assertEqual(len(names), 4)

    def test_featured_artists_are_separate_credits(self):
        self.assertEqual(enrich.credits("A feat. B"), ["A", "B"])


class ParseTitleTests(unittest.TestCase):
    def test_artist_and_title_split_on_a_dash(self):
        artist, title = enrich.parse_video_title(
            "KIRINJI - 時間がない (Jikanga Nai)")
        self.assertEqual(artist, "KIRINJI")
        self.assertTrue(title.startswith("時間がない"))

    def test_a_title_with_no_separator_is_not_guessed_at(self):
        artist, title = enrich.parse_video_title("Just A Song")
        self.assertIsNone(artist)
        self.assertEqual(title, "Just A Song")


class ScoreTests(unittest.TestCase):
    def local(self, **kw):
        base = {"title": "Blue", "artist": "Alpha", "album_artist": "Alpha",
                "duration": 200.0}
        base.update(kw)

        class Row(dict):
            def keys(self):          # the shape score() reads from
                return list(dict.keys(self))

        return Row(base)

    def test_an_exact_match_clears_the_auto_threshold(self):
        s = enrich.score(self.local(),
                         {"title": "Blue", "artists": ["Alpha"], "duration": 201.0})
        self.assertGreaterEqual(s, enrich.AUTO)

    def test_a_wrong_length_is_a_veto_however_good_the_strings(self):
        # The live version of the right song by the right artist. Everything
        # agrees except the one field neither side could mistype.
        s = enrich.score(self.local(),
                         {"title": "Blue", "artists": ["Alpha"], "duration": 320.0})
        self.assertEqual(s, 0.0)

    def test_a_different_song_by_the_same_artist_does_not_pass(self):
        s = enrich.score(self.local(),
                         {"title": "Entirely Other", "artists": ["Alpha"],
                          "duration": 200.0})
        self.assertLess(s, enrich.REVIEW)

    def test_one_agreeing_name_in_a_long_credit_list_is_enough(self):
        row = self.local(artist="Joji, A, B, C, D, E, F")
        s = enrich.score(row, {"title": "Blue", "artists": ["Joji"],
                               "duration": 200.0})
        self.assertGreaterEqual(s, enrich.AUTO)


class ReleasePickTests(unittest.TestCase):
    def test_the_album_wins_over_the_compilation(self):
        rec = recording("r1", "Blue", ["Alpha"], album="Real Album",
                        date="2001-01-01", length=200)
        rec["releases"].append({
            "id": "rel-comp", "title": "Now That's What I Call 2011",
            "date": "2011-01-01",
            "release-group": {"primary-type": "Album",
                              "secondary-types": ["Compilation"]},
        })
        out = enrich.recording_fields(rec)
        self.assertEqual(out["release_fields"]["album"],"Real Album")

    def test_the_earliest_release_wins_between_equals(self):
        rec = recording("r1", "Blue", ["Alpha"], album="Reissue",
                        date="2015-01-01", length=200)
        rec["releases"].insert(0, {
            "id": "rel-first", "title": "Original", "date": "1999-01-01",
            "release-group": {"primary-type": "Album", "secondary-types": []},
            "artist-credit": [{"artist": {"id": "a-0", "name": "Alpha"}}],
        })
        out = enrich.recording_fields(rec)
        self.assertEqual(out["release_fields"]["album"], "Original")

    def test_the_soundtrack_is_not_preferred_to_the_album(self):
        rec = recording("r1", "Apocalypse", ["Cigarettes After Sex"],
                        album="Cigarettes After Sex", date="2017-06-09",
                        length=290)
        rec["releases"].insert(0, {
            "id": "rel-ost", "title": "Betty (HBO Original Series Soundtrack)",
            "date": "2020-05-15",
            "release-group": {"primary-type": "Album",
                              "secondary-types": ["Soundtrack"]},
        })
        out = enrich.recording_fields(rec)
        self.assertEqual(out["release_fields"]["album"],
                         "Cigarettes After Sex")

    def test_a_various_artists_anthology_loses_to_the_artists_own_album(self):
        # The search API often omits secondary-types, so the compilation
        # filter has nothing to see and this is the check that has to work.
        rec = recording("r1", "How to Save a Life", ["The Fray"],
                        album="How to Save a Life", date="2005-09-13",
                        length=262)
        rec["releases"].insert(0, {
            "id": "rel-va", "title": "Hot Party Summer 2007",
            "date": "2007-01-01", "track-count": 40,
            "release-group": {"primary-type": "Album"},
            "artist-credit": [{"artist": {"id": "va", "name": "Various Artists"}}],
        })
        out = enrich.recording_fields(rec)
        self.assertEqual(out["release_fields"]["album"], "How to Save a Life")

    def test_the_album_the_file_already_names_wins_outright(self):
        # The file's own tag is better evidence of which pressing this copy
        # came from than any ranking over forty releases.
        rec = recording("r1", "Blue", ["Alpha"], album="Deluxe Reissue",
                        date="1999-01-01", length=200)
        rec["releases"].append({
            "id": "rel-known", "title": "The One I Have",
            "date": "2005-01-01",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        })
        out = enrich.recording_fields(rec, prefer_album="the one i have")
        self.assertEqual(out["release_fields"]["album"], "The One I Have")


# ------------------------------------------------------------- the enricher

class EnrichTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test audio")
        self.tmp = tempfile.mkdtemp(prefix="lz-enrich-")
        self.lib = os.path.join(self.tmp, "library")
        self.con = db.connect(os.path.join(self.tmp, "lz.db"))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, rel, seconds=2.0, **tagvals):
        path = os.path.join(self.lib, rel)
        make_mp3(path, seconds=seconds, **tagvals)
        return path

    def row(self, like):
        return self.con.execute(
            "SELECT * FROM track WHERE rel_path LIKE ?", (f"%{like}%",)).fetchone()

    # -- backfill ---------------------------------------------------------

    def test_backfill_copies_an_isrc_onto_an_untagged_twin(self):
        self.add("Tagged/Album/Blue.mp3", seconds=2.0,
                 title="Blue", artist="Alpha", isrc="GBAAA0000001",
                 album="First")
        self.add("Downloads/Nemu/Singles/Blue.mp3", seconds=2.0,
                 title="Blue", artist="Nemu")
        scan.scan(self.con, self.lib)
        # The download's own artist is the channel, so the names only agree
        # via the tagged copy's title - which is what makes this a twin.
        self.con.execute("UPDATE track SET artist='Alpha' WHERE rel_path LIKE ?",
                         ("%Nemu%",))
        self.con.commit()

        counts = enrich.backfill_isrc(self.con)
        self.assertEqual(counts["matched"], 1)
        stored = self.con.execute(
            "SELECT fields FROM enrichment WHERE status='candidate'").fetchone()
        self.assertEqual(json.loads(stored["fields"])["isrc"], "GBAAA0000001")

    def test_backfill_declines_when_two_isrcs_claim_the_same_song(self):
        self.add("A/Album/Blue.mp3", title="Blue", artist="Alpha",
                 isrc="GBAAA0000001")
        self.add("B/Album/Blue.mp3", title="Blue", artist="Alpha",
                 isrc="GBAAA0000002")
        self.add("C/Album/Blue.mp3", title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        counts = enrich.backfill_isrc(self.con)
        self.assertEqual(counts["matched"], 0)
        self.assertEqual(counts["ambiguous"], 1)

    def test_backfill_is_free_of_side_effects_when_asked_to_be(self):
        self.add("A/Album/Blue.mp3", title="Blue", artist="Alpha",
                 isrc="GBAAA0000001")
        self.add("B/Album/Blue.mp3", title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        enrich.backfill_isrc(self.con, dry_run=True)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0], 0)

    # -- lookups ----------------------------------------------------------

    def test_an_isrc_hit_is_applied_without_a_search(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                 artist="Alpha", isrc="GBAAA0000001")
        scan.scan(self.con, self.lib)
        client = StubMB(by_isrc={"GBAAA0000001": [
            recording("r1", "Blue (Remastered)", ["Alpha"], album="First",
                      date="1999-05-01", length=2.0)]})
        status = enrich.enrich_track(self.con, self.row("Blue"), client)
        self.assertEqual(status, "applied")
        self.assertEqual(client.search_calls, [])
        self.assertEqual(self.row("Blue")["album"], "First")

    def test_an_isrc_whose_recording_is_the_wrong_length_is_not_trusted(self):
        # A file mis-tagged with somebody else's identifier. The database is
        # right and the tag is wrong, so the lookup must not be applied.
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                 artist="Alpha", isrc="GBAAA0000001")
        scan.scan(self.con, self.lib)
        client = StubMB(
            by_isrc={"GBAAA0000001": [recording("r1", "Something Else",
                                                ["Other"], length=400.0)]},
            search=[])
        status = enrich.enrich_track(self.con, self.row("Blue"), client)
        self.assertEqual(status, "none")

    def test_a_confident_search_match_is_applied(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="First",
                      date="1999-01-01", length=2.0, isrc="GBAAA0000009")])
        self.assertEqual(
            enrich.enrich_track(self.con, self.row("Blue"), client), "applied")
        row = self.row("Blue")
        self.assertEqual(row["album"], "First")
        self.assertEqual(row["isrc"], "GBAAA0000009")

    def test_a_doubtful_match_waits_for_a_person(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue Monday",
                 artist="Alpha")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[
            recording("r1", "Blue Sunday", ["Beta"], album="First",
                      date="1999-01-01", length=4.0)])
        status = enrich.enrich_track(self.con, self.row("Blue"), client)
        self.assertEqual(status, "candidate")
        # Nothing was written into the catalog while it waits.
        self.assertIsNone(self.row("Blue")["album"])
        self.assertEqual(len(enrich.review_queue(self.con)), 1)

    def test_a_channel_name_is_not_used_as_the_search_artist(self):
        # The Nemu case: the tag artist is the uploader, and the real artist
        # is the first half of the title.
        self.add("Nemu/Singles/x.mp3", seconds=2.0,
                 title="KIRINJI - Jikanga Nai", artist="Nemu")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[])
        enrich.enrich_track(self.con, self.row("x.mp3"), client)
        self.assertEqual(client.search_calls[0][0], "KIRINJI")

    def test_a_tag_artist_that_appears_in_the_title_is_kept(self):
        self.add("Alpha/Album/y.mp3", seconds=2.0, title="Alpha - Blue",
                 artist="Alpha")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[])
        enrich.enrich_track(self.con, self.row("y.mp3"), client)
        self.assertEqual(client.search_calls[0][0], "Alpha")

    def test_a_correct_album_is_not_overwritten_by_a_release_guess(self):
        # The regression the first live run produced: a certain recording
        # match carrying an uncertain release, relabelling a right album.
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                 artist="Alpha", album="The Right Album")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="Some Soundtrack",
                      date="2020-01-01", length=2.0, isrc="GBAAA0000009")])
        self.assertEqual(
            enrich.enrich_track(self.con, self.row("Blue"), client), "applied")
        row = self.row("Blue")
        self.assertEqual(row["album"], "The Right Album")
        # The recording's own fields are still taken: those are as certain
        # as the match itself.
        self.assertEqual(row["isrc"], "GBAAA0000009")

    def test_between_two_perfect_matches_the_artists_own_release_wins(self):
        # Both recordings match the file exactly, so the score cannot
        # choose. The anthology's copy came back first.
        anthology = recording("r-va", "Blue", ["Alpha"],
                              album="Hot Party Summer 2007", date="2007-01-01",
                              length=2.0)
        anthology["releases"][0]["artist-credit"] = [
            {"artist": {"id": "va", "name": "Various Artists"}}]
        own = recording("r-own", "Blue", ["Alpha"], album="First",
                        date="1999-01-01", length=2.0)

        self.add("A/Nowhere/Blue.mp3", seconds=2.0, title="Blue",
                 artist="Alpha")
        scan.scan(self.con, self.lib)
        enrich.enrich_track(self.con, self.row("Blue"),
                            StubMB(search=[anthology, own]))
        self.assertEqual(self.row("Blue")["album"], "First")

    def test_an_empty_album_is_filled_in(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="First",
                      date="1999-01-01", length=2.0)]))
        self.assertEqual(self.row("Blue")["album"], "First")

    def test_a_parenthetical_is_dropped_on_a_second_attempt(self):
        self.add("N/Singles/x.mp3", seconds=2.0,
                 title="KIRINJI - Jikanga Nai (Romanised)", artist="Nemu")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[])
        enrich.enrich_track(self.con, self.row("x.mp3"), client)
        tried = [call[1] for call in client.search_calls]
        self.assertIn("Jikanga Nai", tried)

    def test_an_ascii_title_costs_exactly_one_request(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        client = StubMB(search=[])
        enrich.enrich_track(self.con, self.row("Blue"), client)
        self.assertEqual(len(client.search_calls), 1)

    # -- decisions --------------------------------------------------------

    def test_accepting_a_candidate_applies_it(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue Monday",
                 artist="Alpha")
        scan.scan(self.con, self.lib)
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue Sunday", ["Beta"], album="First",
                      date="1999-01-01", length=4.0)]))
        key = self.row("Blue")["content_key"]
        self.assertTrue(enrich.accept(self.con, key))
        self.assertEqual(self.row("Blue")["album"], "First")

    def test_a_rejected_candidate_is_not_looked_up_again(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue Monday",
                 artist="Alpha")
        scan.scan(self.con, self.lib)
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue Sunday", ["Beta"], album="First",
                      length=4.0)]))
        enrich.reject(self.con, self.row("Blue")["content_key"])
        self.assertEqual(enrich.pending(self.con), [])
        self.assertEqual(enrich.review_queue(self.con), [])

    def test_a_hand_typed_value_outranks_the_source(self):
        self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue", artist="Alpha")
        scan.scan(self.con, self.lib)
        key = self.row("Blue")["content_key"]
        enrich.override(self.con, key, album="What I Say It Is")
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="What The Source Says",
                      date="1999-01-01", length=2.0)]))
        self.assertEqual(self.row("Blue")["album"], "What I Say It Is")

    def test_a_run_of_lookup_failures_stops_the_run(self):
        for i in range(6):
            self.add(f"A/Album/{i}.mp3", seconds=2.0, title=f"T{i}",
                     artist="Alpha")
        scan.scan(self.con, self.lib)

        class Broken(StubMB):
            def search(self, *a, **kw):
                raise enrich.LookupError_("MusicBrainz returned 503")

        counts = enrich.run(self.con, client=Broken())
        self.assertIn("503", counts["stopped"])
        self.assertEqual(counts["failed"], enrich.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(counts["applied"] + counts["candidates"]
                         + counts["unmatched"], 0)

    def test_one_failure_in_the_middle_does_not_end_the_run(self):
        # A single request timing out over a forty-minute run is weather.
        for i in range(4):
            self.add(f"A/Album/{i}.mp3", seconds=2.0, title=f"T{i}",
                     artist="Alpha")
        scan.scan(self.con, self.lib)

        class Flaky(StubMB):
            calls = 0

            def search(self, *a, **kw):
                Flaky.calls += 1
                if Flaky.calls == 2:
                    raise enrich.LookupError_("timed out")
                return []

        counts = enrich.run(self.con, client=Flaky())
        self.assertIsNone(counts["stopped"])
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["unmatched"], 3)

    def test_the_least_identified_tracks_are_looked_up_first(self):
        self.add("A/Album/rich.mp3", title="Rich", artist="Alpha",
                 album="First", isrc="GBAAA0000001")
        self.add("B/Album/poor.mp3", title="Poor")
        scan.scan(self.con, self.lib)
        first = enrich.pending(self.con)[0]
        self.assertIn("poor", first["rel_path"])

    # -- writing back -----------------------------------------------------

    def test_writing_tags_updates_the_file_and_the_content_key(self):
        path = self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                        artist="Alpha")
        scan.scan(self.con, self.lib)
        old_key = self.row("Blue")["content_key"]
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="First",
                      date="1999-01-01", length=2.0)]))
        self.assertTrue(enrich.write_back(self.con, old_key))

        from mutagen.mp3 import MP3
        self.assertEqual(str(MP3(path).tags["TALB"]), "First")
        # The catalog followed the file, so a rescan sees no change and the
        # device manifest still matches.
        new_key = self.row("Blue")["content_key"]
        self.assertNotEqual(new_key, old_key)
        counts = scan.scan(self.con, self.lib)
        self.assertEqual(counts["added"], 0)

    def test_the_enrichment_follows_the_file_across_a_rewrite(self):
        path = self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                        artist="Alpha")
        scan.scan(self.con, self.lib)
        key = self.row("Blue")["content_key"]
        enrich.enrich_track(self.con, self.row("Blue"), StubMB(search=[
            recording("r1", "Blue", ["Alpha"], album="First",
                      date="1999-01-01", length=2.0)]))
        enrich.write_back(self.con, key)
        scan.scan(self.con, self.lib)
        # Not offered for lookup again: the decision moved with the key.
        self.assertEqual(enrich.pending(self.con), [])

    def test_a_failed_write_leaves_the_original_file_alone(self):
        path = self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue",
                        artist="Alpha")
        with open(path, "rb") as fh:
            before = fh.read()
        with self.assertRaises(tags.TagWriteError):
            tags.write(path + ".missing", {"title": "X"})
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_writing_nothing_is_not_an_error(self):
        path = self.add("A/Album/Blue.mp3", seconds=2.0, title="Blue")
        self.assertIsNone(tags.write(path, {}))


if __name__ == "__main__":
    unittest.main()
