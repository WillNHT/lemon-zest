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


# ------------------------------------------------------------- suggestions
#
# Found on demand, never merged on their own: a match is only ever a
# suggestion a person accepts or turns down. Strongest evidence first.

# Two files of one song rarely differ in length by more than a fade.
DURATION_SLACK = 3.0
SCORES = {"source": 1.0, "isrc": 0.95, "mbid": 0.95}
MIN_SCORE = 0.6


def _pair(a, b):
    return (a, b) if a < b else (b, a)


def dismissed(con):
    return {(r["key_a"], r["key_b"]) for r in con.execute(
        "SELECT key_a, key_b FROM dup_dismissed")}


def dismiss(con, keys):
    """Every file in ``keys`` is a different song from every other."""
    now = time.time()
    keys = list(dict.fromkeys(keys))
    with con:
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                con.execute("INSERT OR IGNORE INTO dup_dismissed(key_a, key_b, "
                            "at) VALUES (?,?,?)", _pair(a, b) + (now,))


def _rows(con):
    """One row per file: the catalog's values and the recording it matched."""
    out = {}
    for r in con.execute(
            "SELECT t.content_key, t.id, t.title, t.artist, t.album, "
            "t.album_artist, t.year, t.duration, t.isrc, t.source_id, "
            "t.added_at, t.path, t.rel_path, e.mbid, w.work_id, w.is_master "
            "FROM track t "
            "LEFT JOIN enrichment e ON e.content_key = t.content_key "
            "  AND e.status = 'applied' "
            "LEFT JOIN work_member w ON w.content_key = t.content_key "
            "ORDER BY t.id"):
        out.setdefault(r["content_key"], dict(r))
    return out


def _edges(rows):
    """Every (key_a, key_b) -> (score, reason) the evidence supports."""
    from .enrich import _best_artist_ratio, clean_title, credits, fold
    edges = {}

    def add(a, b, score, reason):
        if a == b:
            return
        p = _pair(a, b)
        if score > edges.get(p, (0, None))[0]:
            edges[p] = (score, reason)

    for field, reason in (("source_id", "source"), ("isrc", "isrc"),
                          ("mbid", "mbid")):
        by = {}
        for k, r in rows.items():
            v = (r[field] or "").strip().upper()
            if v:
                by.setdefault(v, []).append(k)
        for keys in by.values():
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    add(a, b, SCORES[reason], reason)

    # The same title, give or take decoration, and the same length. The
    # artist only raises the score: a pseudonym is the case this catches.
    # ponytail: pairwise inside one title block; fine for a library, block
    # further on duration if one title ever gathers hundreds of files.
    by_title = {}
    for k, r in rows.items():
        t = fold(clean_title(r["title"]))
        if t:
            by_title.setdefault(t, []).append(k)
    for keys in by_title.values():
        for i, a in enumerate(keys):
            ra = rows[a]
            for b in keys[i + 1:]:
                rb = rows[b]
                if ra["duration"] is None or rb["duration"] is None:
                    continue
                if abs(ra["duration"] - rb["duration"]) > DURATION_SLACK:
                    continue
                artist = _best_artist_ratio(credits(ra["artist"]) or [""],
                                            credits(rb["artist"]) or [""])
                add(a, b, round(0.6 + 0.3 * artist, 2),
                    "title" if artist >= 0.8 else "title, other artist")
    return edges


