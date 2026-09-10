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

# A hidden library folder stays indexed but drops out of every view of the
# library. Written as SQL rather than a python filter so the counts, the
# facets and the paged table all narrow together.
VISIBLE = ("t.root NOT IN (SELECT root FROM library_root WHERE hidden = 1)")
VISIBLE_BARE = VISIBLE.replace("t.root", "root")
# What is still new. A file leaves the inbox when it has finished arriving
# - identified, skipped or rejected, or simply old enough - which db's
# promote_inbox decides and writes down. The watermark is still honoured
# underneath it, so "clear the inbox" keeps meaning what it did.
INBOX_MARK = ("COALESCE((SELECT CAST(value AS REAL) FROM meta "
              "WHERE key = 'inbox_seen_at'), 0)")
NEW_SQL = (f"(t.added_at IS NOT NULL AND t.added_at > {INBOX_MARK} "
           "AND t.inbox_done_at IS NULL)")

# The promotion is a write, and the stats endpoint is polled every couple of
# seconds, so it runs on a timer rather than on every poll.
PROMOTE_EVERY = 20.0
_promoted_at = [0.0]

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
                        # Set by a download: the item counts of the batch,
                        # which is what a progress bar over forty tracks
                        # needs and what bytes-of-one-file cannot give.
                        "batch": None,
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

    def _promote_due(c):
        """Empty the inbox of whatever has settled, now and then.

        Here because this is the endpoint the page keeps asking: a rule that
        only ran after a scan would leave a file sitting in the inbox for
        hours after the thing it was waiting for had happened.
        """
        if time.time() - _promoted_at[0] < PROMOTE_EVERY:
            return
        _promoted_at[0] = time.time()
        try:
            db_mod.promote_inbox(c)
        except Exception:      # noqa: BLE001 - a badge is not worth a 500
            traceback.print_exc()

    @app.get("/api/stats")
    def stats():
        c = con()
        _promote_due(c)
        one = lambda q: c.execute(q).fetchone()[0]
        vis = f"WHERE {VISIBLE_BARE}"
        return jsonify({
            "tracks": one(f"SELECT COUNT(*) FROM track {vis}"),
            "inbox": one("SELECT COUNT(*) FROM track t "
                         f"WHERE {VISIBLE} AND {NEW_SQL}"),
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
            "sync_list": one("SELECT COUNT(*) FROM sync_list"),
            # The identification backlog, on every poll: it is the one piece
            # of work that outlives the page that started it.
            "enrich_queue": _queue().status(),
            "empty_files": one("SELECT COUNT(*) FROM track WHERE size = 0"),
            "attention": (
                one("SELECT COUNT(*) FROM playlist_entry WHERE track_id IS NULL")
                + one("SELECT COUNT(*) FROM track WHERE size = 0")
                + one("SELECT COUNT(*) FROM track WHERE (title IS NULL OR "
                      "title = '') AND size > 0")
                + one("SELECT COUNT(*) FROM enrichment WHERE status='candidate'")),
            "enrich_states": _enrich_states(c),
            "fingerprint": _fingerprint_state(c),
            "roots": db_mod.roots(c, include_hidden=False),
            "root_detail": db_mod.roots_detail(c),
        })

    def _enrich_states(c):
        from . import enrich as en
        return en.state_counts(c)

    def _fingerprint_state(c):
        """Whether rung four is available. The interface offers it when a
        text search comes back empty, which is the case it exists for."""
        from . import enrich as en
        return en.fingerprint_status(c)

    # A decade off the year column. The year is a string - "2012",
    # "2012-09", "2012-09-03" - so the decade is its first three characters
    # with a nought on the end, which sorts and groups without a date parser.
    DECADE_SQL = ("CASE WHEN t.year IS NULL OR t.year = '' THEN NULL "
                  "ELSE SUBSTR(t.year, 1, 3) || '0s' END")

    def _library_where(args, with_state=True):
        """Build the WHERE clause shared by the track list and the facets."""
        from . import enrich as en

        clauses, params = [VISIBLE], []
        # A playlist is a question about the library, not a different kind
        # of thing: asked here, it comes back through the same table, the
        # same facets and the same inspector as everything else.
        pid = (args.get("playlist") or "").strip()
        if pid.isdigit():
            clauses.append("t.id IN (SELECT track_id FROM playlist_entry "
                           "WHERE playlist_id = ?)")
            params.append(int(pid))
        if str(args.get("new") or "") in ("1", "true", "yes"):
            clauses.append(NEW_SQL)
        state = (args.get("state") or "").strip()
        if with_state and state in en.STATES:
            clauses.append(en.STATE_SQL + " = ?")
            params.append(state)
        q = (args.get("q") or "").strip()
        if q:
            clauses.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
            params += [f"%{q}%"] * 3
        decade = (args.get("decade") or "").strip()
        if decade:
            clauses.append(DECADE_SQL + " = ?")
            params.append(decade)
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

    # What a column heading means in SQL. A whitelist rather than a column
    # name off the query string: this is the one place a request gets to
    # name part of a statement, and it names a key here or nothing at all.
    def _sorts():
        from . import enrich as en
        return {
            "track_no": ["t.disc_no", "t.track_no", "t.title"],
            "title": ["t.title", "t.rel_path"],
            "duration": ["t.duration"],
            "artist": ["t.artist", "t.album", "t.disc_no", "t.track_no"],
            "album": ["t.album", "t.disc_no", "t.track_no"],
            "state": [en.STATE_SQL, "t.rel_path"],
            "format": ["t.ext", "t.bitrate"],
            "added": ["t.added_at", "t.id"],
            "isrc": ["t.isrc"],
            "on_device": ["t.rel_path"],
        }

    def _order_by(args, where_params):
        """The ORDER BY the request asked for, and any parameters it needs.

        Three defaults, because three questions: an inbox is newest first, a
        playlist is in the order its source published, and the library is an
        album shelf where the track order inside a release is the point.
        Whatever the default, a click on a heading overrides it.

        Empty values sort last in both directions. SQLite puts NULLs first,
        which would otherwise fill the first screen of a sort by artist with
        the rows that have no artist - the least useful thing it could show.
        """
        sorts = _sorts()
        key = (args.get("sort") or "").strip()
        pid = (args.get("playlist") or "").strip()
        descending = (args.get("dir") or "asc").lower() == "desc"
        if key in sorts:
            direction = " DESC" if descending else " ASC"
            terms = []
            for expr in sorts[key]:
                terms.append("(%s IS NULL OR %s = '')" % (expr, expr))
                terms.append(expr + direction)
            return "ORDER BY " + ", ".join(terms), []
        if key == "pos" and pid.isdigit():
            direction = " DESC" if descending else " ASC"
            return ("ORDER BY (SELECT MIN(pos) FROM playlist_entry pe "
                    "WHERE pe.track_id = t.id AND pe.playlist_id = ?)"
                    + direction, [int(pid)])
        if pid.isdigit():
            return ("ORDER BY (SELECT MIN(pos) FROM playlist_entry pe "
                    "WHERE pe.track_id = t.id AND pe.playlist_id = ?)",
                    [int(pid)])
        if args.get("order") == "added":
            return "ORDER BY t.added_at DESC, t.id DESC", []
        return ("ORDER BY (t.album_artist IS NULL), t.album_artist, t.album, "
                "t.disc_no, t.track_no, t.title, t.rel_path"), []

    @app.get("/api/library")
    def library():
        from . import enrich as en

        c = con()
        where, params = _library_where(request.args)
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        device_id = request.args.get("device")
        order, order_params = _order_by(request.args, params)
        # Where each track sits in the playlist being looked at, so the
        # table can number the rows the way the playlist does.
        pid = (request.args.get("playlist") or "").strip()
        pos_col = ("(SELECT MIN(pos) FROM playlist_entry pe "
                   " WHERE pe.track_id = t.id AND pe.playlist_id = %d) "
                   "AS playlist_pos, "
                   "(SELECT MAX(pe.added_at) FROM playlist_entry pe "
                   " WHERE pe.track_id = t.id AND pe.playlist_id = %d) "
                   "AS playlist_added_at, " % (int(pid), int(pid))
                   ) if pid.isdigit() else ""

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
            f"SELECT t.*, {pos_col}{en.STATE_SQL} AS enrich_state, "
            "e.status AS enrich_status, e.source AS enrich_source, "
            "e.confidence AS enrich_confidence, "
            "(SELECT COUNT(*) FROM track_override o "
            "   WHERE o.content_key = t.content_key) AS overrides "
            f"FROM track t {join} WHERE {where} "
            + order
            + " LIMIT ? OFFSET ?", params + order_params + [limit, offset]
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
                "playlist_pos": r["playlist_pos"] if pos_col else None,
                "playlist_added_at": (r["playlist_added_at"] if pos_col
                                      else None),
                "state": r["enrich_state"],
                "enrich_source": r["enrich_source"],
                "confidence": r["enrich_confidence"],
                "overrides": r["overrides"],
                "added_at": r["added_at"],
                "mtime": r["mtime"],
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

    @app.post("/api/inbox/seen")
    def inbox_seen():
        """Empty the inbox by hand, ahead of the rule that would anyway.

        Files that have settled leave on their own; this is for the rest -
        the ones still waiting on a decision or on a network that is not
        there. The files are untouched and stay in the library.
        """
        c = con()
        return jsonify({"ok": True, "at": time.time(),
                        "promoted": db_mod.clear_inbox(c)})

    # ---------------------------------------------------- library folders

    @app.get("/api/roots")
    def roots_list():
        return jsonify(db_mod.roots_detail(con()))

    @app.post("/api/roots/hide")
    def roots_hide():
        body = request.json or {}
        root = (body.get("root") or "").strip()
        if not root:
            return jsonify({"error": "no folder given"}), 400
        c = con()
        db_mod.set_root_hidden(c, root, bool(body.get("hidden", True)))
        return jsonify({"roots": db_mod.roots_detail(c)})

    @app.post("/api/roots/remove")
    def roots_remove():
        """Forget a library folder. Never deletes audio files."""
        body = request.json or {}
        root = (body.get("root") or "").strip()
        if not root:
            return jsonify({"error": "no folder given"}), 400
        c = con()
        out = db_mod.remove_root(c, root,
                                 forget_tracks=bool(body.get("forget_tracks", True)))
        out["roots"] = db_mod.roots_detail(c)
        return jsonify(out)

    @app.get("/api/art/<path:content_key>")
    def track_art(content_key):
        """The artwork embedded in the file itself.

        Served from the audio rather than from the Cover Art Archive on
        purpose: it is what the file actually carries, which is the thing
        someone looking at a track wants to see - including when it is a
        video thumbnail that ought to be replaced. The archive's copy is
        offered separately, by URL, in the track detail.
        """
        from . import meta as meta_mod

        c = con()
        row = c.execute(
            "SELECT path FROM track WHERE content_key = ? ORDER BY id LIMIT 1",
            (content_key,)).fetchone()
        if not row:
            return jsonify({"error": "no such track"}), 404
        try:
            art = meta_mod.artwork(row["path"])
        except Exception:      # noqa: BLE001 - a missing picture is a 404
            art = None
        if not art:
            return jsonify({"error": "no embedded artwork"}), 404
        data, mime = art
        # Content keys change when a file is rewritten, so a cached picture
        # can never be the wrong one for this URL.
        return app.response_class(data, mimetype=mime, headers={
            "Cache-Control": "public, max-age=86400"})

    # The types a browser can decode without help. Anything else is offered
    # as a download rather than played: telling somebody a file is broken
    # when the truth is their browser cannot read FLAC would be worse than
    # saying nothing.
    AUDIO_MIME = {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
        ".mp4": "audio/mp4", ".aac": "audio/aac", ".oga": "audio/ogg",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
        ".wav": "audio/wav", ".webm": "audio/webm",
    }

    @app.get("/api/audio/<int:track_id>")
    def track_audio(track_id):
        """The audio itself, for a listen.

        The point is to hear whether a file is what it claims to be - a
        download that produced four seconds of silence, or a video whose
        audio never arrived, looks perfectly healthy in every column of the
        table. Flask serves it with Range support, so seeking works and a
        listen costs the first few seconds rather than the whole file.

        The path comes from the catalog and never from the request: this is
        a local server, but a URL is still an outside thing.
        """
        from flask import send_file

        c = con()
        row = c.execute("SELECT path FROM track WHERE id = ?",
                        (track_id,)).fetchone()
        if not row:
            return jsonify({"error": "no such track"}), 404
        path = row["path"]
        if not os.path.isfile(path):
            return jsonify({"error": "the file is not where the catalog "
                                     "thinks: " + path}), 404
        mime = AUDIO_MIME.get(os.path.splitext(path)[1].lower(),
                              "application/octet-stream")
        return send_file(path, mimetype=mime, conditional=True,
                         download_name=os.path.basename(path))

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
        # The fourth thing the badge counts. It lives in the review queue
        # rather than in a list of broken files, but a count that names a
        # page has to be answered by that page: a badge saying 1 over a
        # page saying "nothing needs attention" is the interface calling
        # itself a liar.
        awaiting = c.execute(
            "SELECT COUNT(*) FROM enrichment WHERE status = 'candidate'"
        ).fetchone()[0]
        return jsonify({
            "awaiting": awaiting,
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
        c = con()
        out = en.proposal_for(c, content_key)
        if not out:
            return jsonify({"error": "no such track"}), 404
        # Where else this track turns up. A track is not only a row in the
        # library: it is in playlists, and those are what reach a player -
        # so "is this one already on the card" is answered here rather than
        # by opening each playlist in turn.
        out["playlists"] = [dict(r) for r in c.execute(
            "SELECT p.id, p.name, p.origin, e.pos, "
            "(SELECT COUNT(*) FROM playlist_entry x "
            " WHERE x.playlist_id = p.id) AS entries "
            "FROM playlist_entry e "
            "JOIN playlist p ON p.id = e.playlist_id "
            "JOIN track t ON t.id = e.track_id "
            "WHERE t.content_key = ? GROUP BY p.id ORDER BY p.name",
            (content_key,))]
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
        # Typed search terms, when the person could see why the automatic
        # ones failed. Refused over a selection: one query cannot describe
        # forty different tracks, and applying it to all of them would file
        # thirty-nine of them under the fortieth.
        query = body.get("query") or None
        if query:
            query = {k: (query.get(k) or "").strip()
                     for k in ("artist", "title", "album")}
            if not query["title"]:
                return jsonify({"error": "a search needs a title"}), 400
            if len(keys) != 1:
                return jsonify({"error": "search terms apply to one track at "
                                         "a time"}), 400
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

                # Through the queue's own client, with the queue held
                # back: a person is waiting, and two clients asking at once
                # is what gets the whole program refused.
                with _queue().exclusive() as client:
                    counts = en.run_tracks(
                        c, keys, write_tags=write_tags, artwork=artwork,
                        use_fingerprint=use_fp, progress=cb, query=query,
                        client=client)
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
        for field in ("decade", "genre", "artist", "album"):
            args = dict(request.args)
            # A pane does not filter itself, so its own options stay visible.
            args.pop(field, None)
            where, params = _library_where(args)
            col = ("COALESCE(t.album_artist, t.artist)" if field == "artist"
                   else DECADE_SQL if field == "decade"
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
            " AND e.track_id IS NULL) unmatched, "
            # What has joined since this playlist was last looked at. The
            # mark in the sidebar is this number being greater than nought,
            # and looking at the playlist is what clears it.
            "(SELECT COUNT(*) FROM playlist_entry e WHERE e.playlist_id=p.id "
            " AND e.added_at IS NOT NULL "
            " AND e.added_at > COALESCE(p.seen_at, 0)) fresh "
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
            "e.added_at, "
            "t.id track_id, t.title, t.artist, t.album, t.ext, t.size, t.bitrate "
            "FROM playlist_entry e LEFT JOIN track t ON t.id=e.track_id "
            "WHERE e.playlist_id=? ORDER BY e.pos", (pid,)
        ).fetchall()
        devices = c.execute(
            "SELECT d.id, d.name, d.label FROM device d "
            "JOIN device_set s ON s.device_id=d.id "
            "WHERE s.kind='playlist' AND s.ref=?", (pl["name"],)
        ).fetchall()
        # The watermark this view was opened against, handed back before it
        # moves: the rows that are new are new *to you*, and they have to go
        # on looking new for as long as you are looking at them. Marking
        # seen here rather than on a later click is the honest reading of
        # "once the user has seen it".
        since = pl["seen_at"] or 0
        if request.args.get("peek") != "1":
            c.execute("UPDATE playlist SET seen_at = ? WHERE id = ?",
                      (time.time(), pid))
            c.commit()
        return jsonify({"playlist": dict(pl),
                        "entries": [dict(e) for e in entries],
                        "since": since,
                        "fresh": sum(1 for e in entries
                                     if (e["added_at"] or 0) > since),
                        "devices": [dict(d) for d in devices]})

    @app.post("/api/playlists/<int:pid>/refresh")
    def playlist_refresh(pid):
        """Fetch a playlist's source again: the entries, and the audio.

        The same run as any other download, on the same queue and reported
        the same way - the archive skips what is already here, so what
        comes down is only what has been added since. The playlist is then
        rebuilt from the source's own listing, so a track added in the
        middle lands in the middle rather than at the end.
        """
        c = con()
        pl = c.execute("SELECT * FROM playlist WHERE id = ?", (pid,)).fetchone()
        if not pl:
            return jsonify({"error": "no such playlist"}), 404
        url = pl["source_uri"]
        if not url or playlists.origin_of(url) != "youtube":
            return jsonify({"error": "this playlist does not come from a "
                                     "YouTube URL, so there is nothing to "
                                     "fetch it from"}), 400
        if not dl_mod.ytdlp_command():
            return jsonify({"error": "yt-dlp is not installed. Install it "
                                     "with: pip install yt-dlp"}), 400
        # Written down as a URL that has been asked for, so it turns up in
        # the recent list beside the ones typed by hand.
        row = c.execute("SELECT root, kept FROM download_url WHERE url = ?",
                        (url,)).fetchone()
        return jsonify({"job": _run_download(
            [url], root=(row["root"] if row else None) or None,
            playlist_name=pl["name"], archive=True,
            kept_url=url if row and row["kept"] else None,
            label=pl["name"])})

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

    def _queue():
        from . import enrichq
        return enrichq.get_queue(app.config["DB_PATH"])

    def _start_auto_enrich(root=None, content_keys=None, label=None,
                           priority="sweep"):
        """Hand what just arrived to the identification queue.

        Not a job of its own any more. A job is a thing with a beginning and
        an end that somebody watches; identification is a backlog the
        program works through at one request a second whatever else is
        going on, and pretending that each scan owned its own pass is what
        let two of them run at once and get the whole program throttled.

        Returns how many files were queued.
        """
        from . import enrich as en
        if not en.auto_enabled():
            return 0
        c = con()
        if content_keys is None:
            keys = [r["content_key"] for r in en.pending(c, root=root)]
        else:
            keys = list(content_keys)
        return _queue().submit(keys, priority=priority, label=label)

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
                # A library folder holds its playlists as well as its music,
                # so a scan of it reads both. Recursive and after the audio:
                # an entry can only be matched to a track the scan has
                # already catalogued.
                _update(job_id, detail="reading playlists")
                found = playlists.import_dir(c, root, recursive=True)
                counts["playlists"] = len(found)
                counts["playlist_entries"] = sum(f["total"] for f in found)
                # Whatever the scan found that has never been looked at
                # joins the identification queue, without being asked.
                counts["queued"] = _start_auto_enrich(root=root)
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
        from . import bundled

        cfg = dl_mod.get_config(c)
        status = dl_mod.cookie_status(cfg)
        return {
            "config": cfg,
            # What the executable brought with it. Worth saying out loud:
            # every "install ffmpeg" message on this page is wrong when the
            # answer is already inside the binary.
            "bundled": bundled.status(),
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
            # The last few URLs asked for, and the playlists being kept an
            # eye on. Both come off the same table.
            "recent": dl_mod.recent_urls(c, 5),
            "kept": dl_mod.kept_urls(c),
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

    def _run_download(urls, root=None, playlist_name=None, single=False,
                      archive=True, label=None, kept_url=None):
        """Start a download on a worker and return its job id.

        Shared by the URL box and by the kept playlists, which are the same
        run with the URL and the playlist name filled in from a row rather
        than from a form.
        """
        job_id = _new_job("download", label or (urls[0] if len(urls) == 1
                                                else f"{len(urls)} URLs"))

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

                def on_batch(batch):
                    # The batch as counts, kept whole on the job: the page
                    # polls this and draws the run, rather than asking a
                    # reader to count "wrote ..." lines in the log.
                    _update(job_id, batch=batch)

                summary = dl_mod.download(
                    c, urls, root=root, playlist=playlist_name,
                    on_event=on_event, on_batch=on_batch,
                    no_playlist=single, archive=archive)
                # The files this run fetched were identified as they
                # landed, one by one, so there is nothing left to start for
                # them. Two exceptions, both scoped to the folder rather
                # than the library: a run that fell back to a full rescan
                # turns up files an interrupted earlier run left
                # uncatalogued, and a run whose pipeline could not start
                # leaves its own arrivals unidentified.
                owed = (summary.get("rescanned")
                        or (summary["downloaded"] and not summary.get("queued")))
                summary["queued_extra"] = _start_auto_enrich(
                    root=summary.get("root")) if owed else 0
                if kept_url:
                    # What the kept row shows next time: when it was last
                    # looked at, and what that look turned up.
                    added = sum(p["added"] for p in summary["playlists"])
                    c.execute("UPDATE download_url SET last_checked = ?, "
                              "last_added = ? WHERE url = ?",
                              (time.time(), added, kept_url))
                    c.commit()
                queued = summary.get("queued") or 0
                _update(job_id, state="done", result=summary,
                        finished=time.time(),
                        detail=f"{summary['downloaded']} downloaded"
                               + (f", {queued} being identified"
                                  if queued else ""))
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
        return job_id

    def _urls_from(body):
        urls = body.get("urls")
        if isinstance(urls, str):
            urls = urls.split()
        return [u.strip() for u in (urls or []) if u and u.strip()]

    @app.post("/api/download")
    def download_start():
        body = request.json or {}
        urls = _urls_from(body)
        if not urls:
            return jsonify({"error": "no URL given"}), 400
        if not dl_mod.ytdlp_command():
            return jsonify({"error": "yt-dlp is not installed. Install it "
                                     "with: pip install yt-dlp"}), 400
        return jsonify({"job": _run_download(
            urls,
            root=(body.get("root") or "").strip() or None,
            playlist_name=(body.get("playlist") or "").strip() or None,
            single=bool(body.get("no_playlist")),
            archive=body.get("archive") is not False)})

    @app.post("/api/download/keep")
    def download_keep():
        """Keep a playlist URL on the page, or stop keeping it.

        Listed here rather than when it is fetched: a URL is kept before it
        has ever been downloaded as often as after, and the name to show it
        under is the source's, which only a listing knows.
        """
        body = request.json or {}
        url = (body.get("url") or "").strip()
        if not url:
            return jsonify({"error": "no URL given"}), 400
        c = con()
        if body.get("kept") is False:
            dl_mod.keep_url(c, url, kept=False)
            return jsonify(_download_state(c))
        try:
            info = dl_mod.probe(url, dl_mod.get_config(c))
        except dl_mod.DownloadError as exc:
            return jsonify({"error": str(exc)}), 400
        if not info["is_playlist"]:
            return jsonify({"error": "that URL is a single video, not a "
                                     "playlist"}), 400
        dl_mod.keep_url(c, url, kept=True, info=info,
                        playlist_name=playlists.norm_name(info["title"] or url),
                        root=(body.get("root") or "").strip() or None)
        return jsonify(_download_state(c))

    @app.post("/api/download/keep/run")
    def download_keep_run():
        """Fetch a kept playlist again: whatever is new, and nothing else.

        The download archive is what makes this cheap - every item already
        fetched is skipped - and the playlist is rebuilt from the source's
        own listing, so a track added in the middle lands in the middle.
        """
        body = request.json or {}
        url = (body.get("url") or "").strip()
        c = con()
        row = c.execute("SELECT * FROM download_url WHERE url = ? AND kept = 1",
                        (url,)).fetchone()
        if row is None:
            return jsonify({"error": "that URL is not one of the kept ones"}), 404
        if not dl_mod.ytdlp_command():
            return jsonify({"error": "yt-dlp is not installed. Install it "
                                     "with: pip install yt-dlp"}), 400
        return jsonify({"job": _run_download(
            [url], root=row["root"] or None,
            playlist_name=row["playlist_name"] or None,
            archive=True, kept_url=url,
            label=row["title"] or url)})

    @app.get("/api/enrich/queue")
    def enrich_queue():
        """What is waiting to be identified, and how it is going."""
        return jsonify(_queue().status())

    @app.post("/api/enrich/queue")
    def enrich_queue_edit():
        """Pause, resume, or drop the backlog.

        Pausing is worth having: identification is the one thing here that
        leans on somebody else's service, and a person who wants their
        network to themselves for ten minutes should not have to close the
        program to get it.
        """
        body = request.json or {}
        q = _queue()
        if body.get("pause"):
            q.pause()
        if body.get("resume"):
            q.resume()
        dropped = q.clear() if body.get("clear") else 0
        return jsonify(dict(q.status(), dropped=dropped))

    # ------------------------------------------------------- the sync list

    def _sync_list(c):
        """What has been prepared, and what it comes to.

        Resolved every time it is asked for rather than stored: the rules
        name a playlist or an artist, and what those hold changes as the
        library does. A list prepared last week should describe this week's
        library when the card finally goes in.
        """
        rules = [dict(r) for r in c.execute(
            "SELECT kind, ref, added_at FROM sync_list ORDER BY kind, ref")]
        tracks, playlist_names = planner.tracks_for_rules(c, rules)
        # Per rule as well as in total: "this playlist is 400 MB of it" is
        # the number somebody dropping a rule is looking for.
        for rule in rules:
            got, _ = planner.tracks_for_rules(c, [rule])
            rule["tracks"] = len(got)
            rule["bytes"] = sum(t["size"] or 0 for t in got)
        return {
            "rules": rules,
            "tracks": len(tracks),
            "bytes": sum(t["size"] or 0 for t in tracks),
            "playlists": playlist_names,
            "devices": [{"id": d["id"], "name": d["name"],
                         "mounted_at": dev_mod.locate(c, d)}
                        for d in c.execute("SELECT * FROM device ORDER BY name")],
        }

    @app.get("/api/sync-list")
    def sync_list_get():
        return jsonify(_sync_list(con()))

    @app.post("/api/sync-list")
    def sync_list_edit():
        """Add or drop rules. Body: {add: [[kind, ref]], remove: [...]}."""
        c = con()
        body = request.json or {}
        for kind, ref in body.get("add", []):
            if kind not in ("playlist", "artist", "album", "track") or not ref:
                return jsonify({"error": "unknown rule: %s" % kind}), 400
            c.execute("INSERT OR IGNORE INTO sync_list(kind, ref, added_at) "
                      "VALUES (?,?,?)", (kind, ref, time.time()))
        for kind, ref in body.get("remove", []):
            c.execute("DELETE FROM sync_list WHERE kind=? AND ref=?",
                      (kind, ref))
        if body.get("clear"):
            c.execute("DELETE FROM sync_list")
        c.commit()
        return jsonify(_sync_list(c))

    @app.post("/api/sync-list/keys")
    def sync_list_add_keys():
        """Add tracks by content key - what a selection in the table is.

        Stored by path, because that is what a device rule names, and one
        content key can be two files.
        """
        c = con()
        keys = _keys(request.json or {})
        if not keys:
            return jsonify({"error": "content_key required"}), 400
        added = 0
        for key in keys:
            for row in c.execute("SELECT path FROM track WHERE content_key = ?",
                                 (key,)):
                cur = c.execute(
                    "INSERT OR IGNORE INTO sync_list(kind, ref, added_at) "
                    "VALUES ('track', ?, ?)", (row["path"], time.time()))
                added += cur.rowcount
        c.commit()
        out = _sync_list(c)
        out["added"] = added
        return jsonify(out)

    @app.post("/api/sync-list/apply")
    def sync_list_apply():
        """Copy the prepared rules onto a device.

        Only the rules move. The list stays as it is, because the same set
        usually goes onto more than one card, and because a list that
        emptied itself when applied would be impossible to check afterwards.
        """
        c = con()
        did = (request.json or {}).get("device")
        d = c.execute("SELECT * FROM device WHERE id = ?", (did,)).fetchone()
        if not d:
            return jsonify({"error": "no such device"}), 404
        rules = c.execute("SELECT kind, ref FROM sync_list").fetchall()
        if not rules:
            return jsonify({"error": "the sync list is empty"}), 400
        for r in rules:
            c.execute("INSERT OR IGNORE INTO device_set(device_id, kind, ref, "
                      "added_at) VALUES (?,?,?,?)",
                      (did, r["kind"], r["ref"], time.time()))
        c.commit()
        tracks, _ = planner.desired_tracks(c, did)
        return jsonify({"ok": True, "device": did, "applied": len(rules),
                        "tracks": len(tracks),
                        "bytes": sum(t["size"] or 0 for t in tracks),
                        "mounted": bool(dev_mod.locate(c, d))})

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


def _warm(app):
    """Fill the caches the first page view would otherwise wait on.

    One thread, at startup, doing what the download page used to do inside
    the click that opened it.
    """
    def work():
        try:
            dl_mod.warm_cache()
        except Exception:      # noqa: BLE001 - a warm cache is an optimisation
            pass
        try:
            _resume_backlog(app)
        except Exception:      # noqa: BLE001 - as above
            traceback.print_exc()

    threading.Thread(target=work, daemon=True).start()
    return app


def _resume_backlog(app):
    """Put back on the queue whatever was never looked at.

    The queue is in memory, and a program that is closed halfway through a
    hundred files would otherwise forget them. It does not need to be
    written down, though: a file that has never been identified is `raw` in
    the catalog, so the backlog can be rebuilt by asking the catalog what it
    still does not know.
    """
    from . import enrich as en
    from . import enrichq

    if not en.auto_enabled():
        return
    con = db_mod.connect(app.config["DB_PATH"])
    try:
        keys = [r["content_key"] for r in en.pending(con)]
    finally:
        con.close()
    if keys:
        enrichq.get_queue(app.config["DB_PATH"]).submit(keys, priority="sweep")


def serve(db_path=None, host="127.0.0.1", port=7777, open_browser=True):
    app = _warm(create_app(db_path))
    if open_browser:
        import webbrowser
        threading.Timer(
            0.8, lambda: webbrowser.open(f"http://{host}:{port}/")
        ).start()
    app.run(host=host, port=port, threaded=True, debug=False)
