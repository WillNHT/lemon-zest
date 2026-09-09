"""Metadata enrichment: reconcile what a file claims with what a source knows.

The catalog's tags are only as good as whatever wrote them, and this library
has two populations with opposite problems. The Apple-sourced tree carries an
ISRC on 98.7% of its files - an exact recording identifier, which makes the
top rung of the ladder free. The yt-dlp tree carries none at all, and for a
plain YouTube upload the ``artist`` field is the *channel name*: a folder
called "Nemu" holding a KIRINJI track, because yt-dlp fell back to
``uploader`` when no ``artist`` tag existed.

The ladder, in the order it is tried:

  1. **Backfill** (offline, free). The same recording often exists twice in
     the library - once from a tagged source, once from a download. Copying
     the ISRC across on an exact artist/title/duration match costs no network
     call and promotes files onto rung 2.
  2. **ISRC lookup**. An exact identifier, so a hit is certain: no fuzzy
     matching, no threshold, confidence 1.0.
  3. **Text search**. Normalised artist and title against MusicBrainz, scored
     with a duration window. Fuzzy, so it is gated on confidence and anything
     doubtful lands in the review queue rather than in the tags.

Rung 4 - AcoustID fingerprinting - is the only source that ignores what a
file claims and listens to it instead, which is exactly the untagged-download
case. It is deliberately not here: it needs the ``fpcalc`` binary bundled
alongside the app, which is a packaging change rather than a code one. The
``Source`` seam below is where it slots in.

Nothing in this module writes to an audio file. Applying an enrichment to the
catalog is one call; writing it back into the file is a separate, explicit
one in ``tags.py``, because those have very different blast radii.
"""
import difflib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

# MusicBrainz asks for one request per second from a client that identifies
# itself, and answers 503 to anything that does not. Both are honoured: the
# limiter is global to the process, and the User-Agent names the project and
# a contact URL as their guidelines require.
MB_ROOT = "https://musicbrainz.org/ws/2"
CAA_ROOT = "https://coverartarchive.org"
USER_AGENT = (f"lemon-zest/{__version__} "
              "( https://github.com/WillNHT/lemon-zest )")
MB_INTERVAL = 1.05          # seconds between requests, with a little slack
HTTP_TIMEOUT = 30.0
# One request timing out is weather; three in a row is the service being
# down, and grinding through two thousand files to collect two thousand
# identical errors helps nobody. Isolated failures are skipped and reported.
MAX_CONSECUTIVE_FAILURES = 3

# A text-search match at or above AUTO is trustworthy enough to apply without
# being looked at; between REVIEW and AUTO it is stored as a candidate for a
# person to judge; below REVIEW it is not worth keeping. The numbers are set
# so that an exact title, an exact artist and a duration inside two seconds
# clears AUTO, while a title match with a wrong-length recording does not.
AUTO = 0.90
REVIEW = 0.62

# Past this the two recordings are not the same performance, whatever the
# strings say - a radio edit, a live version, or a different song entirely.
MAX_DURATION_DELTA = 12.0

# Fields an enrichment may propose. Deliberately not `purl` or anything
# describing the file itself: a source knows about the recording, not about
# where this copy came from.
ENRICHABLE = ("title", "artist", "album", "album_artist",
              "track_no", "disc_no", "year", "isrc")


# ------------------------------------------------------------ normalisation

# Anything after these markers is a qualifier rather than part of the title.
_QUALIFIER = re.compile(
    r"\s*[\(\[\-–—]\s*(official|lyric|audio|video|mv|m/v|full|hd|4k|hq|"
    r"visuali[sz]er|music\s+video|色情|topic)\b.*$", re.I)
# "feat. X", "ft X", "with X" - the featured artist belongs in the credit
# list, not in the title being matched.
_FEAT = re.compile(r"\s*[\(\[]?\s*(feat|ft|featuring|with)\.?\s+[^)\]]*[\)\]]?\s*$",
                   re.I)
# Apostrophes are deleted rather than spaced out, because "Don't" and "Dont"
# are the same word and "don t stop" would not match "dont stop". Every other
# punctuation mark becomes a space, since it usually stands where a space
# stands on the other side.
_ELIDE = re.compile(r"['’ʼ´`]")
_PUNCT = re.compile(r"[^\w\s]", re.U)
_SPACE = re.compile(r"\s+")


