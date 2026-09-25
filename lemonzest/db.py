"""SQLite catalog. Single writer, WAL, schema created on demand."""
import os
import sqlite3
import time

SCHEMA_VERSION = 5

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
    year         TEXT,                   -- the year alone: Rockbox shows it verbatim
    date         TEXT,                   -- the full release date, ISO, when known
    genre        TEXT,
    isrc         TEXT,
    purl         TEXT,                   -- yt-dlp source URL
    -- "youtube:<video id>", off purl: who this file is for life, whatever
    -- it is renamed or re-tagged to. The key into media below.
    source_id    TEXT,
    codec        TEXT,
    bitrate      INTEGER,
    sample_rate  INTEGER,
    seen_at      REAL NOT NULL,
    -- When the catalog first saw this file. Distinct from seen_at, which
    -- every rescan moves forward: the inbox is "what arrived since I last
    -- looked", and that question needs a timestamp that does not change.
    added_at     REAL,
    -- When this file stopped being new. Written by the promotion rule
    -- below rather than by a person: see promote_inbox.
    inbox_done_at REAL,
    -- When what the catalog holds for this file last changed: its tags,
    -- its bytes or its place. Kept by the trigger below rather than by each
    -- writer, so a scan, an enrichment, a hand edit and a tag write all
    -- count without any of them having to remember to.
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_track_isrc   ON track(isrc);
CREATE INDEX IF NOT EXISTS ix_track_artist ON track(artist);
CREATE INDEX IF NOT EXISTS ix_track_album  ON track(album);
CREATE INDEX IF NOT EXISTS ix_track_root   ON track(root);
CREATE INDEX IF NOT EXISTS ix_track_added  ON track(added_at);
CREATE INDEX IF NOT EXISTS ix_track_source ON track(source_id);

-- Where a downloaded file came from, and what it was when it arrived. The
-- track row is what the file is now - renamed by organise, re-tagged by
-- enrichment or by hand - and this is what it was before any of that: the
-- file yt-dlp wrote, where, and what its tags said. Written once, on first
-- arrival, and never updated, so a later pass can start again from the
-- original rather than from whatever the last one left.
CREATE TABLE IF NOT EXISTS media (
    source_id     TEXT PRIMARY KEY,      -- "youtube:<video id>"
    url           TEXT,                  -- the video itself
    root          TEXT,                  -- library folder it landed in
    initial_path  TEXT,                  -- relative to root, as yt-dlp named it
    initial_key   TEXT,
    initial_tags  TEXT,                  -- JSON: what the file said on arrival
    downloaded_at REAL,
    -- Recorded after the fact, for a file downloaded before this table
    -- existed: the "initial" values are what the catalog held then.
    backfilled    INTEGER NOT NULL DEFAULT 0
);

-- Every URL that asked for a media item. Many to one: the same video in two
-- playlists is one file with two sources, and both are kept.
CREATE TABLE IF NOT EXISTS media_source (
    source_id TEXT NOT NULL,
    url       TEXT NOT NULL,
    first_at  REAL,
    last_at   REAL,
    PRIMARY KEY (source_id, url)
);

CREATE TRIGGER IF NOT EXISTS track_updated AFTER UPDATE ON track
WHEN OLD.path IS NOT NEW.path OR OLD.content_key IS NOT NEW.content_key
  OR OLD.title IS NOT NEW.title OR OLD.artist IS NOT NEW.artist
  OR OLD.album IS NOT NEW.album OR OLD.album_artist IS NOT NEW.album_artist
  OR OLD.track_no IS NOT NEW.track_no OR OLD.disc_no IS NOT NEW.disc_no
  OR OLD.year IS NOT NEW.year OR OLD.date IS NOT NEW.date
  OR OLD.genre IS NOT NEW.genre
  OR OLD.isrc IS NOT NEW.isrc
BEGIN
  UPDATE track SET updated_at = (julianday('now') - 2440587.5) * 86400.0
  WHERE id = NEW.id;
END;

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
    imported_at REAL,
    -- When entries last changed, and when somebody last looked. A playlist
    -- that has gained tracks since it was last opened is worth a mark in
    -- the sidebar; one that has not is not.
    updated_at  REAL,
    seen_at     REAL
);

