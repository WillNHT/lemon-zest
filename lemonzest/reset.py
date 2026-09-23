"""Start the library over: forget the catalog, and optionally the files.

The one destructive thing in the program, so it is kept small and says
exactly what it removes. What goes:

  * every track row, every playlist and its entries, the enrichment and the
    hand-typed overrides keyed to those files, and the sync list;
  * the yt-dlp download archive in each library folder, which is what makes
    yt-dlp skip a video it has fetched before. Without removing it a reset
    library could never be downloaded again;
  * with ``delete_files``, the catalogued audio files themselves, the
    playlist files written beside the library, and any folder that is left
    empty by that.

What stays: the library folders (so a download still has somewhere to land),
the paired devices, the download settings, and the list of URLs asked for -
the download page keeps those so the same music can be fetched again with a
click.
"""
import os
import time

from . import db as db_mod
from . import download as dl_mod
from . import playlists as pl_mod
from .paths import resolve_existing

CONFIRM_WORD = "RESET"


def summary(con):
    """What a reset would remove, for the confirmation to show."""
    one = lambda q: con.execute(q).fetchone()[0]
    roots = db_mod.roots(con)
    return {
        "tracks": one("SELECT COUNT(*) FROM track"),
        "bytes": one("SELECT COALESCE(SUM(size),0) FROM track"),
        "playlists": one("SELECT COUNT(*) FROM playlist"),
        "urls": one("SELECT COUNT(*) FROM download_url"),
        "roots": roots,
        "archives": [r for r in roots
                     if os.path.isfile(os.path.join(r, dl_mod.ARCHIVE_NAME))],
    }


def _remove(path, errors):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        errors.append(f"{path}: {exc}")
        return False


def _prune_empty(root, errors):
    """Remove folders under ``root`` that hold nothing. Never ``root``."""
    removed = 0
    for here, _dirs, _files in os.walk(root, topdown=False):
        if os.path.normcase(os.path.abspath(here)) == \
                os.path.normcase(os.path.abspath(root)):
            continue
        try:
            if not os.listdir(here):
                os.rmdir(here)
                removed += 1
        except OSError as exc:
            errors.append(f"{here}: {exc}")
    return removed


def reset_library(con, delete_files=False):
    """Wipe the library, its playlists and the download history.

    Returns counts of what went, plus any file that could not be removed.
    A file that cannot be deleted does not stop the rest: the catalog is
    cleared regardless, and a rescan will bring the survivor back.
    """
    errors = []
    roots = db_mod.roots(con)
    files_deleted = 0
    playlist_files_deleted = 0
    folders_removed = 0

    if delete_files:
        for row in con.execute("SELECT path FROM track").fetchall():
            found = resolve_existing(row["path"])
            if found and _remove(found, errors):
                files_deleted += 1
        names = {pl_mod.safe_filename(r["name"])
                 for r in con.execute("SELECT name FROM playlist")}
        for root in roots:
            # Beside the library only what the catalog made: that folder is
            # shared with whatever else sits next to the library.
            for folder, only in ((pl_mod.local_dir(root), names),
                                 (os.path.join(root, pl_mod.LEGACY_DIR), None)):
                for name in pl_mod.list_playlist_files(folder):
                    if (only is None or name in only) and \
                            _remove(os.path.join(folder, name), errors):
                        playlist_files_deleted += 1
                        # and its cover, which is named after it
                        _remove(os.path.join(folder, os.path.splitext(name)[0]
                                             + ".jpg"), errors)

    archives_deleted = 0
    for root in roots:
        if _remove(os.path.join(root, dl_mod.ARCHIVE_NAME), errors):
            archives_deleted += 1

    if delete_files:
        for root in roots:
            if os.path.isdir(root):
                folders_removed += _prune_empty(root, errors)

    tracks = con.execute("SELECT COUNT(*) FROM track").fetchone()[0]
    playlists = con.execute("SELECT COUNT(*) FROM playlist").fetchone()[0]
    con.execute("DELETE FROM playlist_entry")
    con.execute("DELETE FROM playlist")
    con.execute("DELETE FROM track")
    con.execute("DELETE FROM enrichment")
    con.execute("DELETE FROM track_override")
    con.execute("DELETE FROM sync_list")
    # The URLs stay, but nothing they fed exists any more.
    con.execute("UPDATE download_url SET last_added = 0, last_checked = NULL")
    con.execute("UPDATE library_root SET scanned_at = NULL")
    con.execute("DELETE FROM meta WHERE key = 'inbox_seen_at'")
    con.commit()
    db_mod.meta_set(con, "library_reset_at", time.time())

    return {
        "tracks_forgotten": tracks,
        "playlists_forgotten": playlists,
        "files_deleted": files_deleted,
        "playlist_files_deleted": playlist_files_deleted,
        "folders_removed": folders_removed,
        "archives_deleted": archives_deleted,
        "errors": errors[:50],
        "error_count": len(errors),
    }
