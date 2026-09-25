"""m3u8 reading and writing.

The dialect is not guessed: it is the one already in use on the HiBy card.

    #EXTM3U
    #EXTENC: UTF-8
    #PLAYLIST: chill
    #EXTINF:239,Radiohead - Creep
    ../Music/Radiohead/Pablo Honey/02 Creep.m4a

CRLF line endings, UTF-8 without a BOM, paths relative to the playlist
file's own folder, forward slashes. Two variants exist in the wild and both
must read cleanly:

  * the PC-side copies use plain paths with a "../Music/" prefix, and may
    carry "#Collection URI" and per-track "#Apple Music URI" comments;
  * the copies HiBy itself writes percent-encode every path and drop the
    "../" prefix, because they sit inside the music folder.

The writer always replaces the target file. That is the entire fix for the
duplication found on the real card, where eighteen syncs had merged the
same 101 tracks into an 1,817-line playlist.
"""
import os
import re
import time
import unicodedata
import urllib.parse

from . import dedupe
from .paths import norm, resolve_existing, safe_component


def norm_name(name):
    """NFC-normalise a playlist name.

    The name arrives either from the filename or from a #PLAYLIST header,
    and the two spellings of the same Vietnamese title differ by
    normalisation form. Without this, one playlist becomes two rows that
    look identical in every listing.
    """
    return unicodedata.normalize("NFC", str(name)).strip()

_EXTINF = re.compile(r"^#EXTINF\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*(.*)$", re.I)
_COLLECTION = re.compile(r"^#\s*Collection URI\s*:\s*(.+)$", re.I)
_TRACK_URI = re.compile(r"^#\s*(?:Apple Music|Spotify|Source) URI\s*:\s*(.+)$", re.I)
_NAME = re.compile(r"^#\s*PLAYLIST\s*:\s*(.+)$", re.I)


def read(path):
    """Parse one m3u/m3u8 file.

    Returns a dict with name, source_uri and entries; each entry has
    raw_path, abs_path, title_hint, duration and source_uri. abs_path is
    None when the entry does not resolve on disk.
    """
    base = os.path.dirname(os.path.abspath(path))
    name = norm_name(os.path.splitext(os.path.basename(path))[0])
    out = {"name": name, "source_uri": None, "entries": []}
    pending = {"title_hint": None, "duration": None, "source_uri": None}

    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                m = _EXTINF.match(line)
                if m:
                    dur = float(m.group(1))
                    pending["duration"] = dur if dur >= 0 else None
                    pending["title_hint"] = m.group(2).strip() or None
                    continue
                m = _COLLECTION.match(line)
                if m:
                    out["source_uri"] = m.group(1).strip()
                    continue
                m = _TRACK_URI.match(line)
                if m:
                    pending["source_uri"] = m.group(1).strip()
                    continue
                m = _NAME.match(line)
                if m:
                    # "#Playlist: chill by Someone" -> "chill"
                    out["name"] = norm_name(
                        re.sub(r"\s+by\s+.*$", "", m.group(1).strip())) or name
                continue

            raw = line
            decoded = urllib.parse.unquote(raw)
            cand = decoded if os.path.isabs(decoded) else os.path.join(base, decoded)
            resolved = resolve_existing(os.path.normpath(cand))
            if resolved is None and decoded[:1] in ("/", "\\"):
                # "/Music/..." names a file from the root of the card the
                # playlist sits on, which is the folder above the playlist
                # folder. On a PC that is the folder the library sits in.
                resolved = resolve_existing(os.path.normpath(os.path.join(
                    os.path.dirname(base), decoded.lstrip("/\\"))))
            out["entries"].append({
                "raw_path": raw,
                "abs_path": norm(os.path.abspath(resolved)) if resolved else None,
                "title_hint": pending["title_hint"],
                "duration": pending["duration"],
                "source_uri": pending["source_uri"],
            })
            pending = {"title_hint": None, "duration": None, "source_uri": None}
    return out


def write(path, name, rows, source_uri=None, encode_paths=False):
    """Write a playlist, replacing whatever was there.

    rows is a sequence of (rel_path, title_hint, duration, source_uri), where
    rel_path is already relative to the playlist file's folder. Written to a
    temporary file and renamed, so an interrupted write never leaves a
    half-playlist the player would index.
    """
    lines = ["#EXTM3U", "#EXTENC: UTF-8", "#PLAYLIST: " + name]
    if source_uri:
        lines.append("#Collection URI: " + source_uri)
    for rel, title, duration, uri in rows:
        if uri:
            lines.append("#Source URI: " + uri)
        secs = int(round(duration)) if duration else -1
        lines.append("#EXTINF:%d,%s" % (secs, title or os.path.basename(rel)))
        p = norm(rel)
        lines.append(urllib.parse.quote(p, safe="/") if encode_paths else p)

    body = "\r\n".join(lines) + "\r\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".lz-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(rows)