def fold(text):
    """Reduce a title or artist to what two spellings of it have in common.

    Case, accents, punctuation and spacing all vary between a tag written by
    Apple and a string typed into MusicBrainz, and none of them carry meaning
    for a match. Accent folding is what makes "Bjork" and "Björk" the same
    string, and NFKD is what makes the full-width and half-width spellings of
    a Japanese title agree.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", _ELIDE.sub("", text.lower()))
    return _SPACE.sub(" ", text).strip()


def clean_title(title):
    """Strip the decoration a video title carries and a track title does not."""
    if not title:
        return ""
    out = _QUALIFIER.sub("", str(title))
    out = _FEAT.sub("", out)
    return out.strip(" -–—") or str(title).strip()


def credits(artist):
    """Split an artist field into its individual credits, primary first.

    An art track's ``artist`` is often every credited writer - the library
    has a folder named for seven of them - while the album artist is the one
    name a person would search for. Splitting on the usual separators gives
    the matcher every name to try instead of one long string that matches
    nothing.
    """
    if not artist:
        return []
    # The word separators carry \b *before* the optional full stop, not after
    # it: "feat." ends on a non-word character, so a trailing \b never fires
    # and the stop is left glued to the next name.
    parts = re.split(
        r"\s*(?:[;,&/]|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b|\bx\b"
        r"|\band\b)\s*", str(artist), flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


def parse_video_title(title):
    """Read "ARTIST - TITLE" out of a video title. Returns (artist, title).

    The fallback for a plain YouTube upload, where the only metadata is the
    title the uploader typed. Returns ``(None, title)`` when there is no
    separator to trust - guessing here would put the whole string in the
    artist field, which is worse than leaving it alone.
    """
    if not title:
        return None, title
    text = clean_title(title)
    for sep in (" - ", " – ", " — ", " ~ ", "「", "|"):
        if sep in text:
            left, _, right = text.partition(sep)
            left, right = left.strip(), right.strip(" 」|")
            # A separator inside one half's parentheses is not a split point,
            # and neither half may be empty or absurdly long.
            if left and right and len(left) <= 80:
                return left, right
    return None, text


# ----------------------------------------------------------------- scoring

def _ratio(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _best_artist_ratio(local_artists, remote_artists):
    """Best pairwise agreement between two credit lists.

    Pairwise rather than whole-string, because one side routinely lists seven
    writers where the other lists one performer. Agreeing on any single name
    is the signal; the lists having different lengths is not evidence against.
    """
    best = 0.0
    for a in local_artists:
        for b in remote_artists:
            best = max(best, _ratio(fold(a), fold(b)))
    return best


def score(local, remote):
    """How confident we are that ``remote`` describes ``local``.

    ``local`` is a catalog row (or any mapping with its column names);
    ``remote`` is a candidate's ``fields`` dict plus an optional ``duration``.

    Duration is weighted heavily and can veto outright, because it is the one
    field neither side had a chance to mistype. Two recordings whose lengths
    differ by more than a few seconds are different recordings even when
    every string agrees - which is precisely how a matcher confidently
    attaches an album track to its live version.
    """
    ld = local["duration"] if "duration" in local.keys() else None
    rd = remote.get("duration")
    if ld and rd:
        delta = abs(float(ld) - float(rd))
        if delta > MAX_DURATION_DELTA:
            return 0.0
        if delta <= 2.0:
            dur = 1.0
        elif delta <= 5.0:
            dur = 0.7
        else:
            dur = 0.3
    else:
        # No duration on one side is missing evidence, not bad evidence, so
        # it scores neutral rather than zero - otherwise nothing without a
        # length could ever clear the threshold.
        dur = 0.5

    local_title = fold(clean_title(local["title"]))
    remote_title = fold(remote.get("title"))
    title = _ratio(local_title, remote_title)

    local_credits = credits(local["artist"]) + credits(
        local["album_artist"] if "album_artist" in local.keys() else None)
    artist = _best_artist_ratio(local_credits, remote.get("artists") or [])

    # A title that does not match at all is fatal however good the rest is.
    if title < 0.5:
        return 0.0
    return round(0.42 * title + 0.28 * artist + 0.30 * dur, 4)


# -------------------------------------------------------- MusicBrainz client

class LookupError_(RuntimeError):
    """A source could not be reached. Never fatal: enrichment is optional."""


class MusicBrainz:
    """Minimal MusicBrainz client over urllib.

    urllib rather than requests on purpose - this ships as a frozen
    executable, and a dependency that pulls in its own certificate bundle is
    a packaging problem for one GET per second.
    """

    def __init__(self, contact=None, interval=MB_INTERVAL, opener=None):
        self.interval = interval
        self._last = 0.0
        self._opener = opener or urllib.request.urlopen
        self.user_agent = (f"lemon-zest/{__version__} ( {contact} )"
                           if contact else USER_AGENT)

    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()

    def get(self, path, **params):
        params["fmt"] = "json"
        url = f"{MB_ROOT}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        for attempt in range(3):
            self._wait()
            try:
                with self._opener(req, timeout=HTTP_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                # 503 is MusicBrainz saying "slow down", not "go away".
                if exc.code in (429, 503) and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise LookupError_(f"MusicBrainz returned {exc.code}") from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise LookupError_(str(exc)) from exc
        return None

    def by_isrc(self, isrc):
        """Every recording carrying this ISRC. Usually exactly one."""
        data = self.get(f"isrc/{urllib.parse.quote(isrc)}",
                        inc="artist-credits+releases")
        return (data or {}).get("recordings") or []

    def search(self, artist, title, duration=None, limit=15):
        """Lucene search over recordings.

        Fifteen rather than a handful, because a well-known song is in
        MusicBrainz dozens of times and the studio recording is not reliably
        in the first five - it loses to whichever compilation's tracklist was
        entered most recently. The extra results cost nothing: they arrive in
        the same request, and the ranking in ``_best_candidate`` is what
        decides between them.
        """
        terms = [f'recording:"{_lucene(title)}"']
        if artist:
            terms.append(f'artist:"{_lucene(artist)}"')
        if duration:
            # MusicBrainz stores lengths in milliseconds; a window rather
            # than a value, since our own duration is a decoded estimate.
            ms = int(float(duration) * 1000)
            terms.append(f"dur:[{max(0, ms - 7000)} TO {ms + 7000}]")
        data = self.get("recording", query=" AND ".join(terms), limit=limit)
        return (data or {}).get("recordings") or []


def _lucene(text):
    """Escape a value for a Lucene query string."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", str(text or ""))