CREATE TABLE IF NOT EXISTS playlist_entry (
    playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
    pos         INTEGER NOT NULL,
    track_id    INTEGER REFERENCES track(id) ON DELETE SET NULL,
    raw_path    TEXT NOT NULL,     -- as written in the source file
    title_hint  TEXT,              -- from #EXTINF
    duration    REAL,
    -- When this entry joined the playlist. Carried across a re-import, so a
    -- rescan of a folder does not make every track look new again.
    added_at    REAL,
    source_uri  TEXT,              -- per-track provider URI, e.g. apple_music:track:N
    PRIMARY KEY (playlist_id, pos)
);
CREATE INDEX IF NOT EXISTS ix_pe_track ON playlist_entry(track_id);

-- Every URL a download was asked for, and what was at it. Two jobs in one
-- table: the recent list, which is only the last few rows by time, and the
-- kept ones - a playlist URL somebody asked to keep an eye on, which is
-- shown by name and fetched again on demand.
CREATE TABLE IF NOT EXISTS download_url (
    url           TEXT PRIMARY KEY,
    title         TEXT,
    uploader      TEXT,
    is_playlist   INTEGER NOT NULL DEFAULT 0,
    item_count    INTEGER,
    -- The catalog playlist this URL feeds, when it is a playlist. Named
    -- rather than referenced by id: a playlist that is deleted and made
    -- again under the same name is the same playlist to a person.
    playlist_name TEXT,
    root          TEXT,          -- the library folder it was fetched into
    first_used    REAL,
    last_used     REAL,
    uses          INTEGER NOT NULL DEFAULT 0,
    -- Kept on the page and fetched again on demand.
    -- Strictly increasing, one per asking. Time alone cannot order these:
    -- two downloads started inside the same clock tick - which on Windows
    -- is fifteen milliseconds wide - carry the same timestamp, and the
    -- recent list would then show them in whatever order the query felt
    -- like.
    seq           INTEGER NOT NULL DEFAULT 0,
    kept          INTEGER NOT NULL DEFAULT 0,
    last_checked  REAL,
    last_added    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_dlurl_used ON download_url(last_used);

-- A set of music prepared before there is anywhere to put it. The same
-- (kind, ref) rules a device carries, held against no device at all, so a
-- card can be planned on the train and written when you get home. Applying
-- one copies its rules onto a device; the list itself is not consumed.
CREATE TABLE IF NOT EXISTS sync_list (
    kind     TEXT NOT NULL,        -- playlist | artist | album | track
    ref      TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (kind, ref)
);

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

-- A work: one song held as several files - the album cut, the holiday
-- album, the "best of", the same video fetched twice. One file is the
-- master and every reference resolves to it; the rest are kept, untouched,
-- as a record of what was fetched. Keyed by content_key like the override
-- and enrichment tables, so a move or a rescan does not break the group.
CREATE TABLE IF NOT EXISTS work (
    id         INTEGER PRIMARY KEY,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS work_member (
    content_key TEXT PRIMARY KEY,
    work_id     INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
    is_master   INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,        -- bytes | source | isrc | mbid | fuzzy | manual
    joined_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_wm_work ON work_member(work_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wm_master ON work_member(work_id)
    WHERE is_master;

-- "These two are not the same song", so a turned-down suggestion stays
-- turned down. key_a < key_b.
CREATE TABLE IF NOT EXISTS dup_dismissed (
    key_a TEXT NOT NULL,
    key_b TEXT NOT NULL,
    at    REAL,
    PRIMARY KEY (key_a, key_b)
);

-- Every track id beside the id of the file that stands for it: its work's
-- master, or itself. Resolved on read rather than by rewriting references,
-- so a playlist still says which version it was made from and an unmerge
-- loses nothing.
CREATE VIEW IF NOT EXISTS track_canon AS
SELECT t.id AS id,
       COALESCE((SELECT MIN(mt.id) FROM work_member v
                 JOIN work_member mm ON mm.work_id = v.work_id AND mm.is_master
                 JOIN track mt ON mt.content_key = mm.content_key
                 WHERE v.content_key = t.content_key), t.id) AS canon_id
FROM track t;

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

    if track_cols and "date" not in track_cols:
        con.execute("ALTER TABLE track ADD COLUMN date TEXT")

    if track_cols and "source_id" not in track_cols:
        con.execute("ALTER TABLE track ADD COLUMN source_id TEXT")
        # purl is what yt-dlp writes: https://www.youtube.com/watch?v=<id>.
        con.execute(
            "UPDATE track SET source_id = 'youtube:' || "
            "substr(purl, instr(purl, 'watch?v=') + 8, 11) "
            "WHERE instr(purl, 'youtube.com/watch?v=') > 0")

    if track_cols and "updated_at" not in track_cols:
        con.execute("ALTER TABLE track ADD COLUMN updated_at REAL")
        # The best record there is of the last change: an applied match or
        # a typed value, whichever came last, else when the file arrived.
        con.execute(
            "UPDATE track SET updated_at = MAX(COALESCE(added_at, seen_at), "
            "COALESCE((SELECT applied_at FROM enrichment e "
            "  WHERE e.content_key = track.content_key), 0), "
            "COALESCE((SELECT MAX(set_at) FROM track_override o "
            "  WHERE o.content_key = track.content_key), 0))")

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
    if track_cols and "inbox_done_at" not in track_cols:
        con.execute("ALTER TABLE track ADD COLUMN inbox_done_at REAL")
        # Everything already in the catalog has been lived with; only what
        # arrives from here on gets to be new.
        con.execute("UPDATE track SET inbox_done_at = ? WHERE added_at IS NULL "
                    "OR added_at <= COALESCE((SELECT CAST(value AS REAL) "
                    "FROM meta WHERE key = 'inbox_seen_at'), 0)",
                    (time.time(),))
    # A playlist made by an early download knows nothing about where it came
    # from - the URL was written down beside it but never on it. Joined up
    # once, here, so those playlists stop reading as local.
    pl_cols = _columns(con, "playlist")
    if pl_cols and "updated_at" not in pl_cols:
        con.execute("ALTER TABLE playlist ADD COLUMN updated_at REAL")
        con.execute("ALTER TABLE playlist ADD COLUMN seen_at REAL")
        # Everything already here has been lived with: marking a library's
        # worth of playlists as new the first time this runs would be a
        # notification about nothing.
        con.execute("UPDATE playlist SET updated_at = imported_at, "
                    "seen_at = ?", (time.time(),))
    pe_cols = _columns(con, "playlist_entry")
    if pe_cols and "added_at" not in pe_cols:
        con.execute("ALTER TABLE playlist_entry ADD COLUMN added_at REAL")
        con.execute("UPDATE playlist_entry SET added_at = "
                    "(SELECT imported_at FROM playlist p "
                    " WHERE p.id = playlist_entry.playlist_id)")
    dl_cols = _columns(con, "download_url")
    if dl_cols and _columns(con, "playlist"):
        con.execute(
            "UPDATE playlist SET source_uri = (SELECT d.url FROM download_url d "
            "  WHERE d.playlist_name = playlist.name AND d.url IS NOT NULL "
            "  ORDER BY d.seq DESC LIMIT 1) "
            "WHERE source_uri IS NULL AND name IN "
            "  (SELECT playlist_name FROM download_url "
            "   WHERE playlist_name IS NOT NULL)")
        con.execute(
            "UPDATE playlist SET origin = 'youtube' "
            "WHERE origin IN ('local', 'download') AND ("
            "  source_uri LIKE '%youtube.com%' OR source_uri LIKE '%youtu.be%')")

    # A YouTube Music album or EP used to be made into a playlist. It is a
    # release, not a list anybody made, so those go - the tracks stay, and
    # a device carrying one drops its playlist file on the next sync.
    if pl_cols:
        con.execute("DELETE FROM playlist "
                    "WHERE instr(source_uri, 'list=OLAK5uy_') > 0")
    if dl_cols:
        con.execute("UPDATE download_url SET playlist_name = NULL "
                    "WHERE instr(url, 'list=OLAK5uy_') > 0")

    # A YouTube category is not a genre (see meta.NOT_GENRES); a catalog
    # that stored one treats it as the blank it is, so a lookup fills it.
    if track_cols:
        from .meta import NOT_GENRES
        con.execute("UPDATE track SET genre = NULL WHERE LOWER(genre) IN (%s)"
                    % ",".join("?" * len(NOT_GENRES)), sorted(NOT_GENRES))

    # Typing a value used to mark a file enriched at 1.00 with nothing
    # matched behind it. Those rows go back to raw; the typed values live in
    # track_override and are untouched.
    if _columns(con, "enrichment"):
        con.execute("DELETE FROM enrichment WHERE source = 'manual' "
                    "AND status = 'applied' AND confidence = 1.0 "
                    "AND fields = '{}' AND mbid IS NULL")

    if dl_cols and "seq" not in dl_cols:
        con.execute("ALTER TABLE download_url ADD COLUMN seq INTEGER NOT NULL "
                    "DEFAULT 0")
        # Existing rows keep the order their timestamps imply.
        con.execute("UPDATE download_url SET seq = rowid")
    con.commit()


# How long a file stays new when nothing ever happens to it. A track that
# is never identified - no network, no key, an unknown recording - must
# still leave the inbox eventually, or the inbox becomes a list of things
# that will be there forever.
INBOX_SETTLE_AGE = 24 * 3600

# The enrichment states that count as settled. Applied, skipped and
# rejected are decisions; "none" is one too - the lookup ran and found
# nothing, and nothing further will happen on its own. A file that wants
# tags it cannot get belongs on the problems page, not in the inbox
# forever. Raw and candidate are the unsettled ones: raw has not been
# looked at yet, and a candidate is waiting on a decision, which is exactly
# what an inbox is for.
INBOX_SETTLED = ("applied", "skipped", "rejected", "none")


def promote_inbox(con, now=None, settle_age=INBOX_SETTLE_AGE):
    """Take out of the inbox everything that has finished arriving.

    The inbox used to empty only when somebody pressed a button, which made
    it a chore rather than a view: the files had been identified and tagged
    minutes after they landed, and the badge went on saying 40 until it was
    dismissed by hand.

    A file leaves when there is nothing left to happen to it:

      * its identification has settled - identified, skipped or rejected -
        and it has content. A candidate waiting on a decision stays, because
        that decision is exactly what an inbox is for;
      * or it is older than ``settle_age`` whatever its state, so a library
        with no network behind it still drains.

    Empty files never promote on the first rule: zero bytes on disk is the
    one thing that always wants looking at. The age rule takes them in the
    end, by which time they are on the problems page instead.

    Returns how many files were promoted.
    """
    now = time.time() if now is None else now
    placeholders = ",".join("?" * len(INBOX_SETTLED))
    cur = con.execute(
        "UPDATE track SET inbox_done_at = ? "
        "WHERE inbox_done_at IS NULL AND ("
        "  (size > 0 AND content_key IN ("
        f"     SELECT content_key FROM enrichment WHERE status IN ({placeholders})))"
        "  OR (added_at IS NOT NULL AND added_at < ?))",
        [now] + list(INBOX_SETTLED) + [now - settle_age])
    con.commit()
    return cur.rowcount


def clear_inbox(con, now=None):
    """Empty the inbox by hand: everything indexed so far stops being new."""
    now = time.time() if now is None else now
    cur = con.execute("UPDATE track SET inbox_done_at = ? "
                      "WHERE inbox_done_at IS NULL", (now,))
    meta_set(con, "inbox_seen_at", now)
    con.commit()
    return cur.rowcount


def path_of(con):
    """The database file a connection is open on.

    A worker thread cannot share a connection - sqlite3 refuses one used
    from the thread it was not made on - so a thread that needs its own asks
    the connection it was handed where to open it. Returns None for an
    in-memory database, which nothing can reopen.
    """
    for row in con.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or None
    return None


def connect(path=None, same_thread=True):
    """Open the catalog.

    ``same_thread=False`` is for the identification queue, whose connection
    is built on whichever thread first asks for it and then used by the
    worker. sqlite3's check is a guard against two threads using one
    connection at once, and the queue already guarantees that on its own -
    there is one worker, and the one caller that borrows the queue's client
    does so with the worker held back.
    """
    path = path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0, check_same_thread=same_thread)
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
    # Files downloaded before media existed get a record now, from what the
    # catalog holds - the best "as it arrived" there is for them. Once.
    if meta_get(con, "media_backfilled") is None:
        con.execute(
            "INSERT OR IGNORE INTO media(source_id, url, root, initial_path, "
            "initial_key, downloaded_at, backfilled) "
            "SELECT source_id, purl, root, rel_path, content_key, added_at, 1 "
            "FROM track WHERE source_id IS NOT NULL")
        con.execute(
            "INSERT OR IGNORE INTO media_source(source_id, url, first_at, "
            "last_at) SELECT DISTINCT t.source_id, p.source_uri, "
            "p.imported_at, p.imported_at FROM playlist_entry e "
            "JOIN playlist p ON p.id = e.playlist_id "
            "JOIN track t ON t.id = e.track_id "
            "WHERE t.source_id IS NOT NULL AND p.source_uri IS NOT NULL")
        meta_set(con, "media_backfilled", time.time())
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
            # Not there: moved, renamed, or a catalog brought from another
            # computer. The interface offers to point it somewhere.
            "exists": os.path.isdir(root),
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