# Which service a "#Collection URI" names. A playlist that came from
# somewhere should go on saying so: it is written into the file, so a
# re-import of a file this program wrote does not quietly demote a
# downloaded playlist to a local one.
ORIGINS = (("music.youtube.", "youtube"), ("youtube.com", "youtube"),
           ("youtu.be", "youtube"), ("apple.com", "apple_music"),
           ("spotify.com", "spotify"))


# YouTube Music serves an album, an EP or a single as a playlist whose id
# starts with this, titled "Album - Dookie" or "EP - 7". It is a release,
# not somebody's list, and the library already groups a release by its
# album tag - a playlist of it only lengthens the playlist menu on a player.
ALBUM_LIST_PREFIX = "OLAK5uy_"
_ALBUM_TITLE = re.compile(r"^(album|ep|single)\s+-\s+", re.I)


def is_album(url, info=None):
    """Whether a playlist URL (or its listing) is really an album or EP."""
    if ("list=" + ALBUM_LIST_PREFIX) in (url or ""):
        return True
    info = info or {}
    return (str(info.get("id") or "").startswith(ALBUM_LIST_PREFIX)
            or bool(_ALBUM_TITLE.match(str(info.get("title") or ""))))


def origin_of(source_uri):
    uri = (source_uri or "").lower()
    for needle, name in ORIGINS:
        if needle in uri:
            return name
    return "local"


def import_dir(con, directory, recursive=False):
    """Import every playlist in a directory into the catalog.

    Entries are linked to tracks by resolved absolute path. Unresolvable
    entries are kept with track_id NULL so they surface as unmatched rather
    than vanishing.
    """
    directory = os.path.abspath(directory)
    files = []
    if recursive:
        for dp, _, fns in os.walk(directory):
            files += [os.path.join(dp, f) for f in fns
                      if f.lower().endswith((".m3u", ".m3u8"))]
    else:
        files = [os.path.join(directory, f) for f in sorted(os.listdir(directory))
                 if f.lower().endswith((".m3u", ".m3u8"))]

    by_path = {r["path"]: r["id"] for r in con.execute("SELECT id, path FROM track")}
    results = []
    for f in files:
        pl = read(f)
        if pl["source_uri"] and is_album(pl["source_uri"]):
            continue            # a release, not a playlist: see is_album
        matched = unmatched = 0
        origin = origin_of(pl["source_uri"])
        # How many of these entries actually resolve to catalogued files?
        incoming_matched = sum(
            1 for e in pl["entries"] if e["abs_path"] and e["abs_path"] in by_path
        )
        con.execute(
            "INSERT INTO playlist(name, origin, source_uri, imported_from, imported_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "source_uri=COALESCE(excluded.source_uri, playlist.source_uri), "
            "imported_from=excluded.imported_from, "
            "imported_at=excluded.imported_at, origin=excluded.origin",
            (pl["name"], origin, pl["source_uri"], norm(f), time.time()),
        )
        pid = con.execute(
            "SELECT id FROM playlist WHERE name=?", (pl["name"],)
        ).fetchone()["id"]

        existing = con.execute(
            "SELECT COUNT(*) c, SUM(track_id IS NOT NULL) m "
            "FROM playlist_entry WHERE playlist_id=?", (pid,)
        ).fetchone()
        existing_matched = existing["m"] or 0

        # The same playlist ships in two variants: one whose relative paths
        # resolve, and one that carries the provider URIs but sits a folder
        # deeper so its paths do not. Whichever arrives second must not
        # discard the other's contribution. If this copy resolves worse than
        # what we already hold, keep the stored entries and take only the
        # per-track URIs from it, matched up by position.
        if existing["c"] and incoming_matched < existing_matched:
            enriched = 0
            for pos, e in enumerate(pl["entries"]):
                if e["source_uri"]:
                    cur = con.execute(
                        "UPDATE playlist_entry SET source_uri=? "
                        "WHERE playlist_id=? AND pos=? AND source_uri IS NULL",
                        (e["source_uri"], pid, pos),
                    )
                    enriched += cur.rowcount
            results.append({"name": pl["name"], "file": f,
                            "total": existing["c"], "matched": existing_matched,
                            "unmatched": existing["c"] - existing_matched,
                            "source_uri": pl["source_uri"], "mode": "enriched",
                            "enriched": enriched})
            continue

        # An import replaces the entries wholesale, so when each one first
        # joined has to be carried across by hand. Without this, every scan
        # of the library folder would re-import its own playlists and mark
        # all of them new.
        was = {r["raw_path"]: r["added_at"] for r in con.execute(
            "SELECT raw_path, added_at FROM playlist_entry "
            "WHERE playlist_id = ?", (pid,))}
        # write_local names a variant's entry by its master's file. Read
        # back, that entry keeps pointing at the variant it was made from,
        # so taking the song apart again restores the playlist as it was.
        variants = {}
        for r in con.execute(
                "SELECT e.track_id, e.raw_path, c.canon_id FROM playlist_entry e "
                "JOIN track_canon c ON c.id = e.track_id "
                "WHERE e.playlist_id = ? AND c.canon_id != e.track_id "
                "ORDER BY e.pos", (pid,)):
            variants.setdefault(r["canon_id"], []).append(r)
        now = time.time()
        joined = 0
        con.execute("DELETE FROM playlist_entry WHERE playlist_id=?", (pid,))
        for pos, e in enumerate(pl["entries"]):
            tid = by_path.get(e["abs_path"]) if e["abs_path"] else None
            if tid in variants and variants[tid]:
                old = variants[tid].pop(0)
                tid = old["track_id"]
                e = dict(e, raw_path=old["raw_path"])
            if tid:
                matched += 1
            else:
                unmatched += 1
            when = was.get(e["raw_path"])
            if when is None:
                when = now
                joined += 1
            con.execute(
                "INSERT INTO playlist_entry(playlist_id,pos,track_id,raw_path,"
                "title_hint,duration,source_uri,added_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, pos, tid, e["raw_path"], e["title_hint"], e["duration"],
                 e["source_uri"], when),
            )
        if joined and was:
            # `was` empty means this playlist is new to the catalog, and a
            # playlist that has just been imported for the first time is not
            # a playlist that has changed.
            con.execute("UPDATE playlist SET updated_at = ? WHERE id = ?",
                        (now, pid))
        results.append({"name": pl["name"], "file": f, "total": len(pl["entries"]),
                        "matched": matched, "unmatched": unmatched,
                        "source_uri": pl["source_uri"], "mode": "imported",
                        "enriched": 0})
    con.commit()
    return results


