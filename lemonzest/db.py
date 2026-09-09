"""SQLite catalog. Single writer, WAL, schema created on demand."""
import os
import sqlite3

SCHEMA_VERSION = 3

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
    seen_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_track_isrc   ON track(isrc);
CREATE INDEX IF NOT EXISTS ix_track_artist ON track(artist);
CREATE INDEX IF NOT EXISTS ix_track_album  ON track(album);
CREATE INDEX IF NOT EXISTS ix_track_root   ON track(root);

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
    status      TEXT NOT NULL,    -- candidate | applied | rejected | none
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
    cols = _columns(con, "device")
    if not cols:
        return
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
    con.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def log(con, device_id, kind, detail=None, size=None):
    import time

    con.execute(
        "INSERT INTO sync_log(device_id, at, kind, detail, size) VALUES (?,?,?,?,?)",
        (device_id, time.time(), kind, detail, size),
    )