# ------------------------------------------------------- reading a recording

_SKIP_TYPES = {"compilation", "live", "remix", "dj-mix", "mixtape/street",
               "soundtrack", "interview", "demo", "audiobook", "spokenword"}


# A release with more tracks than this is a box set or an anthology, not the
# album a single song came from. Used only as a tiebreak, because a long
# genuine album should still beat a mislabelled compilation on the checks
# above it.
BIG_RELEASE = 30


def _release_credit(release):
    credit = release.get("artist-credit") or []
    if credit and isinstance(credit[0], dict) and credit[0].get("artist"):
        return credit[0]["artist"]["name"]
    return None


def _pick_release(recording, prefer_album=None):
    """Choose which release to take album, year and track number from.

    A popular recording is on the album, the single, a soundtrack, three
    compilations and a greatest-hits, and MusicBrainz returns all of them.
    Picking wrongly here is the failure mode that matters most, because the
    recording itself was identified with certainty - so a bad album arrives
    wearing a confidence of 1.00.

    ``prefer_album`` is what the file already claims. When one of the
    releases is that album, it wins outright: the file's own tag is better
    evidence of which pressing this copy came from than any ranking, and
    without this rule a correctly tagged "Cigarettes After Sex" track gets
    relabelled with the HBO soundtrack it also appeared on.
    """
    want = fold(prefer_album) if prefer_album else None
    credits_ = [c["artist"]["name"] for c in recording.get("artist-credit") or []
                if isinstance(c, dict) and c.get("artist")]
    performer = fold(credits_[0]) if credits_ else None

    best, best_rank = None, None
    for rel in recording.get("releases") or []:
        group = rel.get("release-group") or {}
        secondary = {s.lower() for s in (group.get("secondary-types") or [])}
        primary = (group.get("primary-type") or "").lower()

        # Whose record is it? A song's own album is credited to the artist
        # who recorded it; "Hot Party Summer 2007" is credited to Various
        # Artists. This is the check that does the real work, because the
        # search API routinely omits secondary-types - so the compilation
        # filter below has nothing to filter on, and without this rule the
        # anthology wins on date and the track lands as number 343 of a box
        # set. An unknown credit ranks between a match and a mismatch: it is
        # missing evidence, not evidence against.
        rel_credit = _release_credit(rel)
        if performer and rel_credit:
            by_artist = 0 if fold(rel_credit) == performer else 2
        else:
            by_artist = 1

        rank = (
            0 if want and fold(rel.get("title")) == want else 1,
            by_artist,
            1 if secondary & _SKIP_TYPES else 0,      # compilations last
            0 if primary == "album" else 1 if primary == "ep" else 2,
            1 if (rel.get("track-count") or 0) > BIG_RELEASE else 0,
            rel.get("date") or "9999",                # earliest release wins
        )
        if best_rank is None or rank < best_rank:
            best, best_rank = rel, rank
    return best