def append_tracks(con, name, track_ids, origin="local", source_uri=None):
    """Add catalogued tracks to the end of a playlist, creating it if new.

    This is the path a download takes: the file is already in the catalog,
    so the entry is written by track id rather than by parsing a path back
    out of a file. Tracks the playlist already holds are not added a second
    time - the whole reason this project exists is a card whose playlists
    had grown 17.9x by appending what was already there.

    Returns a summary dict: the playlist name, its id, how many entries were
    added and how many were already present.
    """
    name = norm_name(name)
    if not name:
        raise ValueError("a playlist needs a name")
    now = time.time()
    con.execute(
        "INSERT INTO playlist(name, origin, source_uri, imported_at) "
        "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        # Where it came from is worth keeping even when the playlist is
        # already here: a run that learns the URL should not have to wait
        # for the playlist to be deleted before it can say so.
        "source_uri = COALESCE(excluded.source_uri, playlist.source_uri), "
        "origin = CASE WHEN excluded.origin = 'local' THEN playlist.origin "
        "         ELSE excluded.origin END",
        (name, origin, source_uri, time.time()),
    )
    pid = con.execute("SELECT id FROM playlist WHERE name=?", (name,)).fetchone()["id"]

    # Compared as masters: the holiday cut of a song already here as its
    # album cut is not a new entry.
    have = {r["canon_id"] for r in con.execute(
        "SELECT c.canon_id FROM playlist_entry e "
        "JOIN track_canon c ON c.id = e.track_id WHERE e.playlist_id=?", (pid,))}
    pos = con.execute(
        "SELECT COALESCE(MAX(pos), -1) + 1 FROM playlist_entry WHERE playlist_id=?",
        (pid,)).fetchone()[0]

    added = skipped = 0
    for tid in dedupe.canon_ids(con, track_ids):
        if tid in have:
            skipped += 1
            continue
        t = con.execute(
            "SELECT path, title, artist, duration, purl FROM track WHERE id=?",
            (tid,)).fetchone()
        if t is None:
            skipped += 1
            continue
        title = t["title"] or os.path.splitext(os.path.basename(t["path"]))[0]
        if t["artist"]:
            title = f"{t['artist']} - {title}"
        con.execute(
            "INSERT INTO playlist_entry(playlist_id,pos,track_id,raw_path,"
            "title_hint,duration,source_uri,added_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, pos, tid, norm(t["path"]), title, t["duration"], t["purl"],
             now),
        )
        have.add(tid)
        pos += 1
        added += 1
    if added:
        # Only when something actually joined: a run that fetched a playlist
        # and found nothing new has not updated it, whatever it cost.
        con.execute("UPDATE playlist SET updated_at = ? WHERE id = ?",
                    (now, pid))
    con.commit()
    return {"name": name, "id": pid, "added": added, "skipped": skipped,
            "entries": pos}


