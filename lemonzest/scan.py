"""Library scanner: walk a root, extract metadata, upsert into the catalog.

Read-only with respect to the music files. Nothing here writes to disk
outside the database.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .meta import is_audio, probe
from .paths import norm

TRACK_FIELDS = (
    "size mtime content_key ext duration title artist album album_artist "
    "track_no disc_no year genre isrc purl codec bitrate sample_rate"
).split()


def walk_audio(root):
    """Yield absolute paths of audio files under ``root``."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if is_audio(fn):
                yield os.path.join(dirpath, fn)


def _upsert(con, root, npath, rec, now):
    """Write one probed file into the catalog.

    ``added_at`` is written on insert and never updated: it is what the
    inbox reads, and a rescan re-probing an edited file must not make that
    file new again.
    """
    rel = norm(os.path.relpath(npath, root))
    cols = ["path", "rel_path", "root", "seen_at", "added_at"] + TRACK_FIELDS
    vals = [npath, rel, root, now, now] + [rec[f] for f in TRACK_FIELDS]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols
                       if c not in ("path", "added_at"))
    con.execute(
        f"INSERT INTO track({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}",
        vals,
    )


def index_paths(con, root, paths):
    """Index a known handful of files without walking the library.

    A download produces two or three files; finding them again by walking a
    2,300-file library is work nobody asked for. Same probe, same columns,
    same conflict handling as ``scan`` - only the set of files differs.

    A file outside ``root`` is counted as failed rather than indexed: the
    catalog stores a path relative to a library root, and a row claiming a
    root that does not contain it would go stale on the next scan of that
    root, which deletes what it did not find.

    Returns ``(counts, track_ids)``, the ids in the order the files were
    given, so a caller can put them straight into a playlist.
    """
    root = norm(os.path.abspath(root))
    now = time.time()
    counts = {"added": 0, "updated": 0, "failed": 0}
    ids = []
    for path in paths:
        npath = norm(os.path.abspath(path))
        if (not is_audio(npath) or not os.path.isfile(path)
                or not npath.startswith(root.rstrip("/") + "/")):
            counts["failed"] += 1
            continue
        existing = con.execute(
            "SELECT id FROM track WHERE path=?", (npath,)).fetchone()
        try:
            rec = probe(path)
        except Exception:
            counts["failed"] += 1
            continue
        _upsert(con, root, npath, rec, now)
        counts["updated" if existing else "added"] += 1
        row = con.execute("SELECT id FROM track WHERE path=?", (npath,)).fetchone()
        if row:
            ids.append(row["id"])
    con.commit()
    return counts, ids


def scan(con, root, workers=8, progress=None, full=False):
    """Index every audio file under ``root``.

    Files whose size and mtime are unchanged are skipped without reading
    tags, so a rescan of an untouched library is fast. ``full=True`` forces
    a re-read of everything.

    Returns a dict of counts.
    """
    root = norm(os.path.abspath(root))
    now = time.time()
    counts = {"seen": 0, "added": 0, "updated": 0, "unchanged": 0,
              "removed": 0, "failed": 0}

    # Registered before the walk, so a folder with no music in it yet is
    # still a library folder afterwards - that is how a library starts.
    db.add_root(con, root, scanned=True)

    known = {
        r["path"]: (r["size"], r["mtime"], r["id"])
        for r in con.execute(
            "SELECT id, path, size, mtime FROM track WHERE root = ?", (root,)
        )
    }
    present = set()
    files = list(walk_audio(root))

    def work(path):
        npath = norm(os.path.abspath(path))
        try:
            st = os.stat(path)
        except OSError:
            return npath, None, "failed"
        prev = known.get(npath)
        if prev and not full and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1e-6:
            return npath, None, "unchanged"
        try:
            return npath, probe(path), "changed"
        except Exception:
            return npath, None, "failed"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (npath, rec, state) in enumerate(pool.map(work, files), 1):
            present.add(npath)
            counts["seen"] += 1
            if state == "failed":
                counts["failed"] += 1
            elif state == "unchanged":
                counts["unchanged"] += 1
                con.execute("UPDATE track SET seen_at=? WHERE path=?", (now, npath))
            else:
                _upsert(con, root, npath, rec, now)
                counts["added" if npath not in known else "updated"] += 1
            if progress and i % 25 == 0:
                progress(i, len(files))

    gone = set(known) - present
    for path in gone:
        con.execute("DELETE FROM track WHERE path=?", (path,))
    counts["removed"] = len(gone)

    con.commit()
    if progress:
        progress(len(files), len(files))
    return counts