def _track_position(release):
    """Track and disc number, when the release carried its medium list."""
    media = release.get("media") or []
    for i, medium in enumerate(media, 1):
        for track in medium.get("track") or medium.get("tracks") or []:
            num = track.get("number") or track.get("position")
            try:
                return int(str(num).lstrip("0") or 0), medium.get("position", i)
            except (TypeError, ValueError):
                return None, medium.get("position", i)
    return None, None


def recording_fields(recording, prefer_album=None):
    """Flatten a MusicBrainz recording into the columns the catalog stores.

    The result separates two things a single confidence score cannot
    describe. ``fields`` are what the *recording* says - title, artist, ISRC
    - and are as certain as the match itself. ``release_fields`` are what one
    of its releases says, and choosing between forty of them is a guess
    however sure we are of the recording, so ``_apply_fields`` only ever
    fills those into columns the file left empty.
    """
    artists = [c["artist"]["name"] for c in recording.get("artist-credit") or []
               if isinstance(c, dict) and c.get("artist")]
    joined = "".join(
        (c["artist"]["name"] + (c.get("joinphrase") or ""))
        if isinstance(c, dict) and c.get("artist") else str(c)
        for c in recording.get("artist-credit") or []).strip()

    fields = {"title": recording.get("title"), "artist": joined or None}
    length = recording.get("length")
    out = {"fields": fields, "artists": artists,
           "duration": (length / 1000.0) if length else None,
           "mbid": recording.get("id"), "release_id": None}

    isrcs = recording.get("isrcs") or []
    if isrcs:
        fields["isrc"] = isrcs[0]

    release_fields = {}
    release = _pick_release(recording, prefer_album=prefer_album)
    if release:
        out["release_id"] = release.get("id")
        release_fields["album"] = release.get("title")
        date = release.get("date")
        if date:
            release_fields["year"] = str(date)[:10]
        credit = release.get("artist-credit") or []
        if credit and isinstance(credit[0], dict) and credit[0].get("artist"):
            release_fields["album_artist"] = credit[0]["artist"]["name"]
        tno, dno = _track_position(release)
        if tno:
            release_fields["track_no"] = tno
        if dno:
            release_fields["disc_no"] = dno

    # The album artist is the single searchable name, so fall back to the
    # first credit rather than leaving it empty and letting the path template
    # use the seven-writer string.
    if not release_fields.get("album_artist") and artists:
        release_fields["album_artist"] = artists[0]

    # How good the release we ended up with is, used to break ties between
    # recordings that score identically. -1 is somebody else's record
    # (Various Artists), 0 is a release that did not say, 1 is the artist's
    # own. See ``_best_candidate``.
    rel_credit = _release_credit(release) if release else None
    performer = fold(artists[0]) if artists else None
    if rel_credit and performer:
        out["release_rank"] = 1 if fold(rel_credit) == performer else -1
    else:
        out["release_rank"] = 0

    clean = lambda d: {k: v for k, v in d.items()
                       if v not in (None, "") and k in ENRICHABLE}
    out["fields"] = clean(fields)
    out["release_fields"] = clean(release_fields)
    return out