# Where a playlist lives on the PC side: a "Playlists" folder beside the
# library folder, laid out the way a Rockbox card is - /Music and
# /Playlists side by side - so the two folders can be copied onto a card as
# they are. Entries are written from that shared parent ("/Music/Artist/..."),
# which is what Rockbox resolves from the root of the card.
LOCAL_DIR = "Playlists"
# Where they used to go: a folder inside the library, with paths relative to
# it. Read, and moved out of, but never written.
LEGACY_DIR = "playlists"


def _parent(root):
    """The folder a library folder sits in; the folder itself for a drive."""
    root = os.path.abspath(root)
    parent = os.path.dirname(root)
    return root if os.path.normcase(parent) == os.path.normcase(root) \
        else parent


def local_dir(root):
    return os.path.join(_parent(root), LOCAL_DIR)


def local_path(root, name):
    return os.path.join(local_dir(root), safe_filename(name))


def card_path(track_root, rel_path):
    """A track as a Rockbox playlist names it: from the card's root."""
    top = os.path.abspath(track_root)
    top = "" if _parent(top) == top else os.path.basename(top)
    return "/" + "/".join(x for x in (norm(top), norm(rel_path)) if x)


def write_local(con, name, root):
    """Write a catalog playlist out as a file beside the library.

    The device copies are written at sync time, against that device's own
    folder layout and filename template. This is the PC-side copy: the one a
    player pointed at the library reads, and the one a person looks at to
    see whether the playlist really did get the new tracks.

    Entries whose file is missing are left out rather than written as dead
    lines - a player that hits one stops rather than skipping it. Returns the
    path written and how many entries it holds.
    """
    name = norm_name(name)
    row = con.execute("SELECT id, source_uri FROM playlist WHERE name = ?",
                      (name,)).fetchone()
    if row is None:
        raise ValueError("no such playlist: " + name)
    path = local_path(root, name)
    rows, seen = [], set()
    for e in con.execute(
            "SELECT e.title_hint, e.duration, e.source_uri, "
            "t.id, t.path AS track_path, t.root, t.rel_path, "
            "c.canon_id = e.track_id AS own FROM playlist_entry e "
            "JOIN track_canon c ON c.id = e.track_id "
            "JOIN track t ON t.id = c.canon_id "
            "WHERE e.playlist_id = ? ORDER BY e.pos", (row["id"],)):
        # Two versions of one song both play the master: write it once.
        if e["id"] in seen or not os.path.isfile(e["track_path"]):
            continue
        seen.add(e["id"])
        rows.append((card_path(e["root"], e["rel_path"]),
                     e["title_hint"] if e["own"] else None,
                     e["duration"] if e["own"] else None, e["source_uri"]))
    # The source goes in the file, not only in the catalog: a re-import -
    # which every scan of the library folder now does - would otherwise
    # read back a playlist that had forgotten where it came from.
    write(path, name, rows, source_uri=row["source_uri"])
    legacy = os.path.join(root, LEGACY_DIR, safe_filename(name))
    if os.path.isfile(legacy):
        os.remove(legacy)
        try:
            os.rmdir(os.path.dirname(legacy))
        except OSError:
            pass            # not empty: something else still lives there
    return {"path": norm(path), "entries": len(rows)}


def cover_path(root, name):
    """Where a playlist's cover sits: beside its file, same name, JPEG.

    Which is where the player looks for it - "chill.jpg" next to
    "chill.m3u8" - so the Playlists folder copied onto a card as it is
    shows the covers too.
    """
    return os.path.splitext(local_path(root, name))[0] + ".jpg"


