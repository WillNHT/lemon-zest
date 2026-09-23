"""Moving a library to another computer, or to another folder on this one.

The catalog stores absolute paths, because a path has to have one spelling
to be comparable - and so a library copied to a machine where it lives at
``C:/Users/Remote/Music/library`` instead of ``C:/Users/Bob/Music/lemon-zest``
finds none of its files. Three pieces put that right:

  * ``relocate`` rewrites every stored path under one folder to another. It
    is the whole of "I moved my library", on this machine or any other.
  * ``pack`` keeps a copy of the catalog *inside* the library folder, at
    ``.lemon-zest/catalog.db``, refreshed after every scan and download. So
    the folder is the thing to copy: the music, the playlists beside it, the
    download archive, the part-downloaded files, and the history of all of
    it travel together. Credentials are left out of the copy.
  * ``unpack`` takes that copy on the other machine, and relocates it to
    wherever the folder landed. This machine keeps its own credentials.
"""
import json
import os
import sqlite3
import time

from . import db as db_mod
from .paths import norm

PACK_DIR = ".lemon-zest"      # a dot folder: the scanner never walks into it
PACK_NAME = "catalog.db"

# Settings that belong to a machine or a person, not to a library: where a
# browser profile or a cookies file sits here, and the keys to somebody's
# accounts. Never written into a pack, never taken from one.
PRIVATE = ("download.cookies_file", "download.firefox_profile",
           "download.cookies_mode", "enrich.acoustid_key", "enrich.contact")


def pack_path(root):
    return os.path.join(root, PACK_DIR, PACK_NAME)


def _canon(path):
    return norm(os.path.abspath(path)).rstrip("/")


# (table, column, extra condition). Every column that holds an absolute
# path under a library folder. Keyed columns (a primary key or UNIQUE) are
# rewritten with OR IGNORE and whatever collides is dropped afterwards.
_PATHS = (
    ("track", "root", ""),
    ("track", "path", ""),
    ("library_root", "root", ""),
    ("playlist_entry", "raw_path", ""),
    ("playlist", "imported_from", ""),
    ("download_url", "root", ""),
    ("sync_list", "ref", "AND kind = 'track'"),
    ("device_set", "ref", "AND kind = 'track'"),
    ("media", "root", ""),
)
_KEYED = {"library_root", "sync_list", "device_set"}


def relocate(con, old, new):
    """Point everything the catalog keeps under ``old`` at ``new``.

    Nothing on disk moves: this is for after the folder has been moved or
    copied. Returns how many rows each table had rewritten.
    """
    old = _canon(old)
    stored = {r.rstrip("/").casefold(): r.rstrip("/")
              for r in db_mod.roots(con)}
    old = stored.get(old.casefold(), old)
    new = _canon(new)
    counts = {}
    if old == new:
        return counts
    n = len(old)
    where = "({c} = ? OR substr({c}, 1, %d) = ?)" % (n + 1)
    args = [old, old + "/"]
    # Rows already scanned at the new place are the same files again: the
    # older rows carry the history (playlists, sync rules), so they win.
    con.execute(
        "DELETE FROM track WHERE path IN (SELECT ? || substr(path, %d) "
        "FROM track WHERE %s)" % (n + 1, where.format(c="path")),
        [new] + args)
    for table, col, extra in _PATHS:
        cur = con.execute(
            "UPDATE OR IGNORE %s SET %s = ? || substr(%s, %d) WHERE %s %s"
            % (table, col, col, n + 1, where.format(c=col), extra),
            [new] + args)
        counts["%s.%s" % (table, col)] = cur.rowcount
        if table in _KEYED:
            con.execute("DELETE FROM %s WHERE %s %s"
                        % (table, where.format(c=col), extra), args)
    # Entries left pointing at nothing by the clean-up above find their
    # file again by path.
    con.execute(
        "UPDATE playlist_entry SET track_id = (SELECT t.id FROM track t "
        "WHERE t.path = playlist_entry.raw_path) WHERE track_id IS NULL "
        "AND raw_path IN (SELECT path FROM track)")

    def moved(value):
        v = (value or "").rstrip("/")
        if v == old or v.startswith(old + "/"):
            return new + v[n:]
        return value

    root = db_mod.meta_get(con, "download.root")
    if root:
        db_mod.meta_set(con, "download.root", moved(norm(root)))
    paused = json.loads(db_mod.meta_get(con, "paused_downloads") or "[]")
    for rec in paused:
        if rec.get("root"):
            rec["root"] = moved(norm(rec["root"]))
    db_mod.meta_set(con, "paused_downloads", json.dumps(paused))
    con.commit()
    return counts


def pack(con, root):
    """Write a copy of the catalog into ``root``, without anyone's keys.

    A consistent copy - SQLite's own backup, so a download writing to the
    catalog at the same moment is not caught half way - written to a temp
    name and swapped in. Returns the path written.
    """
    root = _canon(root)
    dest = pack_path(root)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = sqlite3.connect(tmp)
    try:
        con.backup(out)
        out.executemany("DELETE FROM meta WHERE key = ?",
                        [(k,) for k in PRIVATE])
        out.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [("portable.root", root), ("portable.packed_at", str(time.time()))])
        out.commit()
    finally:
        out.close()
    os.replace(tmp, dest)
    return norm(dest)


def pack_all(con):
    """Refresh the copy in every library folder that is here to write to."""
    out = []
    for root in db_mod.roots(con):
        if os.path.isdir(root):
            out.append(pack(con, root))
    return out


def packed(root):
    """When the copy in ``root`` was made, or None."""
    try:
        return os.path.getmtime(pack_path(root))
    except OSError:
        return None


def unpack(con, folder, replace=False):
    """Take in a library copied from another computer.

    ``folder`` is where the library folder is on this machine. Its packed
    catalog replaces this one - refused while this one holds tracks, unless
    ``replace`` - and is relocated from where the folder was to where it is.
    This machine's own credentials and settings for them are kept.
    """
    folder = _canon(folder)
    source = pack_path(folder)
    if not os.path.isfile(source):
        raise ValueError("no library copy in %s - it is made in the %s "
                         "folder inside a library" % (folder, PACK_DIR))
    tracks = con.execute("SELECT COUNT(*) FROM track").fetchone()[0]
    if tracks and not replace:
        raise ValueError("this catalog already holds %d tracks; replacing "
                         "it has to be asked for" % tracks)
    mine = {r["key"]: r["value"] for r in con.execute(
        "SELECT key, value FROM meta WHERE key IN (%s)"
        % ",".join("?" * len(PRIVATE)), PRIVATE)}
    con.commit()
    src = sqlite3.connect(source)
    try:
        src.backup(con)
    finally:
        src.close()
    # A copy made by an older version is brought up to this one.
    db_mod.migrate(con)
    con.executescript(db_mod.SCHEMA)
    was = db_mod.meta_get(con, "portable.root")
    counts = relocate(con, was, folder) if was else {}
    for key, value in mine.items():
        db_mod.meta_set(con, key, value)
    missing = [r for r in db_mod.roots(con) if not os.path.isdir(r)]
    return {
        "from": was, "to": folder, "moved": counts,
        "tracks": con.execute("SELECT COUNT(*) FROM track").fetchone()[0],
        "missing_roots": missing,
    }