def _best_candidate(records, row, album):
    """Pick the best of several recordings that all describe this track.

    A popular song is in MusicBrainz many times over - the studio recording,
    the one entered from a compilation's tracklist, the one from a radio
    session - and several of them match a file's title, artist and length
    exactly. They therefore score identically, so the score alone cannot
    choose, and taking whichever came back first is how a track ends up
    credited to "Hot Party Summer 2007": the anthology's copy of the
    recording is a perfectly good match whose only release is the anthology.

    Ties are broken on the quality of the release each one leads to, which is
    the thing the score never looked at.
    """
    best, best_key = None, None
    for rec in records:
        cand = recording_fields(rec, prefer_album=album)
        s = score(row, {**cand["fields"], "artists": cand["artists"],
                        "duration": cand["duration"]})
        if s <= 0:
            continue
        key = (s, cand["release_rank"], 1 if cand["fields"].get("isrc") else 0)
        if best_key is None or key > best_key:
            best, best_key = cand, key
    return (best, best_key[0]) if best else (None, 0.0)


# Fields that describe a release rather than the recording. They are filled
# in where a file is silent and never used to overwrite what it already says.
RELEASE_FIELDS = ("album", "album_artist", "year", "track_no", "disc_no")


# ------------------------------------------------------ the offline backfill

def backfill_isrc(con, dry_run=False):
    """Copy ISRCs onto untagged twins of already-identified recordings.

    Costs nothing and needs no network. A file with no ISRC is matched
    against one that has an ISRC by folded artist, folded title and a
    duration inside two seconds; on a unique match the identifier is copied.
    Ambiguity is declined rather than guessed: if two different ISRCs claim
    the same (artist, title, length), neither is right enough to write.

    Returns counts. This is the cheapest thing in the module and it promotes
    files onto the exact-identifier rung, so it runs before any lookup.
    """
    have, want = [], []
    for row in con.execute(
            "SELECT id, content_key, title, artist, album, album_artist, year, "
            "duration, isrc FROM track "
            "WHERE size > 0 AND title IS NOT NULL AND title != ''"):
        (have if row["isrc"] else want).append(row)

    index = {}
    for row in have:
        for name in credits(row["artist"]) + credits(row["album_artist"]):
            key = (fold(name), fold(clean_title(row["title"])))
            index.setdefault(key, []).append(row)

    counts = {"scanned": len(want), "matched": 0, "ambiguous": 0}
    now = time.time()
    for row in want:
        cands = []
        for name in credits(row["artist"]) + credits(row["album_artist"]):
            key = (fold(name), fold(clean_title(row["title"])))
            for other in index.get(key, ()):
                if row["duration"] and other["duration"]:
                    if abs(row["duration"] - other["duration"]) > 2.0:
                        continue
                cands.append(other)
        isrcs = {c["isrc"] for c in cands}
        if len(isrcs) != 1:
            if len(isrcs) > 1:
                counts["ambiguous"] += 1
            continue
        counts["matched"] += 1
        if dry_run:
            continue
        twin = cands[0]
        fields = {"isrc": twin["isrc"]}
        # The twin's album and album artist are worth taking too, but only
        # where this file has nothing: a download that already knows its
        # album should not be relabelled by a compilation copy.
        for f in ("album", "album_artist", "year"):
            if not row[f] and twin[f]:
                fields[f] = twin[f]
        _store(con, row["content_key"], "backfill", 1.0, fields,
               mbid=None, release_id=None, now=now, status="candidate")
    if not dry_run:
        con.commit()
    return counts


# -------------------------------------------------------------- the enricher

def _store(con, content_key, source, confidence, fields, mbid, release_id,
           now, status):
    con.execute(
        "INSERT INTO enrichment(content_key, status, source, confidence, mbid, "
        "release_id, fields, fetched_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(content_key) DO UPDATE SET status=excluded.status, "
        "source=excluded.source, confidence=excluded.confidence, "
        "mbid=excluded.mbid, release_id=excluded.release_id, "
        "fields=excluded.fields, fetched_at=excluded.fetched_at",
        (content_key, status, source, confidence, mbid, release_id,
         json.dumps(fields, ensure_ascii=False), now))


