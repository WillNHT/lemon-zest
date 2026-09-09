"""SQLite catalog. Single writer, WAL, schema created on demand."""
import os
import sqlite3

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per audio file on disk.
CREATE TABLE IF NOT EXISTS track (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,   -- absolute, NFC-normalised
    rel_path     TEXT NOT NULL,          -- relative to its library root
    root         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL    NOT NULL,
    content_key  TEXT    NOT NULL,       -- size + head/tail digest
    ext          TEXT    NOT NULL,
    duration     REAL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         TEXT,
    genre        TEXT,
    isrc         TEXT,
    purl         TEXT,                   -- yt-dlp source URL
    codec        TEXT,
    bitrate      INTEGER,
    sample_rate  INTEGER,
    seen_at      REAL NOT NULL,
    -- When the catalog first saw this file. Distinct from seen_at, which
    -- every rescan moves forward: the inbox is "what arrived since I last
    -- looked", and that question needs a timestamp that does not change.
    added_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_track_isrc   ON track(isrc);
CREATE INDEX IF NOT EXISTS ix_track_artist ON track(artist);
CREATE INDEX IF NOT EXISTS ix_track_album  ON track(album);
CREATE INDEX IF NOT EXISTS ix_track_root   ON track(root);
CREATE INDEX IF NOT EXISTS ix_track_added  ON track(added_at);

-- Library folders the scanner watches. Kept apart from track.root so a
-- folder that holds no music yet is still a place downloads can land: an
-- empty folder is the normal way to start a library.
CREATE TABLE IF NOT EXISTS library_root (
    root       TEXT PRIMARY KEY,   -- absolute, NFC-normalised
    added_at   REAL,
    scanned_at REAL,
    -- Hidden folders stay indexed but drop out of the library, the facets
    -- and the inbox. Removing a folder is the other option, and that one
    -- forgets its tracks; neither ever touches a file on disk.
    hidden     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    origin      TEXT,          -- 'local' | provider name
    source_uri  TEXT,          -- #Collection URI, when the file carried one
    imported_from TEXT,
    imported_at REAL
);

CREATE TABLE IF NOT EXISTS playlist_entry (
    playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
    pos         INTEGER NOT NULL,
    track_id    INTEGER REFERENCES track(id) ON DELETE SET NULL,
    raw_path    TEXT NOT NULL,     -- as written in the source file
    title_hint  TEXT,              -- from #EXTINF
    duration    REAL,
    source_uri  TEXT,              -- per-track provider URI, e.g. apple_music:track:N
    PRIMARY KEY (playlist_id, pos)
);
CREATE INDEX IF NOT EXISTS ix_pe_track ON playlist_entry(track_id);

CREATE TABLE IF NOT EXISTS device (
    id            INTEGER PRIMARY KEY,
    device_uid    TEXT NOT NULL UNIQUE,  -- uuid in the marker file at the device root
    label         TEXT,                  -- volume label, the everyday lookup
    name          TEXT NOT NULL,
    root          TEXT,                  -- last known mount point
    profile       TEXT NOT NULL DEFAULT 'ums',
    music_dir     TEXT NOT NULL DEFAULT 'Music',
    playlist_dir  TEXT NOT NULL DEFAULT 'Music',
    path_template TEXT NOT NULL DEFAULT '{album_artist}/{album}/{track:02d} {title}{ext}',
    -- How the player names its own playlist files. Writing to any other
    -- spelling adds a second playlist beside the one already there.
    playlist_template TEXT NOT NULL DEFAULT '{name}.m3u8',
    created_at    REAL,
    last_seen     REAL,
    last_sync     REAL
);

-- Desired set: what the user ticked. Intent only, no paths.
CREATE TABLE IF NOT EXISTS device_set (
    device_id INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,   -- playlist | artist | album | track
    ref       TEXT NOT NULL,
    added_at  REAL,
    PRIMARY KEY (device_id, kind, ref)
);

-- Observed set: what Lemon Zest believes is physically on the card.
CREATE TABLE IF NOT EXISTS device_manifest (
    device_id   INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    dest_rel    TEXT NOT NULL,
    track_id    INTEGER REFERENCES track(id) ON DELETE SET NULL,
    content_key TEXT NOT NULL,   -- of the SOURCE file, so a replug is a no-op
    size        INTEGER NOT NULL,
    written_at  REAL NOT NULL,
    PRIMARY KEY (device_id, dest_rel)
);
CREATE INDEX IF NOT EXISTS ix_dm_track ON device_manifest(device_id, track_id);

-- Playlist files Lemon Zest has written to a device, so that unticking a
-- playlist removes its file instead of leaving a stale one behind.
CREATE TABLE IF NOT EXISTS device_playlist (
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,       -- relative to the device's playlist dir
    name       TEXT NOT NULL,
    entries    INTEGER,
    written_at REAL,
    PRIMARY KEY (device_id, filename)
);

-- ------------------------------------------------------------ enrichment
--
-- What an external source said about a file, kept apart from what the file
-- itself says. Three reasons it is its own table rather than more columns
-- on track:
--
--   * a wrong answer stays reversible - the raw tags are never overwritten
--     in place, so `enrich reject` restores the file's own reading;
--   * every value carries where it came from and how sure the match was,
--     which is the only way to audit a bad tag or invalidate a stale one;
--   * it is keyed by content_key, not track_id, so moving or rescanning a
--     file keeps its enrichment and costs no further API calls.
CREATE TABLE IF NOT EXISTS enrichment (
    content_key TEXT PRIMARY KEY,
    -- candidate | applied | rejected | skipped | none. The interface
    -- shows four states over these; enrich.STATE_SQL is the mapping.
    status      TEXT NOT NULL,
    source      TEXT NOT NULL,    -- isrc | musicbrainz | backfill
    confidence  REAL NOT NULL,
    mbid        TEXT,             -- MusicBrainz recording id
    release_id  TEXT,             -- MusicBrainz release id, for cover art
    fields      TEXT NOT NULL,    -- JSON: the proposed tag values
    fetched_at  REAL NOT NULL,
    applied_at  REAL,
    -- The content_key the file had when the values were written into it.
    -- Writing tags changes the head of the file and therefore the key, so
    -- without this an applied file looks unenriched on the next scan.
    applied_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_enrich_status ON enrichment(status);

-- The user tier: a hand-typed value outranks every source, and survives a
-- re-run of the enricher. Also keyed by content_key.
CREATE TABLE IF NOT EXISTS track_override (
    content_key TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT,
    set_at      REAL NOT NULL,
    PRIMARY KEY (content_key, field)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER,
    at        REAL NOT NULL,
    kind      TEXT NOT NULL,   -- copy | skip | delete | playlist | error | start | done
    detail    TEXT,
    size      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_log_dev ON sync_log(device_id, id DESC);
"""


# Where the catalog lived when the project was called Hoard. A user who
# already has one keeps using it: the file holds the scanned library, the
# device pairings and the manifest, and moving it silently would be a worse
# outcome than a slightly stale path.
LEGACY_DB = os.path.join(os.path.expanduser("~"), ".hoard", "hoard.db")


def default_db_path():
    env = os.environ.get("LEMONZEST_DB") or os.environ.get("HOARD_DB")
    if env:
        return env
    current = os.path.join(os.path.expanduser("~"), ".lemon-zest", "lemon-zest.db")
    if not os.path.exists(current) and os.path.exists(LEGACY_DB):
        return LEGACY_DB
    return current


def _columns(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def migrate(con):
    """Bring an older catalog up to the current shape.

    Runs before the schema script, because CREATE TABLE IF NOT EXISTS will
    not touch a table that is already there. Each step is guarded by what
    the database actually holds rather than by a stored version number, so
    a half-applied upgrade finishes on the next open.
    """
    track_cols = _columns(con, "track")
    if track_cols and "added_at" not in track_cols:
        con.execute("ALTER TABLE track ADD COLUMN added_at REAL")
        # Every file already in the catalog counts as arrived when it was
        # last seen. Backdating them all to now would put an existing
        # library in the inbox, which is exactly what the inbox is not for.
        con.execute("UPDATE track SET added_at = seen_at WHERE added_at IS NULL")

    root_cols = _columns(con, "library_root")
    if root_cols and "hidden" not in root_cols:
        con.execute("ALTER TABLE library_root ADD COLUMN hidden "
                    "INTEGER NOT NULL DEFAULT 0")

    cols = _columns(con, "device")
    if cols:
        if "hoard_id" in cols and "device_uid" not in cols:
            con.execute("ALTER TABLE device RENAME COLUMN hoard_id TO device_uid")
        if "playlist_template" not in cols:
            con.execute("ALTER TABLE device ADD COLUMN playlist_template "
                        "TEXT NOT NULL DEFAULT '{name}.m3u8'")
    con.commit()


def connect(path=None):
    path = path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    migrate(con)
    con.executescript(SCHEMA)
    # A catalog scanned before library_root existed knows its folders only
    # through the tracks in them. Adopt those, once.
    con.execute(
        "INSERT INTO library_root(root, added_at, scanned_at) "
        "SELECT DISTINCT root, NULL, NULL FROM track "
        "WHERE root NOT IN (SELECT root FROM library_root)"
    )
    con.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def add_root(con, root, scanned=False):
    """Remember ``root`` as a library folder, whether or not it holds music."""
    import time

    from .paths import norm

    root = norm(os.path.abspath(root))
    now = time.time()
    con.execute(
        "INSERT INTO library_root(root, added_at, scanned_at) VALUES (?,?,?) "
        "ON CONFLICT(root) DO UPDATE SET scanned_at = "
        "COALESCE(excluded.scanned_at, library_root.scanned_at)",
        (root, now, now if scanned else None),
    )
    con.commit()
    return root


def roots(con, include_hidden=True):
    """Every library folder, registered or merely inferred from its tracks."""
    rows = con.execute(
        "SELECT root FROM library_root "
        "UNION SELECT DISTINCT root FROM track ORDER BY root")
    out = [r["root"] for r in rows]
    if include_hidden:
        return out
    hidden = hidden_roots(con)
    return [r for r in out if r not in hidden]


def hidden_roots(con):
    """Folders the user has hidden. Their tracks stay in the catalog."""
    return {r["root"] for r in con.execute(
        "SELECT root FROM library_root WHERE hidden = 1")}


def roots_detail(con):
    """Every library folder with what the catalog knows about it."""
    known = {r["root"]: r for r in con.execute("SELECT * FROM library_root")}
    out = []
    for root in roots(con):
        row = known.get(root)
        counts = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM track WHERE root=?",
            (root,)).fetchone()
        out.append({
            "root": root,
            "tracks": counts["n"],
            "bytes": counts["b"],
            "hidden": bool(row["hidden"]) if row else False,
            "scanned_at": row["scanned_at"] if row else None,
            "registered": row is not None,
        })
    return out


def set_root_hidden(con, root, hidden):
    """Hide or unhide a library folder. Nothing on disk is touched."""
    from .paths import norm

    root = norm(os.path.abspath(root))
    # A folder known only through its tracks has no row to flag yet.
    con.execute(
        "INSERT INTO library_root(root, added_at, scanned_at, hidden) "
        "VALUES (?, NULL, NULL, ?) "
        "ON CONFLICT(root) DO UPDATE SET hidden = excluded.hidden",
        (root, 1 if hidden else 0),
    )
    con.commit()
    return root


def remove_root(con, root, forget_tracks=True):
    """Drop a library folder from the catalog.

    The audio files are left exactly where they are: this forgets rows, it
    does not delete music. Playlist entries pointing at forgotten tracks go
    back to unmatched rather than disappearing, which is what the schema's
    ON DELETE SET NULL already does.
    """
    from .paths import norm

    root = norm(os.path.abspath(root))
    removed = 0
    if forget_tracks:
        removed = con.execute(
            "SELECT COUNT(*) FROM track WHERE root=?", (root,)).fetchone()[0]
        con.execute("DELETE FROM track WHERE root=?", (root,))
    con.execute("DELETE FROM library_root WHERE root=?", (root,))
    con.commit()
    return {"root": root, "tracks_removed": removed}


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con, key, value):
    con.execute(
        "INSERT INTO meta(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()


def log(con, device_id, kind, detail=None, size=None):
    import time

    con.execute(
        "INSERT INTO sync_log(device_id, at, kind, detail, size) VALUES (?,?,?,?,?)",
        (device_id, time.time(), kind, detail, size),
    )