def save_cover(name, root, url, opener=None):
    """Fetch a playlist's own picture and keep it beside the playlist.

    Squared and re-encoded as a baseline JPEG on the way in, like every
    other cover, because that is the only kind Rockbox draws in colour.
    Returns the path written, or None - a playlist with no cover still
    shows its first track's, so a failure here costs nothing.
    """
    import urllib.request

    from . import artwork
    from .enrich import HTTP_TIMEOUT, USER_AGENT

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as r:
            data = r.read()
    except Exception:      # noqa: BLE001 - a cover is never worth a failure
        return None
    data, _ = artwork.normalise(data)
    if not data or data[:2] != b"\xff\xd8":
        return None      # not a JPEG and no ffmpeg to make it one
    path = cover_path(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lz-tmp", "wb") as fh:
        fh.write(data)
    os.replace(path + ".lz-tmp", path)
    return norm(path)


def find_cover(con, name):
    """The cover saved for a playlist beside any library folder, or None."""
    from .db import roots
    for root in roots(con):
        path = cover_path(root, name)
        if os.path.isfile(path):
            return path
    return None


def import_library(con, root):
    """Read every playlist that belongs to a library folder.

    The ones inside it, and the ones in the Playlists folder beside it. A
    playlist a download wrote in the old place - inside the library, with
    relative paths - is written out again in the new one.
    """
    legacy = os.path.join(root, LEGACY_DIR)
    # An album an older version made into a playlist. Only a download
    # writes a YouTube Music album URL into a file, so these are ours to
    # remove - and left beside the library they would reach the card.
    for folder in (local_dir(root), legacy):
        for f in list_playlist_files(folder):
            path = os.path.join(folder, f)
            if is_album(read(path)["source_uri"] or ""):
                os.remove(path)
    found = import_dir(con, root, recursive=True)
    if os.path.isdir(local_dir(root)):
        found += import_dir(con, local_dir(root))
    for f in list_playlist_files(legacy):
        name = read(os.path.join(legacy, f))["name"]
        # Only what a download made: a hand-made playlist in that folder may
        # name files the catalog does not hold, and rewriting it would drop
        # them.
        if con.execute("SELECT 1 FROM download_url WHERE playlist_name = ?",
                       (name,)).fetchone():
            write_local(con, name, root)
    return found


DEFAULT_TEMPLATE = "{name}.m3u8"
_PL_EXT = (".m3u8", ".m3u")


def filename_for(name, template=DEFAULT_TEMPLATE):
    """Render a playlist's filename the way the player itself spells it.

    A player writes its own playlists under its own convention - HiBy's are
    "chill-<owner>.m3u8" - and a writer that ignores that convention does
    not fix the file it was meant to replace, it adds a second one beside
    it. The device therefore owns a filename template, of which "{name}" is
    the playlist's own name; everything around it is the device's spelling.

    The name is sanitised as a single path component before substitution,
    so a playlist called "AC/DC live" cannot turn the filename into a path.
    """
    template = (template or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    try:
        rendered = template.replace("{name}", safe_component(norm_name(name)))
    except Exception:
        rendered = safe_component(norm_name(name)) + ".m3u8"
    if not rendered.lower().endswith(_PL_EXT):
        rendered += ".m3u8"
    return safe_component(rendered)


def safe_filename(name):
    """Back-compatible default spelling: "<name>.m3u8"."""
    return filename_for(name, DEFAULT_TEMPLATE)


def template_of(device):
    """Read a device row's playlist template, tolerating an older row."""
    try:
        keys = device.keys()
    except AttributeError:
        keys = device
    if "playlist_template" in keys:
        return device["playlist_template"] or DEFAULT_TEMPLATE
    return DEFAULT_TEMPLATE


def list_playlist_files(directory):
    """Playlist filenames sitting directly in a folder. Never recurses."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().endswith(_PL_EXT)
                  and os.path.isfile(os.path.join(directory, n)))


def infer_template(filenames, known_names):
    """Work out a device's own playlist naming from what is already there.

    Every filename that contains one of the catalog's playlist names votes
    for the prefix and suffix wrapped around it; the winning pair is the
    device's template. Returns (template, matches, total) - or (None, 0, n)
    when nothing lines up, which is the honest answer for a card whose
    playlists came from somewhere else entirely.

    Longest name first, so "chill" does not claim a file belonging to
    "chill winter" and report a suffix of " winter".
    """
    names = sorted({norm_name(n) for n in known_names if norm_name(n)},
                   key=len, reverse=True)
    votes = {}
    matched = 0
    for fn in filenames:
        stem, dot, ext = norm_name(fn).rpartition(".")
        if not dot:
            continue
        low = stem.casefold()
        for n in names:
            i = low.find(n.casefold())
            if i < 0:
                continue
            key = (stem[:i], stem[i + len(n):], ext)
            votes[key] = votes.get(key, 0) + 1
            matched += 1
            break
    if not votes:
        return None, 0, len(filenames)
    (prefix, suffix, ext), count = max(votes.items(), key=lambda kv: kv[1])
    return f"{prefix}{{name}}{suffix}.{ext}", count, len(filenames)
