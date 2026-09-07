"""SQLite catalog. Single writer, WAL, schema created on demand."""
import os
import sqlite3

SCHEMA_VERSION = 1

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
    hoard_id      TEXT NOT NULL UNIQUE,  -- uuid in .hoard-id at the device root
    label         TEXT,                  -- volume label, the everyday lookup
    name          TEXT NOT NULL,
    root          TEXT,                  -- last known mount point
    profile       TEXT NOT NULL DEFAULT 'ums',
    music_dir     TEXT NOT NULL DEFAULT 'Music',
    playlist_dir  TEXT NOT NULL DEFAULT 'Music',
    path_template TEXT NOT NULL DEFAULT '{album_artist}/{album}/{track:02d} {title}{ext}',
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

-- Observed set: what Hoard believes is physically on the card.
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

-- Playlist files Hoard has written to a device, so that unticking a
-- playlist removes its file instead of leaving a stale one behind.
CREATE TABLE IF NOT EXISTS device_playlist (
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,       -- relative to the device's playlist dir
    name       TEXT NOT NULL,
    entries    INTEGER,
    written_at REAL,
    PRIMARY KEY (device_id, filename)
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


def default_db_path():
    return os.environ.get("HOARD_DB") or os.path.join(
        os.path.expanduser("~"), ".hoard", "hoard.db"
    )


def connect(path=None):
    path = path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
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
