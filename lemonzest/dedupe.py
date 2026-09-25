"""Several files, one song: grouping versions under a master.

A work is a song the library holds more than once - the album cut, the
holiday album, the "best of", the same video fetched into two folders. One
file is the master: every reference resolves to it through the track_canon
view. The others stay where they are, untouched, as a record of what was
fetched and from where. Nothing here moves, re-tags or deletes a file.
"""
import time


class DedupeError(ValueError):
    pass


def work_of(con, content_key):
    row = con.execute("SELECT work_id FROM work_member WHERE content_key = ?",
                      (content_key,)).fetchone()
    return row["work_id"] if row else None


def members(con, work_id):
    """Every file in a work, master first, as dicts."""
    return [dict(r) for r in con.execute(
        "SELECT content_key, is_master, reason, joined_at FROM work_member "
        "WHERE work_id = ? ORDER BY is_master DESC, joined_at, content_key",
        (work_id,))]


def _known(con, key):
    if not con.execute("SELECT 1 FROM track WHERE content_key = ? LIMIT 1",
                       (key,)).fetchone():
        raise DedupeError("no such file: " + key)


def merge(con, master_key, variant_keys, reason="manual"):
    """Make ``master_key`` the master of a work holding every variant.

    A file already in another work brings that whole work along: merging
    B into A when B already stands for C gives one work of A, B and C, not
    two works fighting over B. Returns the work id.
    """
    keys = [k for k in dict.fromkeys(variant_keys) if k != master_key]
    if not keys:
        raise DedupeError("nothing to merge")
    for k in [master_key] + keys:
        _known(con, k)
    now = time.time()
    with con:
        works = {w for w in (work_of(con, k) for k in [master_key] + keys) if w}
        work_id = work_of(con, master_key) or (min(works) if works else None)
        if work_id is None:
            work_id = con.execute(
                "INSERT INTO work(created_at, updated_at) VALUES (?,?)",
                (now, now)).lastrowid
        for other in works - {work_id}:
            con.execute("UPDATE work_member SET work_id = ?, is_master = 0 "
                        "WHERE work_id = ?", (work_id, other))
            con.execute("DELETE FROM work WHERE id = ?", (other,))
        con.execute("UPDATE work_member SET is_master = 0 WHERE work_id = ?",
                    (work_id,))
        for k in [master_key] + keys:
            con.execute(
                "INSERT INTO work_member(content_key, work_id, is_master, "
                "reason, joined_at) VALUES (?,?,0,?,?) "
                "ON CONFLICT(content_key) DO UPDATE SET work_id = excluded.work_id",
                (k, work_id, reason, now))
        con.execute("UPDATE work_member SET is_master = 1 WHERE content_key = ?",
                    (master_key,))
        con.execute("UPDATE work SET updated_at = ? WHERE id = ?", (now, work_id))
    return work_id


def set_master(con, content_key):
    """Let another version of the song stand for it."""
    work_id = work_of(con, content_key)
    if work_id is None:
        raise DedupeError("not part of any work: " + content_key)
    with con:
        con.execute("UPDATE work_member SET is_master = 0 WHERE work_id = ?",
                    (work_id,))
        con.execute("UPDATE work_member SET is_master = 1 WHERE content_key = ?",
                    (content_key,))
        con.execute("UPDATE work SET updated_at = ? WHERE id = ?",
                    (time.time(), work_id))
    return work_id


def unmerge(con, content_key):
    """Take one file back out of its work.

    A work left with a single file is no work at all and goes; a work that
    loses its master hands the part to the longest-standing variant.
    """
    work_id = work_of(con, content_key)
    if work_id is None:
        return None
    with con:
        con.execute("DELETE FROM work_member WHERE content_key = ?",
                    (content_key,))
        rest = members(con, work_id)
        if len(rest) < 2:
            con.execute("DELETE FROM work WHERE id = ?", (work_id,))
        elif not rest[0]["is_master"]:
            con.execute("UPDATE work_member SET is_master = 1 "
                        "WHERE content_key = ?", (rest[0]["content_key"],))
    return work_id


def canon_ids(con, track_ids):
    """Track ids resolved to their masters, first occurrence kept, in order."""
    if not track_ids:
        return []
    ids = list(track_ids)
    q = ",".join("?" * len(ids))
    canon = {r["id"]: r["canon_id"] for r in con.execute(
        f"SELECT id, canon_id FROM track_canon WHERE id IN ({q})", ids)}
    return list(dict.fromkeys(canon.get(i, i) for i in ids))
