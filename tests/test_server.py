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
