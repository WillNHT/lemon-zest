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
from . import download as dl_mod
from . import executor, planner, playlists, scan

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# job id -> progress record. Small and bounded; finished jobs are kept so a
# reloaded page can still show the outcome.
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50
# Per job. A download reports every line yt-dlp writes, and the whole run is
# what someone pastes into a bug report, so the ring has to be long enough
# to hold one rather than just the tail of one.
MAX_EVENTS = 500


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
            del job["events"][:-MAX_EVENTS]


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
            "enriched": one("SELECT COUNT(*) FROM enrichment "
                            "WHERE status='applied'"),
            "enrich_candidates": one("SELECT COUNT(*) FROM enrichment "
                                     "WHERE status='candidate'"),
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
                      "title = '') AND size > 0")
                + one("SELECT COUNT(*) FROM enrichment WHERE status='candidate'")),
            "enrich_states": _enrich_states(c),
            "fingerprint": _fingerprint_state(c),
            "roots": db_mod.roots(c),
        })

    def _enrich_states(c):
        from . import enrich as en
        return en.state_counts(c)

    def _fingerprint_state(c):
        """Whether rung four is available. The interface offers it when a
        text search comes back empty, which is the case it exists for."""
        from . import enrich as en
        return en.fingerprint_status(c)

    def _library_where(args, with_state=True):
        """Build the WHERE clause shared by the track list and the facets."""
        from . import enrich as en

        clauses, params = [], []
        state = (args.get("state") or "").strip()
        if with_state and state in en.STATES:
            clauses.append(en.STATE_SQL + " = ?")
            params.append(state)
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
        from . import enrich as en

        c = con()
        where, params = _library_where(request.args)
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        device_id = request.args.get("device")

        # The enrichment join is on every library query rather than fetched
        # separately per page: the state is a column in the table and a filter
        # in the toolbar, so it has to be sortable, pageable and countable in
        # the same statement as the rest.
        join = " LEFT JOIN enrichment e ON e.content_key = t.content_key "

        total = c.execute(
            f"SELECT COUNT(*) FROM track t {join} WHERE {where}", params
        ).fetchone()[0]
        # Untagged files sort last rather than first: SQLite puts NULLs at the
        # top, which would fill the first screen with the least useful rows.
        rows = c.execute(
            f"SELECT t.*, {en.STATE_SQL} AS enrich_state, "
            "e.status AS enrich_status, e.source AS enrich_source, "
            "e.confidence AS enrich_confidence, "
            "(SELECT COUNT(*) FROM track_override o "
            "   WHERE o.content_key = t.content_key) AS overrides "
            f"FROM track t {join} WHERE {where} "
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
                "content_key": r["content_key"],
                "state": r["enrich_state"],
                "enrich_source": r["enrich_source"],
                "confidence": r["enrich_confidence"],
                "overrides": r["overrides"],
                "empty": r["size"] == 0,
                "untagged": not r["title"],
                "on_device": r["id"] in on_device,
            } for r in rows],
        })

    @app.get("/api/library/keys")
    def library_keys():
        """Every content key the current filter matches, not just this page.

        What "select all 2,306" has to mean when the table only ever holds
        200 rows. Capped, because an action over an unbounded selection is
        one the interface cannot honestly show or undo.
        """
        c = con()
        where, params = _library_where(request.args)
        cap = min(int(request.args.get("cap", 5000)), 20000)
        rows = c.execute(
            "SELECT DISTINCT t.content_key FROM track t "
            "LEFT JOIN enrichment e ON e.content_key = t.content_key "
            f"WHERE {where} LIMIT ?", params + [cap]).fetchall()
        total = c.execute(
            "SELECT COUNT(DISTINCT t.content_key) FROM track t "
            "LEFT JOIN enrichment e ON e.content_key = t.content_key "
            f"WHERE {where}", params).fetchone()[0]
        return jsonify({"keys": [r["content_key"] for r in rows],
                        "total": total, "capped": total > len(rows)})

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

    # ------------------------------------------------------- enrichment

    @app.get("/api/enrich/summary")
    def enrich_summary():
        from . import enrich as en
        return jsonify(en.summary(con()))

    @app.get("/api/enrich/review")
    def enrich_review():
        """The candidates a person still has to judge.

        Read-only and side-effect free, like the dry run: the interface shows
        what would change beside what the file says now, and nothing moves
        until someone accepts it.
        """
        from . import enrich as en
        limit = min(int(request.args.get("limit", 100)), 500)
        rows = en.review_queue(con(), limit=limit)
        return jsonify({"candidates": [{
            "track_id": r["track_id"], "content_key": r["content_key"],
            "rel_path": r["rel_path"], "confidence": r["confidence"],
            "source": r["source"], "mbid": r["mbid"],
            "cover": en.cover_art_url(r["release_id"]),
            "current": {f: r.get(f) for f in
                        ("title", "artist", "album", "album_artist", "duration")},
            "proposed": r["proposed"],
        } for r in rows]})

    def _keys(body):
        """The content keys an action was asked to work on.

        One key or many arrive through the same door: every action in this
        section is a bulk action with a selection of one as its ordinary
        case, and two code paths is how the single-item one quietly grows a
        different meaning.
        """
        keys = body.get("content_keys")
        if isinstance(keys, str):
            keys = [keys]
        if not keys and body.get("content_key"):
            keys = [body["content_key"]]
        return [k for k in (keys or []) if k]

    @app.get("/api/enrich/track/<path:content_key>")
    def enrich_track_detail(content_key):
        """Current values, the stored proposal and any hand-typed override."""
        from . import enrich as en
        out = en.proposal_for(con(), content_key)
        if not out:
            return jsonify({"error": "no such track"}), 404
        return jsonify(out)

    @app.post("/api/enrich/<action>")
    def enrich_decide(action):
        """Accept or reject a selection of candidates.

        Writing the values into the audio files is not done here. Accepting
        settles what the catalog believes, which is cheap and reversible;
        rewriting the files is neither, so it is its own request with its own
        confirmation - see /api/enrich/write.
        """
        from . import enrich as en
        if action not in ("accept", "reject"):
            return jsonify({"error": "unknown action"}), 404
        keys = _keys(request.json or {})
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        c = con()
        fn = en.accept if action == "accept" else en.reject
        done = sum(1 for k in keys if fn(c, k))
        return jsonify({"ok": True, "changed": done, "asked": len(keys)})

    @app.post("/api/enrich/state")
    def enrich_state():
        """Move a selection between states by hand.

        Only `skipped` and `raw` are settable: the other two are outcomes,
        and a button that declared a file `enriched` with no answer behind it
        would be writing a claim rather than recording one.
        """
        from . import enrich as en
        body = request.json or {}
        state = (body.get("state") or "").strip()
        keys = _keys(body)
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        try:
            changed = en.set_state(con(), keys, state)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "changed": changed, "state": state})

    @app.post("/api/enrich/edit")
    def enrich_edit():
        """Type metadata by hand, over one file or a whole selection.

        Only the fields actually sent are touched, which is what makes one
        form usable for a selection: filling in `album_artist` alone fixes a
        folder full of tracks without flattening their titles to one value.
        An empty string clears the hand-typed value and lets whatever the
        source said show through again.
        """
        from . import enrich as en
        body = request.json or {}
        keys = _keys(body)
        fields = body.get("fields") or {}
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        fields = {k: v for k, v in fields.items() if k in en.ENRICHABLE}
        if not fields:
            return jsonify({"error": "no editable field given"}), 400
        clear = [k for k, v in fields.items() if v is None or v == ""]
        set_ = {k: v for k, v in fields.items() if k not in clear}
        c = con()
        for key in keys:
            if clear:
                en.clear_overrides(c, key, clear)
            if set_:
                en.override(c, key, **set_)
        return jsonify({"ok": True, "changed": len(keys),
                        "fields": sorted(fields)})

    @app.post("/api/enrich/run")
    def enrich_run():
        """Look up a hand-picked selection again, by hand.

        New files go through this on their own when they arrive; this is the
        deliberate second ask.

        MusicBrainz answers one request a second, so even a modest selection
        outlives a click: this runs on a worker and reports progress the way
        a scan or a sync does.
        """
        from . import enrich as en
        body = request.json or {}
        keys = _keys(body)
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        write_tags = bool(body.get("write_tags"))
        artwork = bool(body.get("artwork"))
        use_fp = bool(body.get("fingerprint"))
        if use_fp and not en.fingerprint_status(con())["ready"]:
            return jsonify({"error": "fingerprinting needs fpcalc and an "
                                     "AcoustID key; set them up first"}), 400
        job_id = _new_job("enrich", "%d track%s" % (len(keys),
                                                   "" if len(keys) == 1 else "s"))

        def work():
            try:
                c = db_mod.connect(app.config["DB_PATH"])

                def cb(done, total, status):
                    _update(job_id, done=done, total=total,
                            detail="%d of %d - %s" % (done, total, status))

                counts = en.run_tracks(
                    c, keys, write_tags=write_tags, artwork=artwork,
                    use_fingerprint=use_fp, progress=cb)
                bits = ["%d enriched" % counts["applied"],
                        "%d to review" % counts["candidates"]]
                if counts["unmatched"]:
                    bits.append("%d not found" % counts["unmatched"])
                if counts["failed"]:
                    bits.append("%d failed" % counts["failed"])
                if counts["skipped"]:
                    bits.append("%d skipped" % counts["skipped"])
                _update(job_id, state="done", result=counts,
                        finished=time.time(), detail=", ".join(bits))
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job_id})

    @app.post("/api/enrich/write/preview")
    def enrich_write_preview():
        """Exactly what a write would put in each file, before it happens."""
        from . import enrich as en
        keys = _keys(request.json or {})
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        c = con()
        # Capped: the dialog is something a person reads, and a preview of
        # two thousand files is not read, it is scrolled past.
        out = [en.pending_write(c, k) for k in keys[:50]]
        out = [o for o in out if o]
        writable = sum(1 for o in out if o["fields"] and not o["missing"])
        return jsonify({
            "files": out,
            "shown": len(out),
            "total": len(keys),
            "writable": writable,
            "missing": sum(1 for o in out if o["missing"]),
            "nothing_to_write": sum(1 for o in out if not o["fields"]),
            # Files whose tags already say what the catalog says. Writing them
            # is harmless and pointless, and saying so is the difference
            # between "it did nothing" and "there was nothing to do".
            "no_change": sum(1 for o in out
                             if o["fields"] and not o["missing"]
                             and not o["changes"]),
            "renormalised": sum(1 for o in out if o["renormalised"]),
            "no_artwork": sum(1 for o in out if not o["artwork_available"]),
        })

    @app.post("/api/enrich/write")
    def enrich_write():
        """Write what the catalog believes into the audio files themselves.

        Its own endpoint rather than a flag on accept. Every other action
        here edits a database row; this one rewrites files on disk, changes
        their content keys, and cannot be undone by pressing the other
        button - so it is asked for separately and confirmed separately.
        """
        from . import enrich as en
        body = request.json or {}
        keys = _keys(body)
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        artwork = bool(body.get("artwork"))
        job_id = _new_job("write-tags", "%d file%s" % (len(keys),
                                                       "" if len(keys) == 1 else "s"))

        def work():
            try:
                c = db_mod.connect(app.config["DB_PATH"])
                written = failed = 0
                notes = []
                for i, key in enumerate(keys, 1):
                    res = en.write_back_result(c, key, artwork=artwork)
                    if res["ok"]:
                        written += 1
                    else:
                        failed += 1
                    if res["reason"]:
                        # Folded the same way a run folds its lookup errors:
                        # fifty files in one unwritable folder is one sentence
                        # with a count, not fifty lines to scroll past.
                        for n in notes:
                            if n["message"] == res["reason"]:
                                n["count"] += 1
                                break
                        else:
                            if len(notes) < en.MAX_REPORTED_ERRORS:
                                notes.append({"message": res["reason"],
                                              "count": 1})
                    _update(job_id, done=i, total=len(keys),
                            detail="%d written" % written)
                _update(job_id, state="done", finished=time.time(),
                        result={"written": written, "failed": failed,
                                "errors": notes},
                        detail="%d written, %d not written" % (written, failed))
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job_id})

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
                f"SELECT {col} AS v, COUNT(*) n FROM track t "
                # Joined even when no state filter is set: _library_where may
                # put `e.status` in the clause, and the facet counts have to
                # narrow with the table rather than describe a different set.
                "LEFT JOIN enrichment e ON e.content_key = t.content_key "
                f"WHERE {where} AND {col} IS NOT NULL AND {col} <> '' "
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

    def _start_auto_enrich(root=None, content_keys=None, label=None):
        """Kick off the automatic pass that follows a scan or a download.

        Its own job rather than a tail on the caller's: a scan of a folder
        finishes in seconds and an identification pass over it is
        rate-limited to one request a second, so tying them together would
        leave the scan looking like it was still running for half an hour.
        Two jobs, two progress bars, and the scan reports what it did when it
        did it.
        """
        from . import enrich as en
        if not en.auto_enabled():
            return None
        job_id = _new_job("enrich", label or root or "new files")
        _update(job_id, detail="looking for anything new to identify")

        def work():
            try:
                c = db_mod.connect(app.config["DB_PATH"])

                def cb(done, total, status):
                    _update(job_id, done=done, total=total,
                            detail="%d of %d - %s" % (done, total, status))

                counts = en.auto_after_ingest(
                    c, root=root, content_keys=content_keys, progress=cb)
                bits = ["%d enriched" % counts["applied"],
                        "%d to review" % counts["candidates"]]
                if counts["written"]:
                    bits.append("%d file%s tagged" % (
                        counts["written"], "" if counts["written"] == 1 else "s"))
                if counts["unmatched"]:
                    bits.append("%d not found" % counts["unmatched"])
                if counts["failed"]:
                    bits.append("%d failed" % counts["failed"])
                _update(job_id, state="done", result=counts,
                        finished=time.time(), detail=", ".join(bits))
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return job_id

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
                # Whatever the scan found that has never been looked at is
                # looked at now, without being asked.
                counts["enrich_job"] = _start_auto_enrich(root=root)
                _update(job_id, state="done", result=counts,
                        finished=time.time())
            except Exception as exc:
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time())
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job_id})

    # ------------------------------------------------------- downloads

    def _download_state(c):
        cfg = dl_mod.get_config(c)
        status = dl_mod.cookie_status(cfg)
        return {
            "config": cfg,
            "ytdlp": dl_mod.ytdlp_version(),
            "js_runtime": dl_mod.js_runtime_status(cfg),
            "cookies": {
                "mode": status["mode"], "source": status["source"],
                "detail": status["detail"],
                "cookies_file": status["cookies_file"],
                "file_exists": status["file_exists"],
                "profile": status["profile"],
                "profiles": [{"name": p["name"], "path": p["path"]}
                             for p in status["profiles"]],
            },
            "roots": db_mod.roots(c),
        }

    @app.get("/api/download/config")
    def download_config():
        return jsonify(_download_state(con()))

    @app.post("/api/download/config")
    def download_config_save():
        c = con()
        body = request.json or {}
        dl_mod.set_config(c, **{k: v for k, v in body.items()
                                if k in dl_mod.CONFIG_DEFAULTS})
        return jsonify(_download_state(c))

    @app.post("/api/download/probe")
    def download_probe():
        url = ((request.json or {}).get("url") or "").strip()
        if not url:
            return jsonify({"error": "no URL given"}), 400
        c = con()
        try:
            return jsonify(dl_mod.probe(url, dl_mod.get_config(c)))
        except dl_mod.DownloadError as exc:
            return jsonify({"error": str(exc)}), 400

    def _keys_for_paths(c, paths):
        """Content keys for files named by path, in the order given."""
        out, seen = [], set()
        for path in paths:
            row = c.execute("SELECT content_key FROM track WHERE path = ?",
                            (path,)).fetchone()
            if row and row["content_key"] not in seen:
                seen.add(row["content_key"])
                out.append(row["content_key"])
        return out

    @app.post("/api/download")
    def download_start():
        body = request.json or {}
        urls = body.get("urls")
        if isinstance(urls, str):
            urls = urls.split()
        urls = [u.strip() for u in (urls or []) if u and u.strip()]
        if not urls:
            return jsonify({"error": "no URL given"}), 400
        if not dl_mod.ytdlp_command():
            return jsonify({"error": "yt-dlp is not installed. Install it "
                                     "with: pip install yt-dlp"}), 400

        root = (body.get("root") or "").strip() or None
        playlist_name = (body.get("playlist") or "").strip() or None
        single = bool(body.get("no_playlist"))
        archive = body.get("archive") is not False
        job_id = _new_job("download", urls[0] if len(urls) == 1
                          else f"{len(urls)} URLs")

        def work():
            try:
                c = db_mod.connect(app.config["DB_PATH"])

                def on_event(kind, detail, done, total):
                    # Progress moves the bar but is not written down: it is
                    # one line per chunk, and it would push the run's actual
                    # output out of the log within seconds.
                    if kind == "progress":
                        _update(job_id, done=done, total=total, detail=detail)
                    else:
                        _update(job_id, detail=detail)
                        _push_event(job_id, kind, detail)

                summary = dl_mod.download(
                    c, urls, root=root, playlist=playlist_name,
                    on_event=on_event, no_playlist=single, archive=archive)
                # Scoped to the files this run actually fetched, not to the
                # whole folder: a download into a library of two thousand
                # would otherwise start an hours-long pass over all of them.
                keys = _keys_for_paths(c, summary.get("files") or [])
                summary["enrich_job"] = _start_auto_enrich(
                    content_keys=keys,
                    label="%d new file%s" % (len(keys),
                                             "" if len(keys) == 1 else "s")
                ) if keys else None
                _update(job_id, state="done", result=summary,
                        finished=time.time(),
                        detail=f"{summary['downloaded']} downloaded")
            except dl_mod.DownloadError as exc:
                # The log is the point of a failed download: it is what gets
                # copied into a bug report, so it outlives the job's event
                # ring rather than only having been streamed past.
                _update(job_id, state="failed", error=str(exc),
                        finished=time.time(),
                        result={"log": getattr(exc, "log", []),
                                "downloaded": 0, "files": []})
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
                           "skipped": pl["skipped"], "filename": pl["filename"],
                           "replaces": pl.get("replaces")}
                          for pl in p["playlists"]],
            "playlist_deletes": p.get("playlist_deletes", []),
            "playlist_template": p.get("playlist_template"),
            "playlist_strays": p.get("playlist_strays", []),
            "missing_source": [{"path": m["track"]["path"], "why": m["why"]}
                               for m in p["missing_source"][:50]],
            "missing_total": len(p["missing_source"]),
        })

    @app.post("/api/devices/<int:did>/detect-playlists")
    def device_detect_playlists(did):
        """Adopt the naming the card's own playlist files already use.

        Reads the card and writes one column. Declining to guess is a
        result, not an error: a card whose playlists came from elsewhere
        keeps the template it has.
        """
        c = con()
        d, root, err = _device_and_root(c, did)
        if err:
            return err
        pl_dir = (d["playlist_dir"] or "").strip("/")
        pl_root = os.path.join(root, pl_dir) if pl_dir else root
        files = playlists.list_playlist_files(pl_root)
        names = [r["name"] for r in c.execute("SELECT name FROM playlist")]
        template, matched, total = playlists.infer_template(files, names)
        if template:
            c.execute("UPDATE device SET playlist_template=? WHERE id=?",
                      (template, did))
            c.commit()
        return jsonify({"template": template, "matched": matched,
                        "total": total, "applied": bool(template)})

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
            # The whole ring: a truncated log is the one thing a log must
            # not be. It is local, and 500 short lines is a few tens of KB.
            payload["events"] = list(job["events"])
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
