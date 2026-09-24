"""Lyrics, from LRCLIB, embedded into the file.

LRCLIB (lrclib.net) is a free, open database of lyrics that needs no key.
It is asked by artist, title, album and length, and answers with the lyrics
time-synced (LRC) when it has them and plain when it does not. The synced
text is preferred: the player reads LRC out of the tag as happily as out of
a sidecar file, and a line that follows the song is the point of having
them on a DAP.

Only the file carries them. The catalog does not: lyrics are the one field
nobody sorts, filters or edits a library by, and keeping a second copy in
SQLite would only give the two a way to disagree.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .enrich import HTTP_TIMEOUT, USER_AGENT, LookupError_

LRCLIB_ROOT = "https://lrclib.net/api/get"
# LRCLIB publishes no limit. It does answer 503 when it is busy, so a run
# asks politely - one at a time, with a gap - and waits when told to.
INTERVAL = 0.3
ATTEMPTS = 3

_last = [0.0]


def _get(params, opener, sleep):
    url = LRCLIB_ROOT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(ATTEMPTS):
        gap = time.monotonic() - _last[0]
        if gap < INTERVAL:
            sleep(INTERVAL - gap)
        _last[0] = time.monotonic()
        try:
            with opener(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (429, 503) and attempt < ATTEMPTS - 1:
                sleep(2.0 * (attempt + 1))
                continue
            raise LookupError_(f"LRCLIB returned {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt < ATTEMPTS - 1:
                sleep(1.0)
                continue
            raise LookupError_(str(exc)) from exc
    return None


def fetch(artist, title, album=None, duration=None, opener=None, sleep=None):
    """The lyrics for one track, synced when available, or None.

    Asked with the length first, which is how LRCLIB tells a radio edit from
    the album cut, and once more without it when that finds nothing: a
    downloaded file is often a second or two off whatever LRCLIB holds.
    Raises LookupError_ when the service cannot be reached.
    """
    if not artist or not title:
        return None
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    base = {"artist_name": artist, "track_name": title}
    if album:
        base["album_name"] = album
    tries = [dict(base, duration=int(round(duration)))] if duration else []
    tries.append(base)
    for params in tries:
        got = _get(params, opener, sleep)
        if not got or got.get("instrumental"):
            continue
        text = (got.get("syncedLyrics") or got.get("plainLyrics") or "").strip()
        if text:
            return text
    return None


def fill(con, progress=None, opener=None, sleep=None):
    """Embed lyrics into every catalogued file that has none. Returns counts.

    Asked from what the catalog says the track is, so an unidentified file
    whose artist is a channel name simply finds nothing. A file LRCLIB has
    nothing for is asked again on the next run: lyrics get added there all
    the time.
    """
    from . import tags

    rows = con.execute(
        "SELECT content_key, path, title, artist, album, duration FROM track "
        "WHERE size > 0 AND title IS NOT NULL AND artist IS NOT NULL "
        "GROUP BY content_key ORDER BY rel_path").fetchall()
    counts = {"considered": 0, "written": 0, "none": 0, "had": 0,
              "failed": 0, "errors": []}
    consecutive = 0
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows))
        if tags.has_lyrics(r["path"]):
            counts["had"] += 1
            continue
        counts["considered"] += 1
        try:
            text = fetch(r["artist"], r["title"], r["album"], r["duration"],
                         opener=opener, sleep=sleep)
            consecutive = 0
        except LookupError_ as exc:
            counts["failed"] += 1
            consecutive += 1
            if str(exc) not in counts["errors"]:
                counts["errors"].append(str(exc))
            if consecutive >= 3:
                break
            continue
        if not text:
            counts["none"] += 1
            continue
        try:
            new_key = tags.write(r["path"], {"lyrics": text})
        except tags.TagWriteError as exc:
            counts["failed"] += 1
            counts["errors"].append(str(exc))
            continue
        tags.rekey(con, r["content_key"], r["path"], new_key)
        con.commit()
        counts["written"] += 1
    return counts
