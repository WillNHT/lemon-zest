"""Local web server: JSON API over the core, plus the single-page UI.

The core does the work; this is a thin translation layer. Anything that
takes longer than a click - a scan, a sync - runs on a worker thread and is
reported through the job registry, so the page stays responsive and a sync
survives a page reload.

SQLite connections are not shared across threads: every worker opens its
own. The catalog is in WAL mode, so a sync writing its manifest does not
block the UI reading the library.
"""
import os
import threading
import time
import traceback
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import db as db_mod
from . import devices as dev_mod
from . import executor, planner, playlists, scan

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# job id -> progress record. Small and bounded; finished jobs are kept so a
# reloaded page can still show the outcome.
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50


def _new_job(kind, label):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        if len(JOBS) >= MAX_JOBS:
            for k in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:10]:
                if JOBS[k]["state"] in ("done", "failed"):
                    JOBS.pop(k, None)
        JOBS[job_id] = {"id": job_id, "kind": kind, "label": label,
                        "state": "running", "done": 0, "total": 0,
                        "detail": "", "started": time.time(),
                        "finished": None, "result": None, "error": None,
                        "events": []}
    return job_id


def _update(job_id, **kw):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(kw)


def _push_event(job_id, kind, text):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["events"].append({"kind": kind, "text": text, "at": time.time()})
            del job["events"][:-200]