def pending(con, limit=None, root=None, redo=False):
    """Tracks worth looking up, most-improvable first.

    A file already carrying an ISRC, an album and a real artist has little to
    gain, so the ones missing the most are done first - which matters when a
    run is rate-limited to one lookup per second and the user stops it early.
    """
    where = ["t.size > 0"]
    params = []
    if root:
        where.append("t.root = ?")
        params.append(root)
    if not redo:
        # Anything already decided is left alone; only "none" is retried,
        # since that is a lookup that found nothing rather than a judgement.
        where.append("(e.status IS NULL OR e.status = 'none')")
    sql = (
        "SELECT t.* FROM track t "
        "LEFT JOIN enrichment e ON e.content_key = t.content_key "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY (t.isrc IS NOT NULL) + (t.album IS NOT NULL) "
        "       + (t.album_artist IS NOT NULL) + (t.track_no IS NOT NULL), "
        "t.rel_path"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params).fetchall()


def enrich_track(con, row, client, now=None):
    """Look one track up and store the best candidate. Returns its status.

    ``'applied'`` means the match was certain enough to write into the
    catalog on the spot; ``'candidate'`` means it wants a human; ``'none'``
    means nothing good enough came back.
    """
    now = now or time.time()
    album = row["album"] if "album" in row.keys() else None

    if row["isrc"]:
        for rec in client.by_isrc(row["isrc"]):
            cand = recording_fields(rec, prefer_album=album)
            # An ISRC is an exact identifier, so the only check left is that
            # the file is not mis-tagged with someone else's: a wildly
            # different length means the tag is wrong, not the database.
            if cand["duration"] and row["duration"]:
                if abs(cand["duration"] - row["duration"]) > MAX_DURATION_DELTA:
                    continue
            fields = {**cand["fields"], **cand["release_fields"]}
            _store(con, row["content_key"], "isrc", 1.0, fields,
                   cand["mbid"], cand["release_id"], now, "applied")
            _apply_fields(con, row["content_key"], fields, now)
            con.commit()
            return "applied"

    best, best_score = None, 0.0
    for artist, title in _search_attempts(row):
        cand, s = _best_candidate(client.search(artist, title, row["duration"]),
                                  row, album)
        if s > best_score:
            best, best_score = cand, s
        # A confident hit ends the ladder; the later spellings exist only
        # because the first one found nothing.
        if best_score >= AUTO:
            break

    if not best or best_score < REVIEW:
        _store(con, row["content_key"], "musicbrainz", best_score or 0.0, {},
               None, None, now, "none")
        con.commit()
        return "none"

    fields = {**best["fields"], **best["release_fields"]}
    status = "applied" if best_score >= AUTO else "candidate"
    _store(con, row["content_key"], "musicbrainz", best_score, fields,
           best["mbid"], best["release_id"], now, status)
    if status == "applied":
        _apply_fields(con, row["content_key"], fields, now)
    con.commit()
    return status


_PARENTHETICAL = re.compile(r"\s*[\(（\[][^)）\]]*[\)）\]]")


def _search_terms(row):
    """What to search for: the tags when they are usable, the title when not.

    A download whose artist is the channel name has a title of the form
    "KIRINJI - 時間がない", and searching for the channel finds nothing. When
    the title parses into two halves, the parsed artist is tried instead -
    the tag is not evidence when we know how it was guessed.
    """
    title = clean_title(row["title"])
    artist = row["album_artist"] or row["artist"]
    parsed_artist, parsed_title = parse_video_title(row["title"])
    if parsed_artist:
        # Prefer the parsed pair when the tag artist looks like a channel:
        # nothing in the title mentions it.
        if not artist or fold(parsed_artist) not in fold(artist):
            if fold(artist or "") not in fold(row["title"] or ""):
                return parsed_artist, parsed_title
    return artist, title


def _search_attempts(row):
    """Spellings of one track to try, in order, until something matches.

    A search is a string comparison inside somebody else's index, so the
    exact spelling decides whether there is a hit at all. Two rewrites are
    worth a second request:

      * **without the parenthetical.** A download titled "時間がない (Jikanga
        Nai)" carries a romanisation the database does not; dropping it is
        the difference between no result and the right one.
      * **compatibility-normalised.** Full-width titles - "ＨＥＡＲＴＢＲＯＫＥＮ" -
        are the same characters to a reader and different bytes to a search
        index.

    Only spellings that actually differ are tried, so a plain ASCII title
    still costs exactly one request.
    """
    artist, title = _search_terms(row)
    if not title:
        return []
    seen, out = set(), []
    for cand in (title,
                 _PARENTHETICAL.sub("", title).strip(),
                 _SPACE.sub(" ", unicodedata.normalize("NFKC", title)).strip()):
        if cand and cand not in seen:
            seen.add(cand)
            out.append((artist, cand))
    return out


