"""Move library files so the folders match the metadata.

Enrichment fixes what the *catalog* believes. This fixes what is on disk.
They are separate on purpose: the catalog can be rebuilt from a rescan and a
wrong answer costs nothing, whereas moving two thousand files is the most
destructive thing this program can be asked to do. So it plans by default,
asks before it acts, and writes a journal that puts everything back.

The case it exists for is a folder named

    Joji, Kurtis McKenzie, Linden Jay, Chelsea Lena, George Miller,
    Joshua Bliss Taffel, Kacy Anne Hill/Nectar/

because an art track's ``artist`` tag is every credited writer, and yt-dlp
had nothing better to build a path from. The catalog now says "Joji"; this
makes the disk agree.

The default template changes **folders only** and keeps every filename
exactly as it is. That is deliberately the smallest thing that fixes the
complaint: renaming files as well is a much larger diff for a library whose
two halves follow different conventions - one has track numbers in the
filename, one does not - so it is available with ``--template`` and is not
what you get by accident.
"""
import json
import os
import shutil
import time

from .paths import norm, render_template, resolve_existing

# Folders from the metadata, filename untouched.
DEFAULT_TEMPLATE = "{album_artist}/{album}/{filename}"

JOURNAL_PREFIX = "organise-"


class OrganiseError(RuntimeError):
    """The move did not happen. Nothing on disk was changed."""


# What render_template substitutes when a field is empty. A destination
# containing one of these is not knowledge about where the file belongs; it
# is the absence of it, spelled out.
PLACEHOLDERS = ("Unknown", "Unknown Album", "Unknown Artist")


def _has_name(row):
    """Whether this track knows enough to be filed under an artist."""
    return bool((row["album_artist"] or "").strip()
                or (row["artist"] or "").strip())


def _placeholder_component(rel):
    """The first path component that is a placeholder, or None.

    A file whose album is unknown would be moved out of a folder that says
    "someday you'll wake up, and you'll be 26" and into one that says
    "Unknown Album". Whatever the first folder is - a playlist title yt-dlp
    salvaged, a name somebody typed - it carries more than the second does,
    so the move is refused rather than made. Tidiness that destroys
    information is not tidiness.
    """
    for part in rel.split("/")[:-1]:
        if part in PLACEHOLDERS:
            return part
    return None


def plan(con, root, template=None, limit=None):
    """Work out which files would move. Touches nothing.

    Returns ``(moves, skipped)``. Each move is a dict with the source path,
    the destination relative to ``root``, and the absolute destination.
    """
    template = template or DEFAULT_TEMPLATE
    root = norm(os.path.abspath(root))
    rows = con.execute(
        "SELECT * FROM track WHERE root = ? ORDER BY rel_path", (root,)
    ).fetchall()

    moves, skipped = [], []
    # Destinations are claimed as they are decided, and the sources that are
    # staying claim theirs too - otherwise a file being moved could be given
    # the path of one that is not moving, and the rename would destroy it.
    taken = set()
    planned = []
    for row in rows:
        if not _has_name(row):
            skipped.append({"track_id": row["id"], "rel_path": row["rel_path"],
                            "why": "no artist to file it under"})
            continue
        base = os.path.basename(row["path"])
        # render_template has already sanitised every component it built.
        # The filename is put in afterwards and left exactly as it is: it
        # came off this filesystem, so it is valid here, and running it
        # through the sanitiser again would mangle characters it is entitled
        # to contain.
        dest = render_template(template, row, ext=os.path.splitext(base)[1])
        dest = dest.replace("__FILENAME__", base)
        placeholder = _placeholder_component(dest)
        if placeholder:
            skipped.append({
                "track_id": row["id"], "rel_path": row["rel_path"],
                "why": f"would file it under \"{placeholder}\""})
            continue
        planned.append((row, dest))

    for row, dest in planned:
        if norm(dest).casefold() == norm(row["rel_path"]).casefold():
            taken.add(norm(dest).casefold())

    for row, dest in planned:
        current = norm(row["rel_path"])
        if norm(dest).casefold() == current.casefold():
            continue
        dest = _dedupe(dest, taken)
        moves.append({
            "track_id": row["id"],
            "src": row["path"],
            "src_rel": current,
            "dest_rel": dest,
            "dest": norm(os.path.join(root, dest)),
        })
        if limit and len(moves) >= limit:
            break
    return moves, skipped


def _dedupe(rel, taken):
    key = norm(rel).casefold()
    if key not in taken:
        taken.add(key)
        return rel
    head, _, tail = rel.rpartition("/")
    stem, dot, ext = tail.rpartition(".")
    if not dot:
        stem, ext = tail, ""
    for n in range(2, 1000):
        cand_tail = f"{stem} ({n}){'.' + ext if ext else ''}"
        cand = f"{head}/{cand_tail}" if head else cand_tail
        if norm(cand).casefold() not in taken:
            taken.add(norm(cand).casefold())
            return cand
    raise OrganiseError(f"cannot find a free name for {rel!r}")