def suggest(con, keys=None, min_score=MIN_SCORE):
    """Groups of files that look like one song, strongest first.

    ``keys`` narrows it to groups touching those files - what a download
    that just landed asks. Pairs already in one work, and pairs somebody
    said were different songs, are left out.
    """
    rows = _rows(con)
    no = dismissed(con)
    parent = {}

    def find(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k

    links = []
    for (a, b), (score, reason) in _edges(rows).items():
        if score < min_score or (a, b) in no:
            continue
        wa, wb = rows[a]["work_id"], rows[b]["work_id"]
        if wa and wa == wb:
            continue
        links.append((a, b, score, reason))
        if find(a) != find(b):
            parent[find(a)] = find(b)

    groups = {}
    for a, b, score, reason in links:
        g = groups.setdefault(find(a), {"keys": set(), "score": 0,
                                        "reasons": set()})
        g["keys"] |= {a, b}
        g["score"] = max(g["score"], score)
        g["reasons"].add(reason)

    wanted = set(keys or ())
    out = []
    for g in groups.values():
        if wanted and not (g["keys"] & wanted):
            continue
        # An existing master stays master; otherwise the file that knows
        # its album, the earliest release - the original, not the holiday
        # album or the "best of" - and then the one that arrived first.
        members_ = sorted((rows[k] for k in g["keys"]), key=lambda r: (
            not r["is_master"], not r["album"], str(r["year"] or "9999")[:4],
            r["added_at"] or 0, r["id"]))
        out.append({"score": g["score"], "reasons": sorted(g["reasons"]),
                    "master": members_[0]["content_key"],
                    "tracks": [_public(r) for r in members_]})
    out.sort(key=lambda g: (-g["score"], g["tracks"][0]["title"] or ""))
    return out


PUBLIC = ("content_key", "id", "title", "artist", "album", "album_artist",
          "year", "duration", "isrc", "source_id", "rel_path")


def _public(row):
    return {f: row[f] for f in PUBLIC}


# ------------------------------------------------------------ the versions

def versions_of(con, content_key):
    """The work a file belongs to: every version, and what the master took
    from which. None when the file stands alone."""
    work_id = work_of(con, content_key)
    if work_id is None:
        return None
    tracks = []
    for m in members(con, work_id):
        r = con.execute(
            "SELECT t.*, md.url AS source_url FROM track t "
            "LEFT JOIN media md ON md.source_id = t.source_id "
            "WHERE t.content_key = ? ORDER BY t.id LIMIT 1",
            (m["content_key"],)).fetchone()
        if r is None:           # its file has left the catalog
            continue
        d = _public(r)
        d.update(is_master=bool(m["is_master"]), reason=m["reason"],
                 source_url=r["source_url"])
        tracks.append(d)
    master = next((t["content_key"] for t in tracks if t["is_master"]), None)
    picks = {r["field"]: r["from_key"] for r in con.execute(
        "SELECT field, from_key FROM track_override "
        "WHERE content_key = ? AND from_key IS NOT NULL", (master,))}
    return {"work_id": work_id, "master": master, "tracks": tracks,
            "picks": picks}


PICKABLE = ("title", "artist", "album", "album_artist", "track_no",
            "disc_no", "year", "date", "genre", "isrc", "cover")


def pick(con, field, from_key):
    """Give the master one value - or the cover - of another version.

    A text value goes in as a hand-typed override on the master, so it
    outranks every lookup from here on; the version it came from is kept
    beside it. A cover is written into the master's file at once. Picking
    the master's own version undoes an earlier pick. Returns the master's
    content key, which a cover write changes.
    """
    from . import enrich, tags
    from .paths import resolve_existing
    if field not in PICKABLE:
        raise DedupeError("cannot take %s from another version" % field)
    work_id = work_of(con, from_key)
    if work_id is None:
        raise DedupeError("not part of any work: " + from_key)
    master = next(m["content_key"] for m in members(con, work_id)
                  if m["is_master"])
    src = con.execute("SELECT * FROM track WHERE content_key = ? LIMIT 1",
                      (from_key,)).fetchone()
    dest = con.execute("SELECT path FROM track WHERE content_key = ? LIMIT 1",
                       (master,)).fetchone()
    if src is None or dest is None:
        raise DedupeError("a file in this work has left the catalog")

    if master == from_key:
        # The pick goes and the file's own reading comes back. A cover
        # already written into the file stays written.
        con.execute("DELETE FROM track_override WHERE content_key = ? "
                    "AND field = ?", (master, field))
        if field != "cover":
            from .meta import read_tags
            real = resolve_existing(dest["path"])
            own = read_tags(real).get(field) if real else None
            con.execute(f"UPDATE track SET {field} = ? WHERE content_key = ?",
                        (own, master))
        con.commit()
        return master

    value = None
    if field == "cover":
        real = resolve_existing(src["path"])
        cover = tags.read_cover(real) if real else None
        if not cover:
            raise DedupeError("that version has no cover")
        try:
            new_key = tags.write(dest["path"], {}, cover=cover)
        except tags.TagWriteError as exc:
            raise DedupeError(str(exc)) from exc
        tags.rekey(con, master, dest["path"], new_key)
        master = new_key or master
    else:
        value = src[field]
        enrich.override(con, master, **{field: value})
    con.execute(
        "INSERT INTO track_override(content_key, field, value, set_at, "
        "from_key) VALUES (?,?,?,?,?) ON CONFLICT(content_key, field) "
        "DO UPDATE SET from_key = excluded.from_key",
        (master, field, value, time.time(), from_key))
    con.commit()
    return master