def _apply_fields(con, content_key, fields, now):
    """Write proposed values onto every track row sharing this content key.

    Two rules, and the second is the one that matters:

      * a hand-typed override always wins, whatever the source said;
      * a **release**-derived field only fills a column the file left empty.

    That second rule exists because identifying a recording and choosing
    which of its releases this copy came from are different questions with
    very different certainties, and a single confidence score describes only
    the first. A correctly tagged "Cigarettes After Sex" track matched with
    total confidence to its own recording would otherwise be relabelled with
    the HBO soundtrack that recording also appears on - a confident, wrong
    answer overwriting a right one, which is the failure this whole layer is
    supposed to prevent.
    """
    rows = con.execute("SELECT * FROM track WHERE content_key = ?",
                       (content_key,)).fetchall()
    overrides = {r["field"]: r["value"] for r in con.execute(
        "SELECT field, value FROM track_override WHERE content_key = ?",
        (content_key,))}
    for row in rows:
        sets, params = [], []
        for field, value in fields.items():
            if field not in ENRICHABLE or field in overrides:
                continue
            if field in RELEASE_FIELDS and row[field] not in (None, ""):
                continue
            sets.append(f"{field} = ?")
            params.append(value)
        for field, value in overrides.items():
            if field in ENRICHABLE:
                sets.append(f"{field} = ?")
                params.append(value)
        if sets:
            con.execute(f"UPDATE track SET {','.join(sets)} WHERE id = ?",
                        params + [row["id"]])
    con.execute("UPDATE enrichment SET applied_at = ? WHERE content_key = ?",
                (now, content_key))


def accept(con, content_key):
    """Apply a stored candidate to the catalog."""
    row = con.execute("SELECT * FROM enrichment WHERE content_key = ?",
                      (content_key,)).fetchone()
    if not row:
        return False
    fields = json.loads(row["fields"])
    if not fields:
        return False
    now = time.time()
    con.execute("UPDATE enrichment SET status='applied' WHERE content_key=?",
                (content_key,))
    _apply_fields(con, content_key, fields, now)
    con.commit()
    return True


def reject(con, content_key):
    """Mark a candidate wrong. It is not offered again, and not re-fetched."""
    con.execute("UPDATE enrichment SET status='rejected' WHERE content_key=?",
                (content_key,))
    con.commit()


def override(con, content_key, **fields):
    """Record a hand-typed value. Outranks every source, now and later."""
    now = time.time()
    for field, value in fields.items():
        if field not in ENRICHABLE:
            continue
        con.execute(
            "INSERT INTO track_override(content_key, field, value, set_at) "
            "VALUES (?,?,?,?) ON CONFLICT(content_key, field) DO UPDATE SET "
            "value=excluded.value, set_at=excluded.set_at",
            (content_key, field, value, now))
    _apply_fields(con, content_key, {}, now)
    con.commit()