def affected_devices(con):
    """Devices whose destinations are derived from the library's own layout.

    A device using the ``{rel_path}`` template mirrors the library folder for
    folder, so moving files here changes where they belong on the card: the
    next sync will copy them to their new places and delete the old ones.
    That is correct, and it is a lot of writing to a memory card, so it is
    said out loud before anything moves rather than discovered afterwards.
    """
    return [dict(r) for r in con.execute(
        "SELECT id, name, path_template FROM device "
        "WHERE path_template LIKE '%{rel_path}%'")]


def apply(con, moves, journal_dir=None, progress=None):
    """Carry out planned moves, updating the catalog as each one lands.

    One file at a time, each committed before the next begins, so an
    interruption leaves a catalog that matches the disk rather than one that
    describes a library half of which has moved.

    Returns ``(counts, journal_path)``. The journal is written first and
    appended to as moves succeed, so it describes what actually happened even
    if the process is killed partway.
    """
    counts = {"moved": 0, "failed": 0, "skipped": 0}
    if not moves:
        return counts, None

    journal_dir = journal_dir or os.path.dirname(
        os.path.abspath(con.execute("PRAGMA database_list").fetchone()[2]))
    os.makedirs(journal_dir, exist_ok=True)
    journal_path = os.path.join(
        journal_dir, f"{JOURNAL_PREFIX}{int(time.time())}.json")
    done = []
    _write_journal(journal_path, done)

    for i, move in enumerate(moves, 1):
        try:
            _move_one(con, move)
        except OrganiseError:
            counts["failed"] += 1
        else:
            counts["moved"] += 1
            done.append({"from": move["src"], "to": move["dest"],
                         "track_id": move["track_id"]})
            _write_journal(journal_path, done)
        if progress:
            progress(i, len(moves))

    _prune_empty_dirs({os.path.dirname(m["src"]) for m in moves})
    return counts, journal_path


def _move_one(con, move):
    src = resolve_existing(move["src"]) or move["src"]
    if not os.path.isfile(src):
        raise OrganiseError(f"gone: {move['src']}")
    dest = move["dest"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # A case-only rename on a case-insensitive filesystem is the one
    # collision that is not a collision: the destination "exists" because it
    # is the source.
    if os.path.exists(dest) and norm(dest).casefold() != norm(src).casefold():
        raise OrganiseError(f"destination exists: {dest}")
    try:
        os.replace(src, dest)
    except OSError:
        # A different volume, which os.replace will not cross.
        try:
            shutil.move(src, dest)
        except (OSError, shutil.Error) as exc:
            raise OrganiseError(str(exc)) from exc

    # The content key does not change - the bytes did not - so the device
    # manifest still matches and a replug stays a no-op.
    con.execute("UPDATE track SET path = ?, rel_path = ? WHERE id = ?",
                (norm(dest), move["dest_rel"], move["track_id"]))
    con.commit()


def _write_journal(path, entries):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "moves": entries}, fh,
                  ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _prune_empty_dirs(dirs):
    """Remove directories the moves emptied, and their empty parents."""
    for d in sorted(dirs, key=len, reverse=True):
        current = d
        for _ in range(4):      # far enough for Artist/Album, not unbounded
            try:
                if not os.path.isdir(current) or os.listdir(current):
                    break
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)


def undo(con, journal_path, progress=None):
    """Put every file in a journal back where it came from.

    Read in reverse, so a chain of moves unwinds in the order that keeps each
    destination free.
    """
    try:
        with open(journal_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OrganiseError(f"cannot read the journal: {exc}") from exc

    entries = list(reversed(data.get("moves") or []))
    counts = {"restored": 0, "failed": 0}
    for i, entry in enumerate(entries, 1):
        row = con.execute("SELECT * FROM track WHERE id = ?",
                          (entry["track_id"],)).fetchone()
        move = {"src": entry["to"], "dest": entry["from"],
                "track_id": entry["track_id"],
                "dest_rel": (norm(os.path.relpath(entry["from"], row["root"]))
                             if row else None)}
        if move["dest_rel"] is None:
            counts["failed"] += 1
            continue
        try:
            _move_one(con, move)
        except OrganiseError:
            counts["failed"] += 1
        else:
            counts["restored"] += 1
        if progress:
            progress(i, len(entries))
    _prune_empty_dirs({os.path.dirname(e["to"]) for e in entries})
    return counts


def journals(journal_dir):
    """Past organise runs, newest first."""
    try:
        names = [n for n in os.listdir(journal_dir)
                 if n.startswith(JOURNAL_PREFIX) and n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(journal_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                moves = json.load(fh).get("moves") or []
        except (OSError, ValueError):
            continue
        out.append({"path": path, "moves": len(moves),
                    "at": os.path.getmtime(path)})
    return sorted(out, key=lambda j: j["at"], reverse=True)
