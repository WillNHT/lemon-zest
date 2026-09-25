"""API tests for the local server.

Uses Flask's test client, so no port is opened and no browser is involved.
"""
import os

# Automatic enrichment follows every scan and download. Switched off for the
# suite: these tests are about the catalog and the sync, and none of them
# should be making rate-limited calls to somebody else's service.
os.environ["LEMONZEST_AUTO_ENRICH"] = "0"

import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lemonzest import db, devices, playlists, scan  # noqa: E402
from lemonzest.server import create_app  # noqa: E402

from test_sync import FFMPEG, make_mp3  # noqa: E402


class ServerTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.skipTest("ffmpeg is needed to generate test audio")
        self.tmp = tempfile.mkdtemp(prefix="lz-srv-")
        self.lib = os.path.join(self.tmp, "library")
        self.card = os.path.join(self.tmp, "card")
        os.makedirs(self.card)

        self.files = []
        for i, (artist, title) in enumerate(
                [("Alpha", "One"), ("Alpha", "Two"), ("Beta", "Three")], 1):
            p = os.path.join(self.lib, artist, "Album", f"{i:02d} {title}.mp3")
            make_mp3(p, frames=40 + i, artist=artist, album="Album",
                     title=title, track=i)
            self.files.append(p)
        # An empty file: the "needs attention" case, straight from real data.
        empty = os.path.join(self.lib, "Alpha", "Album", "04 Broken.mp3")
        open(empty, "wb").close()

        self.db_path = os.path.join(self.tmp, "lemon-zest.db")
        con = db.connect(self.db_path)
        scan.scan(con, self.lib)

        pl_dir = os.path.join(self.tmp, "playlists")
        os.makedirs(pl_dir)
        rows = [(os.path.relpath(f, pl_dir).replace("\\", "/"), "t", 10, None)
                for f in self.files[:2]]
        playlists.write(os.path.join(pl_dir, "mix.m3u8"), "mix", rows)
        playlists.import_dir(con, pl_dir)
        devices.register(con, self.card, name="Card", profile="ums")
        con.close()

        self.app = create_app(self.db_path)
        self.c = self.app.test_client()

    def tearDown(self):
        from lemonzest import enrichq
        enrichq.reset_queues()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_and_assets_are_served(self):
        for path in ("/", "/app.css", "/app.js"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_stats(self):
        d = self.c.get("/api/stats").get_json()
        self.assertEqual(d["tracks"], 4)          # three real, one empty
        self.assertEqual(d["empty_files"], 1)
        self.assertEqual(d["devices"], 1)
        self.assertGreaterEqual(d["attention"], 1)

    def test_a_merged_version_leaves_the_library_list(self):
        from lemonzest import dedupe
        con = db.connect(self.db_path)
        keys = [r[0] for r in con.execute(
            "SELECT content_key FROM track WHERE title IN ('One', 'Two') "
            "ORDER BY title")]
        dedupe.merge(con, keys[0], [keys[1]])
        con.close()
        titles = [t["title"] for t in self.c.get("/api/library").get_json()["tracks"]]
        self.assertIn("One", titles)
        self.assertNotIn("Two", titles)
        rows = self.c.get("/api/library?versions=1").get_json()["tracks"]
        self.assertEqual({t["title"]: t["versions"] for t in rows
                          if t["title"] in ("One", "Two")}, {"One": 2, "Two": 2})

    def test_library_sorts_untagged_last(self):
        d = self.c.get("/api/library").get_json()
        self.assertEqual(d["total"], 4)
        self.assertIsNotNone(d["tracks"][0]["title"])
        self.assertTrue(d["tracks"][-1]["empty"])

    def test_library_filters(self):
        d = self.c.get("/api/library?artist=Beta").get_json()
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["tracks"][0]["artist"], "Beta")
        d = self.c.get("/api/library?q=Three").get_json()
        self.assertEqual(d["total"], 1)

    def test_facets_do_not_filter_themselves(self):
        """Picking an artist must leave the other artists visible."""
        d = self.c.get("/api/facets?artist=Alpha").get_json()
        names = [x["value"] for x in d["artist"]]
        self.assertIn("Beta", names)
        albums = [x["value"] for x in d["album"]]
        self.assertEqual(albums, ["Album"])

    # ------------------------------------------------------------ inbox

    def test_inbox_holds_everything_scanned_until_it_is_emptied(self):
        d = self.c.get("/api/library?new=1&order=added").get_json()
        self.assertEqual(d["total"], 4)
        self.assertEqual(self.c.get("/api/stats").get_json()["inbox"], 4)

        self.assertEqual(self.c.post("/api/inbox/seen").status_code, 200)
        self.assertEqual(
            self.c.get("/api/library?new=1").get_json()["total"], 0)
        self.assertEqual(self.c.get("/api/stats").get_json()["inbox"], 0)
        # Emptying the inbox is a watermark, not a deletion.
        self.assertEqual(self.c.get("/api/library").get_json()["total"], 4)

    def test_a_file_added_after_the_inbox_was_emptied_is_new_again(self):
        self.c.post("/api/inbox/seen")
        make_mp3(os.path.join(self.lib, "Beta", "Album", "05 Later.mp3"),
                 frames=44, artist="Beta", album="Album", title="Later",
                 track=5)
        con = db.connect(self.db_path)
        scan.scan(con, self.lib)
        con.close()

        d = self.c.get("/api/library?new=1&order=added").get_json()
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["tracks"][0]["title"], "Later")

    def test_rescanning_an_unchanged_library_adds_nothing_to_the_inbox(self):
        self.c.post("/api/inbox/seen")
        con = db.connect(self.db_path)
        scan.scan(con, self.lib)
        con.close()
        self.assertEqual(
            self.c.get("/api/library?new=1").get_json()["total"], 0)

    # --------------------------------------------------- library folders

    def test_roots_report_what_is_in_them(self):
        rows = self.c.get("/api/roots").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tracks"], 4)
        self.assertFalse(rows[0]["hidden"])

    def test_hiding_a_folder_takes_it_out_of_every_view(self):
        root = self.c.get("/api/roots").get_json()[0]["root"]
        self.c.post("/api/roots/hide", json={"root": root, "hidden": True})

        self.assertEqual(self.c.get("/api/library").get_json()["total"], 0)
        self.assertEqual(self.c.get("/api/library?new=1").get_json()["total"], 0)
        self.assertEqual(self.c.get("/api/facets").get_json()["artist"], [])
        stats = self.c.get("/api/stats").get_json()
        self.assertEqual(stats["tracks"], 0)
        self.assertNotIn(root, stats["roots"])
        self.assertTrue(stats["root_detail"][0]["hidden"])

        # Hiding forgets nothing: unhiding brings the library straight back.
        self.c.post("/api/roots/hide", json={"root": root, "hidden": False})
        self.assertEqual(self.c.get("/api/library").get_json()["total"], 4)

    def test_removing_a_folder_forgets_rows_and_keeps_the_files(self):
        root = self.c.get("/api/roots").get_json()[0]["root"]
        out = self.c.post("/api/roots/remove", json={"root": root}).get_json()
        self.assertEqual(out["tracks_removed"], 4)
        self.assertEqual(out["roots"], [])
        self.assertEqual(self.c.get("/api/library").get_json()["total"], 0)
        for f in self.files:
            self.assertTrue(os.path.exists(f), f)

    def test_removing_a_folder_needs_one(self):
        self.assertEqual(
            self.c.post("/api/roots/remove", json={}).status_code, 400)

    # --------------------------------------------- artwork and searching

    def test_artwork_is_a_404_when_the_file_carries_none(self):
        key = self.c.get("/api/library").get_json()["tracks"][0]["content_key"]
        self.assertEqual(self.c.get("/api/art/" + key).status_code, 404)
        self.assertEqual(self.c.get("/api/art/nosuchkey").status_code, 404)

    def test_track_detail_says_what_a_lookup_would_search_for(self):
        track = self.c.get("/api/library?q=Three").get_json()["tracks"][0]
        d = self.c.get("/api/enrich/track/" + track["content_key"]).get_json()
        self.assertEqual(d["search"]["title"], "Three")
        self.assertEqual(d["search"]["artist"], "Beta")

    def test_typed_search_terms_are_refused_over_a_selection(self):
        keys = [t["content_key"]
                for t in self.c.get("/api/library").get_json()["tracks"][:2]]
        r = self.c.post("/api/enrich/run",
                        json={"content_keys": keys,
                              "query": {"title": "Something"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("one track at a time", r.get_json()["error"])

    def test_a_typed_search_needs_a_title(self):
        key = self.c.get("/api/library").get_json()["tracks"][0]["content_key"]
        r = self.c.post("/api/enrich/run",
                        json={"content_keys": [key],
                              "query": {"artist": "Alpha", "title": "  "}})
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------ sorting

    def test_a_column_sorts_both_ways(self):
        up = self.c.get("/api/library?sort=title&dir=asc").get_json()["tracks"]
        down = self.c.get("/api/library?sort=title&dir=desc").get_json()["tracks"]
        titles = [t["title"] for t in up if t["title"]]
        self.assertEqual(titles, sorted(titles))
        self.assertEqual([t["title"] for t in down if t["title"]],
                         list(reversed(titles)))

    def test_untagged_rows_sort_last_whichever_way_it_is_asked(self):
        """SQLite puts NULLs first; a screen of blanks is not a sort."""
        for direction in ("asc", "desc"):
            tracks = self.c.get(
                "/api/library?sort=artist&dir=" + direction).get_json()["tracks"]
            self.assertIsNotNone(tracks[0]["artist"], direction)
            self.assertIsNone(tracks[-1]["artist"], direction)

    def test_the_last_edited_track_sorts_first_by_updated(self):
        key = self.c.get("/api/library?q=Two").get_json()["tracks"][0][
            "content_key"]
        time.sleep(0.05)
        self.c.post("/api/enrich/edit", json={"content_keys": [key],
                                              "fields": {"album": "Other"}})
        tracks = self.c.get(
            "/api/library?sort=updated&dir=desc").get_json()["tracks"]
        self.assertEqual(tracks[0]["content_key"], key)
        self.assertGreater(tracks[0]["updated_at"], tracks[0]["added_at"])

    def test_a_column_nobody_defined_is_ignored_rather_than_run(self):
        r = self.c.get("/api/library?sort=t.rel_path);DROP+TABLE+track;--")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["total"], 4)
        self.assertEqual(self.c.get("/api/stats").get_json()["tracks"], 4)

    # ----------------------------------------------------------- playlists

    def test_tracks_in_no_playlist_and_an_ignored_catch_all(self):
        # "mix" holds two of the four: the other two are in no playlist.
        d = self.c.get("/api/library?unlisted=1").get_json()
        self.assertEqual(d["total"], 2)
        self.assertEqual(self.c.get("/api/stats").get_json()["unlisted"], 2)
        # A playlist set aside stops counting, as a DAP-master one would.
        out = self.c.post("/api/unlisted", json={"ignore": ["mix"]}).get_json()
        self.assertEqual(out["ignore"], ["mix"])
        d = self.c.get("/api/library?unlisted=1").get_json()
        self.assertEqual(d["total"], 4)
        self.assertEqual(self.c.get("/api/unlisted").get_json()["ignore"],
                         ["mix"])

    def test_a_download_cut_off_by_closing_is_offered_back(self):
        from lemonzest import download
        con = db.connect(self.db_path)
        download.save_paused(con, {"id": "gone", "label": "big list",
                                   "urls": ["https://example.test/list"],
                                   "remaining": ["https://example.test/list"],
                                   "state": "running", "at": time.time()})
        con.close()
        rows = self.c.get("/api/download/config").get_json()["paused"]
        # Marked running, but no job is: the program was closed mid-run.
        self.assertEqual([(r["id"], r["state"]) for r in rows],
                         [("gone", "interrupted")])
        self.c.delete("/api/download/paused/gone")
        self.assertEqual(
            self.c.get("/api/download/config").get_json()["paused"], [])
        self.assertEqual(self.c.post("/api/jobs/nojob/pause").status_code, 404)

    def test_a_playlist_narrows_the_same_library_query(self):
        pid = self.c.get("/api/playlists").get_json()[0]["id"]
        d = self.c.get("/api/library?playlist=%d" % pid).get_json()
        self.assertEqual(d["total"], 2)
        # In the playlist's own order, and numbered by it.
        self.assertEqual([t["playlist_pos"] for t in d["tracks"]], [0, 1])
        titles = [t["title"] for t in d["tracks"]]
        self.assertEqual(titles, ["One", "Two"])

    def test_a_playlist_can_be_sorted_against_its_own_order(self):
        pid = self.c.get("/api/playlists").get_json()[0]["id"]
        d = self.c.get("/api/library?playlist=%d&sort=pos&dir=desc"
                       % pid).get_json()
        self.assertEqual([t["playlist_pos"] for t in d["tracks"]], [1, 0])

    def test_the_facets_narrow_with_the_playlist(self):
        pid = self.c.get("/api/playlists").get_json()[0]["id"]
        d = self.c.get("/api/facets?playlist=%d" % pid).get_json()
        self.assertEqual([a["value"] for a in d["artist"]], ["Alpha"])

    # ------------------------------------------------------------ decades

    def test_the_decade_facet_groups_by_the_first_three_digits(self):
        c = db.connect(self.db_path)
        for year, title in (("2012-09-03", "One"), ("2019", "Two"),
                            ("2001-11", "Three")):
            c.execute("UPDATE track SET year = ? WHERE title = ?", (year, title))
        c.commit()
        c.close()
        got = {f["value"]: f["count"]
               for f in self.c.get("/api/facets").get_json()["decade"]}
        self.assertEqual(got, {"2000s": 1, "2010s": 2})

    def test_a_decade_narrows_the_table(self):
        c = db.connect(self.db_path)
        c.execute("UPDATE track SET year = '1999-05-01' WHERE title = 'One'")
        c.commit()
        c.close()
        d = self.c.get("/api/library?decade=1990s").get_json()
        self.assertEqual([t["title"] for t in d["tracks"]], ["One"])

    # -------------------------------------------------- what is new to you

    def add_to_playlist(self):
        """Put a track into the playlist as a download would."""
        c = db.connect(self.db_path)
        tid = c.execute("SELECT id FROM track WHERE title = 'Three'"
                        ).fetchone()["id"]
        playlists.append_tracks(c, "mix", [tid], origin="download")
        c.close()

    def playlist_id(self):
        return self.c.get("/api/playlists").get_json()[0]["id"]

    def fresh(self):
        return self.c.get("/api/playlists").get_json()[0]["fresh"]

    def test_a_playlist_nobody_has_opened_is_all_new(self):
        """It has just appeared in the library; none of it has been seen."""
        self.assertEqual(self.fresh(), 2)

    def test_looking_at_it_clears_the_mark(self):
        pid = self.playlist_id()
        d = self.c.get("/api/playlists/%d" % pid).get_json()
        self.assertEqual(d["fresh"], 2)
        self.assertEqual(self.fresh(), 0)

    def test_what_arrives_afterwards_is_marked_again(self):
        pid = self.playlist_id()
        self.c.get("/api/playlists/%d" % pid)          # seen
        self.add_to_playlist()
        self.assertEqual(self.fresh(), 1)
        d = self.c.get("/api/playlists/%d" % pid).get_json()
        # Named while you are looking at it...
        self.assertEqual(d["fresh"], 1)
        arrived = [e["title"] for e in d["entries"]
                   if (e["added_at"] or 0) > d["since"]]
        self.assertEqual(arrived, ["Three"])
        # ...and ordinary by the time you come back.
        self.assertEqual(self.fresh(), 0)

    def test_a_peek_does_not_count_as_looking(self):
        """A reload under a page you are already on must not read the marks."""
        pid = self.playlist_id()
        self.c.get("/api/playlists/%d?peek=1" % pid)
        self.assertEqual(self.fresh(), 2)

    def test_the_table_says_when_each_track_joined(self):
        pid = self.playlist_id()
        self.c.get("/api/playlists/%d" % pid)
        self.add_to_playlist()
        rows = self.c.get("/api/library?playlist=%d" % pid).get_json()["tracks"]
        joined = {t["title"]: t["playlist_added_at"] for t in rows}
        self.assertEqual(len(joined), 3)
        self.assertGreater(joined["Three"], joined["One"])

    def test_re_importing_does_not_make_everything_new_again(self):
        """A scan re-reads the folder's playlists; that is not a change."""
        pid = self.playlist_id()
        self.c.get("/api/playlists/%d" % pid)
        c = db.connect(self.db_path)
        playlists.import_dir(c, os.path.join(self.tmp, "playlists"))
        c.close()
        self.assertEqual(self.fresh(), 0)

    # ---------------------------------------------------- the enrich queue

    def test_the_queue_says_what_it_is_doing(self):
        d = self.c.get("/api/enrich/queue").get_json()
        for key in ("waiting", "current", "paused", "done", "applied",
                    "failed", "recent"):
            self.assertIn(key, d)

    def test_the_queue_can_be_paused_and_resumed(self):
        d = self.c.post("/api/enrich/queue", json={"pause": True}).get_json()
        self.assertTrue(d["paused"])
        d = self.c.post("/api/enrich/queue", json={"resume": True}).get_json()
        self.assertFalse(d["paused"])

    def test_the_backlog_can_be_dropped_from_the_page(self):
        from lemonzest import enrichq
        q = enrichq.get_queue(self.db_path)
        q.pause()
        q.submit(["nothing-real"], priority="sweep")
        d = self.c.post("/api/enrich/queue", json={"clear": True}).get_json()
        self.assertEqual(d["dropped"], 1)
        self.assertEqual(d["waiting"], 0)
        self.c.post("/api/enrich/queue", json={"resume": True})

    def test_the_stats_carry_the_backlog(self):
        d = self.c.get("/api/stats").get_json()
        self.assertIn("enrich_queue", d)
        self.assertIn("waiting", d["enrich_queue"])

    # -------------------------------------------------------- the sync list

    def test_a_set_can_be_prepared_with_no_device_in_sight(self):
        r = self.c.post("/api/sync-list", json={"add": [["playlist", "mix"]]})
        d = r.get_json()
        self.assertEqual(d["tracks"], 2)
        self.assertGreater(d["bytes"], 0)
        self.assertEqual(d["rules"][0]["tracks"], 2)

    def test_tracks_go_on_the_list_by_content_key(self):
        keys = [t["content_key"] for t in
                self.c.get("/api/library").get_json()["tracks"][:2]]
        d = self.c.post("/api/sync-list/keys",
                        json={"content_keys": keys}).get_json()
        self.assertEqual(d["added"], 2)
        self.assertEqual({r["kind"] for r in d["rules"]}, {"track"})

    def test_the_same_rule_twice_is_still_one_rule(self):
        for _ in range(2):
            d = self.c.post("/api/sync-list",
                            json={"add": [["playlist", "mix"]]}).get_json()
        self.assertEqual(len(d["rules"]), 1)

    def test_a_rule_can_be_dropped_and_the_list_emptied(self):
        self.c.post("/api/sync-list", json={"add": [["playlist", "mix"],
                                                    ["artist", "Beta"]]})
        d = self.c.post("/api/sync-list",
                        json={"remove": [["artist", "Beta"]]}).get_json()
        self.assertEqual([r["ref"] for r in d["rules"]], ["mix"])
        d = self.c.post("/api/sync-list", json={"clear": True}).get_json()
        self.assertEqual(d["rules"], [])

    def test_applying_copies_the_rules_and_keeps_the_list(self):
        did = self.c.get("/api/devices").get_json()[0]["id"]
        self.c.post("/api/sync-list", json={"add": [["playlist", "mix"]]})
        out = self.c.post("/api/sync-list/apply", json={"device": did}).get_json()
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["tracks"], 2)
        rules = self.c.get("/api/devices").get_json()[0]["rules"]
        self.assertIn({"kind": "playlist", "ref": "mix"}, rules)
        # The list is not consumed: the same set usually goes on two cards.
        self.assertEqual(len(self.c.get("/api/sync-list").get_json()["rules"]), 1)

    def test_an_unknown_rule_kind_is_refused(self):
        r = self.c.post("/api/sync-list", json={"add": [["everything", "x"]]})
        self.assertEqual(r.status_code, 400)

    def test_applying_an_empty_list_says_so(self):
        did = self.c.get("/api/devices").get_json()[0]["id"]
        r = self.c.post("/api/sync-list/apply", json={"device": did})
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------ playlist refresh

    def test_a_playlist_with_no_source_cannot_be_fetched(self):
        pid = self.c.get("/api/playlists").get_json()[0]["id"]
        r = self.c.post("/api/playlists/%d/refresh" % pid)
        self.assertEqual(r.status_code, 400)
        self.assertIn("YouTube", r.get_json()["error"])

    def test_a_playlist_that_is_not_there_is_a_404(self):
        r = self.c.post("/api/playlists/9999/refresh")
        self.assertEqual(r.status_code, 404)

    # --------------------------------------------------------- the inbox

    def test_a_settled_file_leaves_the_inbox_on_its_own(self):
        c = db.connect(self.db_path)
        keys = [r["content_key"] for r in
                c.execute("SELECT content_key FROM track WHERE size > 0")]
        for key in keys[:2]:
            c.execute("INSERT INTO enrichment(content_key, status, source, "
                      "confidence, fields, fetched_at) "
                      "VALUES (?,'applied','isrc',1.0,'{}',0)", (key,))
        c.commit()
        self.assertEqual(db.promote_inbox(c), 2)
        c.close()
        self.assertEqual(self.c.get("/api/stats").get_json()["inbox"], 2)

    def test_a_lookup_that_found_nothing_is_settled_too(self):
        """Nothing further will happen on its own, so it is not still new."""
        c = db.connect(self.db_path)
        key = c.execute("SELECT content_key FROM track WHERE size > 0"
                        ).fetchone()["content_key"]
        c.execute("INSERT INTO enrichment(content_key, status, source, "
                  "confidence, fields, fetched_at) "
                  "VALUES (?,'none','musicbrainz',0.0,'{}',0)", (key,))
        c.commit()
        self.assertEqual(db.promote_inbox(c), 1)
        c.close()

    def test_a_file_waiting_on_a_decision_stays(self):
        c = db.connect(self.db_path)
        key = c.execute("SELECT content_key FROM track WHERE size > 0"
                        ).fetchone()["content_key"]
        c.execute("INSERT INTO enrichment(content_key, status, source, "
                  "confidence, fields, fetched_at) "
                  "VALUES (?,'candidate','musicbrainz',0.7,'{}',0)", (key,))
        c.commit()
        self.assertEqual(db.promote_inbox(c), 0)
        c.close()

    def test_an_old_file_leaves_however_it_ended_up(self):
        """No network, no key, no match - it still must not sit there."""
        c = db.connect(self.db_path)
        c.execute("UPDATE track SET added_at = ?", (time.time() - 48 * 3600,))
        c.commit()
        self.assertEqual(db.promote_inbox(c), 4)
        c.close()
        self.assertEqual(self.c.get("/api/stats").get_json()["inbox"], 0)

    def test_an_empty_file_is_not_promoted_for_being_identified(self):
        c = db.connect(self.db_path)
        key = c.execute("SELECT content_key FROM track WHERE size = 0"
                        ).fetchone()["content_key"]
        c.execute("INSERT INTO enrichment(content_key, status, source, "
                  "confidence, fields, fetched_at) "
                  "VALUES (?,'applied','isrc',1.0,'{}',0)", (key,))
        c.commit()
        self.assertEqual(db.promote_inbox(c), 0)
        c.close()

    def test_clearing_by_hand_empties_the_rest(self):
        before = self.c.get("/api/stats").get_json()["inbox"]
        self.assertEqual(before, 4)
        got = self.c.post("/api/inbox/seen").get_json()
        self.assertEqual(got["promoted"], 4)
        self.assertEqual(self.c.get("/api/stats").get_json()["inbox"], 0)

    def test_problems_lists_the_empty_file(self):
        d = self.c.get("/api/problems").get_json()
        self.assertEqual(d["empty_total"], 1)
        self.assertTrue(d["empty"][0]["rel_path"].endswith("Broken.mp3"))

    def test_set_then_plan_then_sync(self):
        dev = self.c.get("/api/devices").get_json()[0]
        r = self.c.post(f"/api/devices/{dev['id']}/set",
                        json={"add": [["playlist", "mix"]]}).get_json()
        self.assertEqual(r["tracks"], 2)

        plan = self.c.get(f"/api/devices/{dev['id']}/plan").get_json()
        self.assertEqual(plan["copies_total"], 2)
        self.assertTrue(plan["space"]["fits"])

        job = self.c.post(f"/api/devices/{dev['id']}/sync", json={}).get_json()
        state = self._await_job(job["job"])
        self.assertEqual(state["state"], "done", state.get("error"))
        self.assertEqual(state["result"]["copied"], 2)

        after = self.c.get(f"/api/devices/{dev['id']}/plan").get_json()
        self.assertEqual(after["copies_total"], 0)
        self.assertEqual(after["unchanged"], 2)

    def test_plan_reports_a_device_that_is_not_mounted(self):
        dev = self.c.get("/api/devices").get_json()[0]
        shutil.rmtree(self.card)
        r = self.c.get(f"/api/devices/{dev['id']}/plan")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["not_mounted"])

    def test_scan_job(self):
        job = self.c.post("/api/scan", json={"root": self.lib}).get_json()
        state = self._await_job(job["job"])
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["result"]["seen"], 4)

    def test_download_config_reports_what_it_would_use(self):
        d = self.c.get("/api/download/config").get_json()
        self.assertIn("cookies_mode", d["config"])
        self.assertIn(d["cookies"]["source"], ("firefox", "file", "none"))
        self.assertIn(self.lib.replace("\\", "/"),
                      [r.replace("\\", "/") for r in d["roots"]])

        saved = self.c.post("/api/download/config",
                            json={"cookies_mode": "none",
                                  "audio_format": "opus"}).get_json()
        self.assertEqual(saved["config"]["cookies_mode"], "none")
        self.assertEqual(saved["config"]["audio_format"], "opus")
        self.assertEqual(saved["cookies"]["source"], "none")

    def test_a_download_without_a_url_is_rejected(self):
        r = self.c.post("/api/download", json={"urls": []})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/download/probe", json={"url": ""})
        self.assertEqual(r.status_code, 400)

    def test_bad_paths_are_rejected(self):
        r = self.c.post("/api/scan", json={"root": "/no/such/place"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/devices", json={"root": "/no/such/place"})
        self.assertEqual(r.status_code, 400)

    def _await_job(self, job_id, timeout=30):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.c.get("/api/jobs/" + job_id).get_json()
            if state["state"] != "running":
                return state
            time.sleep(0.05)
        self.fail("job did not finish in time")


if __name__ == "__main__":
    unittest.main(verbosity=2)