def review_queue(con, limit=200):
    """Candidates awaiting a decision, least confident first.

    Least confident first because those are the ones where a person adds the
    most: the 0.89 matches are nearly all right, and the 0.63s are where a
    matcher quietly attaches the wrong recording.
    """
    rows = con.execute(
        "SELECT e.*, t.id AS track_id, t.rel_path, t.title, t.artist, "
        "       t.album, t.album_artist, t.duration, t.path "
        "FROM enrichment e JOIN track t ON t.content_key = e.content_key "
        "WHERE e.status = 'candidate' ORDER BY e.confidence, t.rel_path "
        "LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["proposed"] = json.loads(r["fields"] or "{}")
        out.append(d)
    return out


def summary(con):
    """Counts for the status line: how much of the library is identified."""
    one = lambda q, *a: con.execute(q, a).fetchone()[0]
    return {
        "tracks": one("SELECT COUNT(*) FROM track WHERE size > 0"),
        "with_isrc": one("SELECT COUNT(*) FROM track WHERE isrc IS NOT NULL"),
        "applied": one("SELECT COUNT(*) FROM enrichment WHERE status='applied'"),
        "candidates": one("SELECT COUNT(*) FROM enrichment WHERE status='candidate'"),
        "rejected": one("SELECT COUNT(*) FROM enrichment WHERE status='rejected'"),
        "unmatched": one("SELECT COUNT(*) FROM enrichment WHERE status='none'"),
        "overrides": one("SELECT COUNT(DISTINCT content_key) FROM track_override"),
    }


def run(con, client=None, root=None, limit=None, redo=False, write_tags=False,
        artwork=False, progress=None, contact=None):
    """Enrich a library: backfill, then look up, then optionally write back.

    One pass, in the order that costs least. The offline backfill runs first
    because it is free and it promotes files onto the exact-identifier rung,
    so fewer of them need the slow, fuzzy path afterwards.

    ``progress(done, total, status)`` is called per track. An isolated lookup
    failure is skipped and counted - one request timing out over a long run
    is weather, and losing forty minutes of work to it would be absurd - but
    ``MAX_CONSECUTIVE_FAILURES`` in a row ends the run, because at that point
    the service is down rather than slow.

    Returns counts, including what was written to disk. Nothing is written to
    an audio file unless ``write_tags`` is set.
    """
    client = client or MusicBrainz(contact=contact)
    counts = dict(backfilled=0, applied=0, candidates=0, unmatched=0,
                  written=0, write_failed=0, failed=0, stopped=None)

    counts["backfilled"] = backfill_isrc(con)["matched"]

    rows = pending(con, limit=limit, root=root, redo=redo)
    total = len(rows)
    consecutive = 0
    for i, row in enumerate(rows, 1):
        try:
            status = enrich_track(con, row, client)
        except LookupError_ as exc:
            counts["failed"] += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                counts["stopped"] = f"{exc} ({consecutive} in a row)"
                break
            if progress:
                progress(i, total, "failed")
            continue
        consecutive = 0
        counts[{"applied": "applied", "candidate": "candidates",
                "none": "unmatched"}[status]] += 1
        if status == "applied" and write_tags:
            ok = write_back(con, row["content_key"], artwork=artwork)
            counts["written" if ok else "write_failed"] += 1
        if progress:
            progress(i, total, status)
    return counts


def write_back(con, content_key, artwork=False):
    """Push an applied enrichment into the audio file itself.

    Returns True on success. A failure is reported, not raised: one file with
    an exotic container must not end a run over two thousand of them.
    """
    from . import tags as tags_mod   # imported late: enrichment works without it

    # Driven from track, not from enrichment: a hand-typed correction on a
    # file no source could identify is exactly the case that most needs
    # writing out, and it has no enrichment row to join to.
    row = con.execute(
        "SELECT t.*, e.release_id FROM track t LEFT JOIN enrichment e "
        "  ON e.content_key = t.content_key AND e.status = 'applied' "
        "WHERE t.content_key = ? LIMIT 1", (content_key,)).fetchone()
    if not row:
        return False

    # What goes into the file is what the catalog now says, not the raw
    # proposal. The catalog has already had the fill-only rule and any
    # override applied to it, so taking the proposal instead would write a
    # guessed album into a file the catalog deliberately left alone - and
    # leave the two disagreeing about the same track.
    fields = {f: row[f] for f in ENRICHABLE if row[f] not in (None, "")}
    if not fields:
        return False

    cover = None
    if artwork and row["release_id"]:
        try:
            cover = fetch_cover(row["release_id"])
        except LookupError_:
            cover = None

    try:
        new_key = tags_mod.write(row["path"], fields, cover=cover)
    except tags_mod.TagWriteError:
        return False
    tags_mod.rekey(con, content_key, row["path"], new_key)
    con.commit()
    return True


def cover_art_url(release_id, size=500):
    """Front cover for a release, from the Cover Art Archive.

    This is the answer to "is the artwork the video thumbnail or the real
    cover": embedded artwork today is whatever yt-dlp scraped, which for an
    art track is the real square cover and for an ordinary upload is a video
    frame. Once a recording is identified, the release's own front cover is a
    direct URL, and ``tags.embed_cover`` puts it in the file.
    """
    if not release_id:
        return None
    return f"{CAA_ROOT}/release/{release_id}/front-{size}"


def fetch_cover(release_id, size=500, opener=None):
    """Download front cover bytes. Returns (bytes, mime) or None."""
    url = cover_art_url(release_id, size)
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
            mime = resp.headers.get("Content-Type") or "image/jpeg"
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            return None
        raise LookupError_(str(exc)) from exc
    return data, mime.split(";")[0].strip()