def create_app(db_path=None):
    app = Flask(__name__, static_folder=None)
    app.config["DB_PATH"] = db_path or db_mod.default_db_path()

    def con():
        """A connection for the request thread."""
        return db_mod.connect(app.config["DB_PATH"])

    # ---------------------------------------------------------- static

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/<path:name>")
    def static_file(name):
        return send_from_directory(WEB_DIR, name)

    # ---------------------------------------------------------- library

    @app.get("/api/stats")
    def stats():
        c = con()
        one = lambda q: c.execute(q).fetchone()[0]
        return jsonify({
            "tracks": one("SELECT COUNT(*) FROM track"),
            "bytes": one("SELECT COALESCE(SUM(size),0) FROM track"),
            "artists": one("SELECT COUNT(DISTINCT artist) FROM track"),
            "albums": one("SELECT COUNT(DISTINCT album) FROM track"),
            "with_isrc": one("SELECT COUNT(*) FROM track WHERE isrc IS NOT NULL"),
            "playlists": one("SELECT COUNT(*) FROM playlist"),
            "playlist_entries": one("SELECT COUNT(*) FROM playlist_entry"),
            "unmatched": one("SELECT COUNT(*) FROM playlist_entry "
                             "WHERE track_id IS NULL"),
            "devices": one("SELECT COUNT(*) FROM device"),
            "empty_files": one("SELECT COUNT(*) FROM track WHERE size = 0"),
            "attention": (
                one("SELECT COUNT(*) FROM playlist_entry WHERE track_id IS NULL")
                + one("SELECT COUNT(*) FROM track WHERE size = 0")
                + one("SELECT COUNT(*) FROM track WHERE (title IS NULL OR "
                      "title = '') AND size > 0")),
            "roots": [r["root"] for r in
                      c.execute("SELECT DISTINCT root FROM track ORDER BY root")],
        })

    def _library_where(args):
        """Build the WHERE clause shared by the track list and the facets."""
        clauses, params = [], []
        q = (args.get("q") or "").strip()
        if q:
            clauses.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
            params += [f"%{q}%"] * 3
        for field, key in (("genre", "genre"), ("artist", "artist"),
                           ("album", "album")):
            val = args.get(key)
            if val:
                if field == "artist":
                    clauses.append("(t.artist = ? COLLATE NOCASE "
                                   "OR t.album_artist = ? COLLATE NOCASE)")
                    params += [val, val]
                else:
                    clauses.append(f"t.{field} = ? COLLATE NOCASE")
                    params.append(val)
        return (" AND ".join(clauses) or "1=1"), params

    @app.get("/api/library")
    def library():
        c = con()
        where, params = _library_where(request.args)
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        device_id = request.args.get("device")

        total = c.execute(
            f"SELECT COUNT(*) FROM track t WHERE {where}", params
        ).fetchone()[0]
        # Untagged files sort last rather than first: SQLite puts NULLs at the
        # top, which would fill the first screen with the least useful rows.
        rows = c.execute(
            f"SELECT t.* FROM track t WHERE {where} "
            "ORDER BY (t.album_artist IS NULL), t.album_artist, t.album, "
            "t.disc_no, t.track_no, t.title, t.rel_path "
            "LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()

        # Which of these are already on the selected device?
        on_device = set()
        if device_id:
            on_device = {
                r["track_id"] for r in c.execute(
                    "SELECT track_id FROM device_manifest WHERE device_id=?",
                    (device_id,))
                if r["track_id"] is not None
            }

        return jsonify({
            "total": total, "offset": offset, "limit": limit,
            "tracks": [{
                "id": r["id"], "title": r["title"], "artist": r["artist"],
                "album": r["album"], "album_artist": r["album_artist"],
                "track_no": r["track_no"], "duration": r["duration"],
                "size": r["size"], "ext": r["ext"], "genre": r["genre"],
                "bitrate": r["bitrate"], "sample_rate": r["sample_rate"],
                "isrc": r["isrc"], "purl": r["purl"], "path": r["path"],
                "rel_path": r["rel_path"],
                "empty": r["size"] == 0,
                "untagged": not r["title"],
                "on_device": r["id"] in on_device,
            } for r in rows],
        })

    @app.get("/api/problems")
    def problems():
        """Everything worth a second look, in one place."""
        c = con()
        empty = c.execute(
            "SELECT id, path, rel_path FROM track WHERE size = 0 "
            "ORDER BY rel_path LIMIT 500").fetchall()
        untagged = c.execute(
            "SELECT id, path, rel_path FROM track "
            "WHERE (title IS NULL OR title = '') AND size > 0 "
            "ORDER BY rel_path LIMIT 500").fetchall()
        unmatched = c.execute(
            "SELECT p.id pid, p.name, e.pos, e.title_hint, e.raw_path "
            "FROM playlist_entry e JOIN playlist p ON p.id = e.playlist_id "
            "WHERE e.track_id IS NULL ORDER BY p.name, e.pos LIMIT 500"
        ).fetchall()
        return jsonify({
            "empty": [dict(r) for r in empty],
            "empty_total": c.execute(
                "SELECT COUNT(*) FROM track WHERE size = 0").fetchone()[0],
            "untagged": [dict(r) for r in untagged],
            "untagged_total": c.execute(
                "SELECT COUNT(*) FROM track WHERE (title IS NULL OR title = '') "
                "AND size > 0").fetchone()[0],
            "unmatched": [dict(r) for r in unmatched],
            "unmatched_total": c.execute(
                "SELECT COUNT(*) FROM playlist_entry "
                "WHERE track_id IS NULL").fetchone()[0],
        })

    @app.get("/api/facets")
    def facets():
        """Counts for the three-pane column browser."""
        c = con()
        out = {}
        for field in ("genre", "artist", "album"):
            args = dict(request.args)
            # A pane does not filter itself, so its own options stay visible.
            args.pop(field, None)
            where, params = _library_where(args)
            col = ("COALESCE(t.album_artist, t.artist)" if field == "artist"
                   else f"t.{field}")
            rows = c.execute(
                f"SELECT {col} AS v, COUNT(*) n FROM track t WHERE {where} "
                f"AND {col} IS NOT NULL AND {col} <> '' "
                "GROUP BY v COLLATE NOCASE ORDER BY v COLLATE NOCASE",
                params
            ).fetchall()
            out[field] = [{"value": r["v"], "count": r["n"]} for r in rows]
        return jsonify(out)

    # -------------------------------------------------------- playlists

    @app.get("/api/playlists")
    def playlist_list():
        c = con()
        rows = c.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM playlist_entry e WHERE e.playlist_id=p.id) n, "
            "(SELECT COUNT(*) FROM playlist_entry e WHERE e.playlist_id=p.id "
            " AND e.track_id IS NULL) unmatched "
            "FROM playlist p ORDER BY n DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/playlists/<int:pid>")
    def playlist_detail(pid):
        c = con()
        pl = c.execute("SELECT * FROM playlist WHERE id=?", (pid,)).fetchone()
        if not pl:
            return jsonify({"error": "no such playlist"}), 404
        entries = c.execute(
            "SELECT e.pos, e.title_hint, e.duration, e.source_uri, e.raw_path, "
            "t.id track_id, t.title, t.artist, t.album, t.ext, t.size, t.bitrate "
            "FROM playlist_entry e LEFT JOIN track t ON t.id=e.track_id "
            "WHERE e.playlist_id=? ORDER BY e.pos", (pid,)
        ).fetchall()
        devices = c.execute(
            "SELECT d.id, d.name, d.label FROM device d "
            "JOIN device_set s ON s.device_id=d.id "
            "WHERE s.kind='playlist' AND s.ref=?", (pl["name"],)
        ).fetchall()
        return jsonify({"playlist": dict(pl),
                        "entries": [dict(e) for e in entries],
                        "devices": [dict(d) for d in devices]})

    @app.post("/api/playlists/import")
    def playlist_import():
        directory = (request.json or {}).get("directory", "").strip()
        if not directory or not os.path.isdir(directory):
            return jsonify({"error": "not a directory: " + directory}), 400
        results = playlists.import_dir(con(), directory,
                                       recursive=bool((request.json or {})
                                                      .get("recursive")))
        return jsonify({"results": results})

    # ----------------------------------------------------------- scan

    @app.post("/api/scan")
    def start_scan():
        root = (request.json or {}).get("root", "").strip()
        if not root or not os.path.isdir(root):
            return jsonify({"error": "not a directory: " + root}), 400
        job_id = _new_job("scan", root)

        def work():
            try:
                c = db_mod.connect(app.config["DB_PATH"])
                def cb(done, total):
                    _update(job_id, done=done, total=total,
                            detail=f"{done:,} of {total:,} files")
                counts = scan.scan(c, root, progress=cb)
                _update(job_id, state="done", result=counts,
                        finished=time.time())
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job_id})

    # --------------------------------------------------------- devices

    @app.get("/api/devices")
    def device_list():
        c = con()
        out = []
        for d in c.execute("SELECT * FROM device ORDER BY name"):
            row = dict(d)
            row["mounted_at"] = dev_mod.locate(c, d)
            row["files"] = c.execute(
                "SELECT COUNT(*) FROM device_manifest WHERE device_id=?",
                (d["id"],)).fetchone()[0]
            row["bytes"] = c.execute(
                "SELECT COALESCE(SUM(size),0) FROM device_manifest "
                "WHERE device_id=?", (d["id"],)).fetchone()[0]
            row["rules"] = [dict(r) for r in c.execute(
                "SELECT kind, ref FROM device_set WHERE device_id=? "
                "ORDER BY kind, ref", (d["id"],))]
            if row["mounted_at"]:
                try:
                    row["space"] = dev_mod.free_space(row["mounted_at"])
                except Exception:
                    row["space"] = None
            out.append(row)
        return jsonify(out)

    @app.get("/api/volumes")
    def volumes():
        show_all = request.args.get("all") == "1"
        return jsonify(dev_mod.list_volumes(removable_only=not show_all))

    @app.get("/api/profiles")
    def profiles():
        return jsonify([dict(spec, key=k) for k, spec in dev_mod.PROFILES.items()])

    @app.post("/api/devices")
    def device_add():
        body = request.json or {}
        root = (body.get("root") or "").strip()
        if not root or not os.path.isdir(root):
            return jsonify({"error": "not a directory: " + root}), 400
        try:
            row = dev_mod.register(con(), root, name=body.get("name") or None,
                                   profile=body.get("profile") or "ums")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(dict(row))

    @app.post("/api/devices/<int:did>/set")
    def device_set(did):
        """Add or remove set rules. Body: {add: [[kind, ref]], remove: [...]}"""
        c = con()
        body = request.json or {}
        for kind, ref in body.get("add", []):
            c.execute("INSERT OR IGNORE INTO device_set(device_id, kind, ref, "
                      "added_at) VALUES (?,?,?,?)", (did, kind, ref, time.time()))
        for kind, ref in body.get("remove", []):
            c.execute("DELETE FROM device_set WHERE device_id=? AND kind=? "
                      "AND ref=?", (did, kind, ref))
        c.commit()
        tracks, _ = planner.desired_tracks(c, did)
        return jsonify({"rules": [dict(r) for r in c.execute(
                            "SELECT kind, ref FROM device_set WHERE device_id=? "
                            "ORDER BY kind, ref", (did,))],
                        "tracks": len(tracks),
                        "bytes": sum(t["size"] for t in tracks)})

    def _device_and_root(c, did):
        d = c.execute("SELECT * FROM device WHERE id=?", (did,)).fetchone()
        if not d:
            return None, None, (jsonify({"error": "no such device"}), 404)
        root = dev_mod.locate(c, d)
        if not root:
            return d, None, (jsonify({
                "error": f"{d['name']} is not mounted",
                "not_mounted": True}), 409)
        return d, root, None

    @app.get("/api/devices/<int:did>/plan")
    def device_plan(did):
        c = con()
        d, root, err = _device_and_root(c, did)
        if err:
            return err
        prune = request.args.get("prune") == "1"
        p = planner.plan(c, d, root, prune=prune)
        space = planner.check_space(p, dev_mod.free_space(root)["free"])
        return jsonify({
            "device": dict(d), "root": root, "space": space,
            "tracks_desired": p["tracks_desired"],
            "unchanged": len(p["unchanged"]),
            "bytes_in": p["bytes_in"], "bytes_out": p["bytes_out"],
            "copies": [{"rel": c_["rel"], "size": c_["size"],
                        "reason": c_["reason"]} for c_ in p["copies"][:500]],
            "copies_total": len(p["copies"]),
            "deletes": [{"rel": d_["rel"], "reason": d_["reason"],
                         "size": d_["size"]} for d_ in p["deletes"][:500]],
            "deletes_total": len(p["deletes"]),
            "playlists": [{"name": pl["name"], "entries": len(pl["entries"]),
                           "skipped": pl["skipped"]} for pl in p["playlists"]],
            "playlist_deletes": p.get("playlist_deletes", []),
            "missing_source": [{"path": m["track"]["path"], "why": m["why"]}
                               for m in p["missing_source"][:50]],
            "missing_total": len(p["missing_source"]),
        })

    @app.post("/api/devices/<int:did>/sync")
    def device_sync(did):
        c = con()
        d, root, err = _device_and_root(c, did)
        if err:
            return err
        prune = bool((request.json or {}).get("prune"))
        p = planner.plan(c, d, root, prune=prune)
        space = planner.check_space(p, dev_mod.free_space(root)["free"])
        if not space["fits"]:
            return jsonify({"error": "the set does not fit on this device",
                            "space": space}), 409

        job_id = _new_job("sync", d["name"])
        _update(job_id, total=len(p["copies"]))

        def work():
            try:
                wc = db_mod.connect(app.config["DB_PATH"])
                # Re-plan on the worker's own connection: the row objects in
                # `p` belong to the request thread's connection.
                wd = wc.execute("SELECT * FROM device WHERE id=?", (did,)).fetchone()
                wp = planner.plan(wc, wd, root, prune=prune)
                _update(job_id, total=len(wp["copies"]))
                dev_mod.touch(wc, did, root)

                def on_event(kind, detail, done, total):
                    _update(job_id, done=done, total=total or len(wp["copies"]),
                            detail=detail)
                    _push_event(job_id, kind, detail)

                summary = executor.execute(wc, wp, prune=prune, on_event=on_event)
                _update(job_id, state="done", result=summary,
                        finished=time.time(), detail="finished")
            except executor.Aborted as exc:
                _update(job_id, state="failed", finished=time.time(),
                        error=f"{exc}. Nothing was left half-written - "
                              "sync again to resume where it stopped.")
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job_id})

    @app.get("/api/devices/<int:did>/log")
    def device_log(did):
        c = con()
        rows = c.execute(
            "SELECT * FROM sync_log WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (did, min(int(request.args.get("limit", 60)), 500))
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    # ------------------------------------------------------------ jobs

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return jsonify({"error": "no such job"}), 404
            payload = dict(job)
            payload["events"] = job["events"][-40:]
        return jsonify(payload)

    @app.get("/api/jobs")
    def job_list():
        with JOBS_LOCK:
            return jsonify([{k: v for k, v in j.items() if k != "events"}
                            for j in sorted(JOBS.values(),
                                            key=lambda j: -j["started"])])

    return app


def serve(db_path=None, host="127.0.0.1", port=7777, open_browser=True):
    app = create_app(db_path)
    if open_browser:
        import webbrowser
        threading.Timer(
            0.8, lambda: webbrowser.open(f"http://{host}:{port}/")
        ).start()
    app.run(host=host, port=port, threaded=True, debug=False)
