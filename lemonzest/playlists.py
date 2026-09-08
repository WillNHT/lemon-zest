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
        matched = unmatched = 0
        origin = "local"
        # How many of these entries actually resolve to catalogued files?
        incoming_matched = sum(
            1 for e in pl["entries"] if e["abs_path"] and e["abs_path"] in by_path
        )
        if pl["source_uri"] and "apple.com" in pl["source_uri"]:
            origin = "apple_music"
        elif pl["source_uri"] and "spotify.com" in pl["source_uri"]:
            origin = "spotify"
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

        con.execute("DELETE FROM playlist_entry WHERE playlist_id=?", (pid,))
        for pos, e in enumerate(pl["entries"]):
            tid = by_path.get(e["abs_path"]) if e["abs_path"] else None
            if tid:
                matched += 1
            else:
                unmatched += 1
            con.execute(
                "INSERT INTO playlist_entry(playlist_id,pos,track_id,raw_path,"
                "title_hint,duration,source_uri) VALUES (?,?,?,?,?,?,?)",
                (pid, pos, tid, e["raw_path"], e["title_hint"], e["duration"],
                 e["source_uri"]),
            )
        results.append({"name": pl["name"], "file": f, "total": len(pl["entries"]),
                        "matched": matched, "unmatched": unmatched,
                        "source_uri": pl["source_uri"], "mode": "imported",
                        "enriched": 0})
    con.commit()
    return results


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
