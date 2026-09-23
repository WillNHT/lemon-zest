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

  4. **Fingerprint**. AcoustID, via Chromaprint's ``fpcalc``. The only source
     that ignores what the file claims and listens to it instead, which is
     the whole of the untagged-download case: a file whose artist is a
     channel name and whose title is a video title gives a text search
     nothing to work with, and gives a fingerprint everything. Needs
     ``fpcalc`` on PATH and a free AcoustID application key, so it is opt-in
     and the ladder above still works without it.

Nothing in this module writes to an audio file. Applying an enrichment to the
catalog is one call; writing it back into the file is a separate, explicit
one in ``tags.py``, because those have very different blast radii.
"""
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .artwork import iso_date

# MusicBrainz asks for one request per second from a client that identifies
# itself, and answers 503 to anything that does not. Both are honoured: the
# limiter is global to the process, and the User-Agent names the project and
# a contact URL as their guidelines require.
MB_ROOT = "https://musicbrainz.org/ws/2"
CAA_ROOT = "https://coverartarchive.org"
USER_AGENT = (f"lemon-zest/{__version__} "
              "( https://github.com/WillNHT/lemon-zest )")
MB_INTERVAL = 1.05          # seconds between requests, with a little slack
# What a refusal costs. MusicBrainz answers 503 both when you have gone too
# fast and when it is simply busy, and it does not distinguish; either way
# the answer is to ask less often for a while. The interval doubles on a
# refusal up to the ceiling and eases back after a run of answers, so a busy
# hour slows the queue down instead of filling it with failures.
MB_MAX_INTERVAL = 8.0
MB_EASE_AFTER = 10          # consecutive answers before easing back off
MB_ATTEMPTS = 4
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
              "track_no", "disc_no", "year", "date", "genre", "isrc")


# ------------------------------------------------------------------ states
#
# The `enrichment.status` column records what happened to a lookup; the
# interface needs the shorter question of what a person should do next.
# Four states answer it, and every status maps onto one:
#
#   raw       nothing decided - no row at all, or a lookup that came back
#             empty. These are the ones a run picks up.
#   awaiting  a candidate is stored and wants a human judgement.
#   enriched  values are in the catalog: applied automatically, accepted by
#             hand, or typed in by hand.
#   skipped   deliberately excluded. Never looked up again, whatever a run
#             is asked to do, until someone changes the state back.
#
# `rejected` folds into `skipped` rather than into `raw` because that is what
# it already did: rejecting a proposal has always meant "do not offer this
# file again", which is a skip that arrived through a judgement.
STATES = ("raw", "awaiting", "enriched", "skipped")

_STATE_OF_STATUS = {
    None: "raw",
    "none": "raw",
    "candidate": "awaiting",
    "applied": "enriched",
    "rejected": "skipped",
    "skipped": "skipped",
}

# Statuses a lookup must never touch. One list, because the SQL in `pending`
# and the guard in `run_tracks` have to agree exactly: a file the list picks
# up but the guard drops is a lookup nobody asked for, and the other way
# round is a skip that quietly stopped meaning anything.
NEVER_LOOK = ("skipped", "rejected")

# The same mapping in SQL, for queries that report a state beside a track.
# Written out rather than mapped in Python afterwards because the library
# list pages and filters in SQL, and cannot filter on a value it computes
# after the LIMIT.
STATE_SQL = (
    "CASE e.status "
    "WHEN 'candidate' THEN 'awaiting' "
    "WHEN 'applied' THEN 'enriched' "
    "WHEN 'rejected' THEN 'skipped' "
    "WHEN 'skipped' THEN 'skipped' "
    "ELSE 'raw' END"
)


def state_of(status):
    """The four-state answer for a stored ``enrichment.status``."""
    return _STATE_OF_STATUS.get(status, "raw")


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

    def __init__(self, contact=None, interval=MB_INTERVAL, opener=None,
                 sleep=None):
        self.base_interval = interval
        self.interval = interval
        self._last = 0.0
        self._good = 0
        self._opener = opener or urllib.request.urlopen
        # Injectable so a test can watch the pacing without living through
        # it. Everything else about the timing is real.
        self._sleep = sleep or time.sleep
        self.user_agent = (f"lemon-zest/{__version__} ( {contact} )"
                           if contact else USER_AGENT)
        # One release answers the same question for every track on it, and
        # an album is a dozen tracks. Asked once per process instead.
        self._genre_cache = {}

    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self.interval:
            self._sleep(self.interval - gap)
        self._last = time.monotonic()

    def _refused(self):
        """Told to slow down: ask less often until it stops happening."""
        self.interval = min(MB_MAX_INTERVAL, max(self.interval, 0.5) * 2)
        self._good = 0

    def _answered(self):
        self._good += 1
        if self._good >= MB_EASE_AFTER and self.interval > self.base_interval:
            self.interval = max(self.base_interval, self.interval / 2)
            self._good = 0

    def _retry_after(self, exc, attempt):
        """How long to wait, asked of the service before it is guessed.

        A Retry-After header is the service saying exactly when it will
        listen again; ignoring it and guessing is how a client turns a
        busy minute into a failed run. The guess is a backoff with jitter,
        so a restart does not put every request back on the same second.
        """
        header = None
        try:
            header = exc.headers.get("Retry-After")
        except Exception:      # noqa: BLE001 - a header is never worth a crash
            header = None
        if header:
            try:
                return max(0.5, min(60.0, float(str(header).strip())))
            except ValueError:
                pass
        return min(30.0, 2.0 * (2 ** attempt)) * (0.75 + random.random() / 2)

    def get(self, path, **params):
        params["fmt"] = "json"
        url = f"{MB_ROOT}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        last = None
        for attempt in range(MB_ATTEMPTS):
            self._wait()
            try:
                with self._opener(req, timeout=HTTP_TIMEOUT) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                self._answered()
                return out
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._answered()
                    return None
                # 503 is MusicBrainz saying "slow down", not "go away".
                if exc.code in (429, 503):
                    self._refused()
                    last = LookupError_(f"MusicBrainz returned {exc.code}")
                    if attempt < MB_ATTEMPTS - 1:
                        self._sleep(self._retry_after(exc, attempt))
                        continue
                raise LookupError_(f"MusicBrainz returned {exc.code}") from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = LookupError_(str(exc))
                if attempt < MB_ATTEMPTS - 1:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                raise last from exc
        if last:
            raise last
        return None

    def by_isrc(self, isrc):
        """Every recording carrying this ISRC. Usually exactly one."""
        data = self.get(f"isrc/{urllib.parse.quote(isrc)}",
                        inc="artist-credits+releases")
        return (data or {}).get("recordings") or []

    def release_genres(self, release_id):
        if release_id in self._genre_cache:
            return self._genre_cache[release_id]
        got = self._release_genres(release_id)
        self._genre_cache[release_id] = got
        return got

    def _release_genres(self, release_id):
        """The genres MusicBrainz records for a release, best first.

        Genre is community-voted rather than editorial, and it hangs off the
        release group more often than the release - an album's genre is a
        property of the work, not of the Japanese CD pressing of it. So both
        are asked for, in one request, and the counts decide.
        """
        data = self.get(f"release/{urllib.parse.quote(release_id)}",
                        inc="genres+release-groups")
        if not data:
            return []
        pools = [data.get("genres") or []]
        group = data.get("release-group") or {}
        pools.append(group.get("genres") or [])
        by_name = {}
        for pool in pools:
            for g in pool:
                name = (g.get("name") or "").strip()
                if not name:
                    continue
                by_name[name] = max(by_name.get(name, 0), g.get("count") or 0)
        return [n for n, _ in sorted(by_name.items(),
                                     key=lambda kv: (-kv[1], kv[0]))]

    def artist_genres(self, artist_id):
        """The genres MusicBrainz records for an artist, best first."""
        if ("artist", artist_id) not in self._genre_cache:
            data = self.get(f"artist/{urllib.parse.quote(artist_id)}",
                            inc="genres")
            got = sorted((g for g in (data or {}).get("genres") or []
                          if (g.get("name") or "").strip()),
                         key=lambda g: (-(g.get("count") or 0), g["name"]))
            self._genre_cache[("artist", artist_id)] = [
                g["name"].strip() for g in got]
        return self._genre_cache[("artist", artist_id)]

    def recording(self, mbid):
        """One recording in full, by id. How a fingerprint result is read."""
        return self.get(f"recording/{urllib.parse.quote(mbid)}",
                        inc="artist-credits+releases+isrcs")

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


# ------------------------------------------------------- AcoustID (rung 4)

ACOUSTID_ROOT = "https://api.acoustid.org/v2/lookup"
# AcoustID's published limit is three requests a second per key.
ACOUSTID_INTERVAL = 0.34
# Chromaprint's own similarity, 0..1. Below this the audio is not the same
# recording and the answer is worth nothing.
FP_MIN = 0.80
# How many of a fingerprint's recordings to look up in full. A popular track
# resolves to a dozen; the ranking only ever picks one, and each costs a
# rate-limited MusicBrainz request.
FP_MAX_RECORDINGS = 3
FPCALC_TIMEOUT = 120.0


def fpcalc_command():
    """How to invoke ``fpcalc`` here, as an argv prefix, or None.

    Located rather than bundled, for the same reason yt-dlp is: it is a
    separate project on its own release schedule, it is LGPL and shipping it
    inside the binary would drag its licence along, and a user who already
    has Chromaprint installed should not carry a second copy. PATH first,
    then beside the frozen executable, so dropping fpcalc.exe next to
    lemon-zest.exe is enough to enable it.

    A list rather than a string, matching ``ytdlp_command``: it is what
    subprocess wants, and it lets a test substitute an interpreter and a
    script for the real binary.
    """
    exe = shutil.which("fpcalc")
    if exe:
        return [exe]
    if getattr(sys, "frozen", False):
        beside = os.path.join(os.path.dirname(sys.executable),
                              "fpcalc.exe" if os.name == "nt" else "fpcalc")
        if os.path.isfile(beside):
            return [beside]
    return None


class FingerprintError(RuntimeError):
    """This one file could not be fingerprinted. Never ends a run."""


def fingerprint(path, command=None):
    """Chromaprint fingerprint for one file: ``(duration, fingerprint)``.

    Runs fpcalc as a subprocess rather than binding the C library, which
    keeps this an optional dependency a user installs with one command and
    means a codec fpcalc chokes on cannot take the process down.
    """
    cmd = command or fpcalc_command()
    if not cmd:
        raise FingerprintError(
            "fpcalc is not installed. Install Chromaprint - on Windows: "
            "winget install AcoustID.Chromaprint")
    if isinstance(cmd, str):
        cmd = [cmd]
    try:
        out = subprocess.run(list(cmd) + ["-json", path], capture_output=True,
                             text=True, timeout=FPCALC_TIMEOUT,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise FingerprintError(str(exc)) from exc
    if out.returncode != 0:
        # The last line of stderr is fpcalc's own complaint; the exit code
        # is all there is to say when it did not make one.
        lines = (out.stderr or "").strip().splitlines()
        raise FingerprintError(lines[-1] if lines
                               else f"fpcalc exited {out.returncode}")
    try:
        data = json.loads(out.stdout)
        return float(data["duration"]), data["fingerprint"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FingerprintError("fpcalc produced no fingerprint") from exc


class AcoustID:
    """Look a Chromaprint fingerprint up against AcoustID.

    Returns MusicBrainz recording ids and lets the MusicBrainz client fetch
    them in full, rather than reading AcoustID's own abbreviated metadata.
    One reason: the release-choosing rules above are the delicate part of
    this module, and they need a whole recording to work on. Having one path
    into them is worth an extra request.
    """

    def __init__(self, key, interval=ACOUSTID_INTERVAL, opener=None):
        self.key = key
        self.interval = interval
        self._last = 0.0
        self._opener = opener or urllib.request.urlopen

    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()

    def lookup(self, duration, fp):
        """``[(score, [recording_id, ...]), ...]``, best score first."""
        if not self.key:
            raise LookupError_("no AcoustID key configured")
        body = urllib.parse.urlencode({
            "client": self.key, "duration": str(int(duration)),
            "fingerprint": fp, "meta": "recordingids",
        }).encode("ascii")
        # POST, not GET: a fingerprint is a few kilobytes of base64 and
        # overflows the URL length limit on a longer track.
        req = urllib.request.Request(
            ACOUSTID_ROOT, data=body,
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded"})
        self._wait()
        try:
            with self._opener(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LookupError_(f"AcoustID returned {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LookupError_(str(exc)) from exc

        if data.get("status") != "ok":
            raise LookupError_(
                (data.get("error") or {}).get("message") or "AcoustID refused")
        out = []
        for result in data.get("results") or []:
            ids = [r["id"] for r in result.get("recordings") or [] if r.get("id")]
            if ids:
                out.append((float(result.get("score") or 0.0), ids))
        out.sort(key=lambda p: p[0], reverse=True)
        return out


# ------------------------------------------------------- reading a recording

_SKIP_TYPES = {"compilation", "live", "remix", "dj-mix", "mixtape/street",
               "soundtrack", "interview", "demo", "audiobook", "spokenword"}


# A release with more tracks than this is a box set or an anthology, not the
# album a single song came from. Used only as a tiebreak, because a long
# genuine album should still beat a mislabelled compilation on the checks
# above it.
BIG_RELEASE = 30

# Names that stand for "no single artist" rather than naming one. Never
# written into album_artist, which is what a path is built from.
_PLACEHOLDER_ARTISTS = {"various artists", "various", "va", "unknown artist",
                        "verschiedene interpreten", "soundtrack"}


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
           "mbid": recording.get("id"), "release_id": None,
           "artist_id": next((c["artist"].get("id")
                              for c in recording.get("artist-credit") or []
                              if isinstance(c, dict) and c.get("artist")),
                             None)}

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
            # The year alone, which is what a player shows, and the full
            # date beside it, which is what "on this day" needs.
            release_fields["year"] = str(date)[:4]
            if iso_date(date):
                release_fields["date"] = iso_date(date)
        credit = _release_credit(release)
        # "Various Artists" is a placeholder standing in for the fact that a
        # compilation has no single artist. Writing it into album_artist
        # would file Alphaville under V, and album_artist is the first thing
        # the path template reads - so it is refused and the recording's own
        # artist is used instead.
        if credit and fold(credit) not in _PLACEHOLDER_ARTISTS:
            release_fields["album_artist"] = credit
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


# A genre lookup is one more rate-limited request per track, so it is only
# spent where it changes anything: on a file whose own tag is empty. A file
# that already says "Shibuya-kei" is not improved by MusicBrainz voting for
# "pop", and the release it came from is asked about once either way.
def lookup_genres(client, release_id, artist_id):
    """Genres for a track: its release's, or else its artist's.

    Most releases carry no genre votes at all - a single, a Japanese
    pressing, anything nobody has tagged - while the artist usually does.
    The artist is the coarser answer and the far more common one, so it is
    asked second, and only when the release had nothing. Never raises.
    """
    for ask, ref in (("release_genres", release_id),
                     ("artist_genres", artist_id)):
        if not ref or not hasattr(client, ask):
            continue
        try:
            names = getattr(client, ask)(ref)
        except Exception:      # noqa: BLE001 - a genre is never worth a failure
            continue
        if names:
            return names
    return []


def _add_genre(client, cand, row):
    """Fill the candidate's genre from MusicBrainz, when the file has none.

    Never raises and never overwrites: a genre is the softest field in the
    catalog, and losing a lookup to it would be absurd.
    """
    try:
        if row["genre"]:
            return
    except (IndexError, KeyError):
        pass
    if cand["release_fields"].get("genre"):
        return
    names = lookup_genres(client, cand.get("release_id"), cand.get("artist_id"))
    if names:
        # Title case, because that is how every other tagger writes them and
        # a library sorted by genre should not hold "rock" and "Rock".
        cand["release_fields"]["genre"] = names[0].title()


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
RELEASE_FIELDS = ("album", "album_artist", "year", "date", "track_no",
                  "disc_no")


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


# -------------------------------------------------------------------- config

CONFIG_DEFAULTS = {
    # MusicBrainz asks that a client identify itself with a way to get in
    # touch. Blank falls back to the project's own repository URL.
    "contact": "",
    # A free AcoustID application key. Without one, rung four is unavailable
    # and the ladder stops at text search.
    "acoustid_key": "",
}
_PREFIX = "enrich."


def get_config(con):
    cfg = dict(CONFIG_DEFAULTS)
    for row in con.execute("SELECT key, value FROM meta WHERE key LIKE ?",
                           (_PREFIX + "%",)):
        key = row["key"][len(_PREFIX):]
        if key in cfg and row["value"] is not None:
            cfg[key] = row["value"]
    return cfg


def set_config(con, **changes):
    for key, value in changes.items():
        if key not in CONFIG_DEFAULTS or value is None:
            continue
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_PREFIX + key, str(value)))
    con.commit()


def fingerprint_status(con):
    """Whether rung four is usable here, and what is missing if not."""
    cfg = get_config(con)
    cmd = fpcalc_command()
    return {
        "fpcalc": " ".join(cmd) if cmd else None,
        "has_key": bool(cfg["acoustid_key"]),
        "ready": bool(cmd and cfg["acoustid_key"]),
        "missing": ([] if cmd else ["fpcalc"])
                   + ([] if cfg["acoustid_key"] else ["acoustid_key"]),
    }


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
    else:
        # `redo` re-asks about decisions, but a skip is not a decision about
        # an answer - it is an instruction not to ask. Enforced here rather
        # than at the call site so that no caller can forget it.
        where.append("(e.status IS NULL OR e.status NOT IN (%s))"
                     % ",".join("'%s'" % st for st in NEVER_LOOK))
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


def _fingerprint_candidate(row, client, acoustid, fpcalc=None):
    """Identify a file by its audio. Returns ``(candidate, fp_score)``.

    The tags play no part in getting here, which is the point: a file whose
    artist is a channel name and whose title is a video title tells a text
    search nothing and tells Chromaprint everything.

    They do decide how much of the answer is applied, though. When the tags
    corroborate what the audio says, the two agreeing is as good as evidence
    gets. When they disagree - and for the files this rung exists for they
    will, because the tags are what is wrong - the answer still wants a
    person, since "trust the sound over the tag" is a judgement call about
    somebody's library, not a fact.
    """
    duration, fp = fingerprint(row["path"], command=fpcalc)
    results = acoustid.lookup(duration, fp)
    if not results or results[0][0] < FP_MIN:
        return None, 0.0
    fp_score, ids = results[0]

    album = row["album"] if "album" in row.keys() else None
    records = []
    for mbid in ids[:FP_MAX_RECORDINGS]:
        rec = client.recording(mbid)
        if rec:
            records.append(rec)
    if not records:
        return None, fp_score

    # Score against the local row as usual, but never let a disagreeing tag
    # veto the match: the fingerprint already established it is this audio.
    best, tag_score = _best_candidate(records, row, album)
    if best is None:
        best = recording_fields(records[0], prefer_album=album)
        tag_score = 0.0
    best["tag_score"] = tag_score
    return best, fp_score


def enrich_track(con, row, client, acoustid=None, fpcalc=None, now=None,
                 query=None):
    """Look one track up and store the best candidate. Returns its status.

    ``'applied'`` means the match was certain enough to write into the
    catalog on the spot; ``'candidate'`` means it wants a human; ``'none'``
    means nothing good enough came back.

    ``query`` overrides what is searched for: ``{"artist", "title",
    "album"}``, any of them. It is what the interface sends when a person
    has looked at a wrong answer and can see why - "Song (Single Version)"
    finds nothing, "Song" finds it. Given one, the automatic rewrites are
    not tried and the ISRC shortcut is skipped: both exist to guess, and a
    typed query is not a guess.
    """
    now = now or time.time()
    album = row["album"] if "album" in row.keys() else None
    if query:
        album = (query.get("album") or "").strip() or album

    if row["isrc"] and not query:
        for rec in client.by_isrc(row["isrc"]):
            cand = recording_fields(rec, prefer_album=album)
            # An ISRC is an exact identifier, so the only check left is that
            # the file is not mis-tagged with someone else's: a wildly
            # different length means the tag is wrong, not the database.
            if cand["duration"] and row["duration"]:
                if abs(cand["duration"] - row["duration"]) > MAX_DURATION_DELTA:
                    continue
            if not cand["release_id"]:
                # The ISRC endpoint answers with the recording and its artist
                # credit, and no releases at all, whatever `inc` asks for. So
                # the top rung - the free one, on 98.7% of the tagged library -
                # was delivering a title and an artist and then nothing: no
                # album, no year, and no release to fetch a cover from. One
                # more request by mbid is what turns it back into a full
                # answer, and it is only spent when the first reply was short.
                full = client.recording(cand["mbid"]) if cand["mbid"] else None
                if full:
                    cand = recording_fields(full, prefer_album=album)
            _add_genre(client, cand, row)
            fields = {**cand["fields"], **cand["release_fields"]}
            _store(con, row["content_key"], "isrc", 1.0, fields,
                   cand["mbid"], cand["release_id"], now, "applied")
            _apply_fields(con, row["content_key"], fields, now)
            con.commit()
            return "applied"

    best, best_score = None, 0.0
    # A typed query is also what the results are judged against. Scoring a
    # hand-picked search against the file's own tags is what makes the box
    # useless in the case it exists for: "Song (Single Version) [Official
    # Video]" scores nothing against the "Song" it was told to look for.
    judge = row
    if query:
        judge = {"title": query.get("title") or row["title"],
                 "artist": query.get("artist") or row["artist"],
                 "album_artist": query.get("artist") or row["album_artist"],
                 "duration": row["duration"]}
    for artist, title in (_query_attempts(query) if query
                          else _search_attempts(row)):
        cand, s = _best_candidate(client.search(artist, title, row["duration"]),
                                  judge, album)
        if s > best_score:
            best, best_score = cand, s
        # A confident hit ends the ladder; the later spellings exist only
        # because the first one found nothing.
        if best_score >= AUTO:
            break

    if (not best or best_score < REVIEW) and acoustid is not None:
        # Rung four. Only reached when the tags were not enough, which is
        # exactly the population it was added for.
        try:
            cand, fp_score = _fingerprint_candidate(row, client, acoustid,
                                                    fpcalc=fpcalc)
        except FingerprintError:
            cand, fp_score = None, 0.0
        if cand is not None:
            _add_genre(client, cand, row)
            fields = {**cand["fields"], **cand["release_fields"]}
            # Audio and tags agreeing is the strongest evidence available;
            # audio alone still wants a person to look.
            status = "applied" if cand["tag_score"] >= AUTO else "candidate"
            _store(con, row["content_key"], "acoustid", round(fp_score, 4),
                   fields, cand["mbid"], cand["release_id"], now, status)
            if status == "applied":
                _apply_fields(con, row["content_key"], fields, now)
            con.commit()
            return status

    if not best or best_score < REVIEW:
        _store(con, row["content_key"], "musicbrainz", best_score or 0.0, {},
               None, None, now, "none")
        con.commit()
        return "none"

    _add_genre(client, best, row)
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


def _query_attempts(query):
    """What to search for when a person typed it. Their spelling, once.

    One attempt, not four: the rewrites exist to recover from tags nobody
    chose, and second-guessing a typed query would make the box that fixes
    a bad match unable to reproduce one.
    """
    title = (query.get("title") or "").strip()
    artist = (query.get("artist") or "").strip()
    if not title:
        return []
    return [(artist or None, title)]


# Enough attempts to cover the spellings that actually occur, few enough
# that a stubborn track cannot cost half a minute of a rate-limited run.
MAX_ATTEMPTS = 4


def _search_attempts(row):
    """Spellings of one track to try, in order, until something matches.

    A search is a string comparison inside somebody else's index, so the
    exact spelling decides whether there is a hit at all. Three rewrites earn
    their extra request:

      * **the primary credit alone.** An art track's `artist` is every
        credited writer - "Joji, Kurtis McKenzie, Linden Jay, Chelsea Lena,
        George Miller, Joshua Bliss Taffel, Kacy Anne Hill" is one field, and
        one folder - and no index has an artist by that name. The full string
        is still tried first, because splitting is not always right: "Simon &
        Garfunkel" is one artist that the credit splitter happily halves, and
        the whole string is what matches it.
      * **without the parenthetical.** A download titled "時間がない (Jikanga
        Nai)" carries a romanisation the database does not; dropping it is
        the difference between no result and the right one.
      * **compatibility-normalised.** Full-width titles - "ＨＥＡＲＴＢＲＯＫＥＮ" -
        are the same characters to a reader and different bytes to a search
        index.

    Only spellings that actually differ are tried, so an ordinary track with
    one artist and an ASCII title still costs exactly one request.
    """
    artist, title = _search_terms(row)
    if not title:
        return []

    names = credits(artist)
    primary = names[0] if names else artist
    # The narrower artist is what the title rewrites are paired with: by the
    # time we are reaching for them the wide string has already failed.
    titles = [title,
              _PARENTHETICAL.sub("", title).strip(),
              _SPACE.sub(" ", unicodedata.normalize("NFKC", title)).strip()]

    out, seen = [], set()
    for pair in ([(artist, title), (primary, title)]
                 + [(primary, t) for t in titles[1:]]):
        cand_artist, cand_title = pair
        key = (fold(cand_artist), fold(cand_title))
        if not cand_title or key in seen:
            continue
        seen.add(key)
        out.append((cand_artist, cand_title))
        if len(out) >= MAX_ATTEMPTS:
            break
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
    """Mark a candidate wrong. It is not offered again, and not re-fetched.

    Reads as ``skipped`` in the interface: the file has been judged and the
    judgement was "not this", which for every later run means the same thing
    as a skip. The distinct status is kept so the reason survives.
    """
    con.execute("UPDATE enrichment SET status='rejected' WHERE content_key=?",
                (content_key,))
    con.commit()
    return True


def set_state(con, content_keys, state):
    """Move files between the four states by hand. Returns how many moved.

    ``skipped``, ``raw`` and ``enriched`` are accepted. ``awaiting`` is what a
    lookup produces, and declaring one by hand would claim a proposal exists
    when none does.

    ``raw`` deletes the row rather than storing a status. A file with no
    enrichment row is exactly what "never been through the queue" means, and
    leaving a husk behind would keep a stale confidence and a stale mbid
    attached to a file whose next lookup starts from nothing.

    ``enriched`` is a person saying "this file is right as it is". It is
    stored with source ``manual`` and no confidence - nothing was matched -
    and it drops any stored proposal, since accepting one is what Accept is
    for.
    """
    if state not in ("skipped", "raw", "enriched"):
        raise ValueError("cannot set state %r by hand" % state)
    keys = [k for k in (content_keys or []) if k]
    if not keys:
        return 0
    now = time.time()
    n = 0
    for key in keys:
        if state == "raw":
            cur = con.execute("DELETE FROM enrichment WHERE content_key = ?",
                              (key,))
            n += cur.rowcount if cur.rowcount > 0 else 0
        elif state == "enriched":
            con.execute(
                "INSERT INTO enrichment(content_key, status, source, "
                "confidence, fields, fetched_at, applied_at) "
                "VALUES (?,'applied','manual',0.0,'{}',?,?) "
                "ON CONFLICT(content_key) DO UPDATE SET status='applied', "
                "source='manual', confidence=0.0, fields='{}', mbid=NULL, "
                "release_id=NULL, applied_at=excluded.applied_at",
                (key, now, now))
            n += 1
        else:
            con.execute(
                "INSERT INTO enrichment(content_key, status, source, "
                "confidence, fields, fetched_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(content_key) DO UPDATE SET status=excluded.status",
                (key, "skipped", "manual", 0.0, "{}", now))
            n += 1
    con.commit()
    return n


def override(con, content_key, **fields):
    """Record a hand-typed value. Outranks every source, now and later.

    Typing does not change the file's state. Correcting a spelling is not
    identifying a recording, so a raw file stays raw - a later lookup fills
    the columns the typing did not cover, and the typed ones still win - and
    an enriched one keeps its source and confidence. Declaring a file
    enriched is its own act: ``set_state(..., "enriched")``.
    """
    now = time.time()
    wrote = False
    for field, value in fields.items():
        if field not in ENRICHABLE:
            continue
        con.execute(
            "INSERT INTO track_override(content_key, field, value, set_at) "
            "VALUES (?,?,?,?) ON CONFLICT(content_key, field) DO UPDATE SET "
            "value=excluded.value, set_at=excluded.set_at",
            (content_key, field, value, now))
        wrote = True
    _apply_fields(con, content_key, {}, now)
    con.commit()
    return wrote


def overrides_for(con, content_key):
    """The hand-typed values on one file, as a plain dict."""
    return {r["field"]: r["value"] for r in con.execute(
        "SELECT field, value FROM track_override WHERE content_key = ?",
        (content_key,))}


def clear_overrides(con, content_key, fields=None):
    """Forget hand-typed values, so the source's own answer shows again."""
    if fields:
        con.executemany(
            "DELETE FROM track_override WHERE content_key = ? AND field = ?",
            [(content_key, f) for f in fields])
    else:
        con.execute("DELETE FROM track_override WHERE content_key = ?",
                    (content_key,))
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


def proposal_for(con, content_key):
    """Everything the interface needs to judge one file, in one call.

    Three tiers side by side, because a person deciding whether to accept a
    proposal is really comparing them: what the catalog currently says, what
    the source proposed, and what has been typed by hand. Kept separate
    rather than merged - a merged view cannot show that the album on screen
    came from a guess while the title came from the file.
    """
    row = con.execute(
        "SELECT * FROM track WHERE content_key = ? ORDER BY id LIMIT 1",
        (content_key,)).fetchone()
    if not row:
        return None
    enr = con.execute("SELECT * FROM enrichment WHERE content_key = ?",
                      (content_key,)).fetchone()
    copies = con.execute(
        "SELECT COUNT(*) FROM track WHERE content_key = ?",
        (content_key,)).fetchone()[0]
    return {
        "content_key": content_key,
        "track_id": row["id"],
        "path": row["path"],
        "rel_path": row["rel_path"],
        "copies": copies,
        "duration": row["duration"],
        "purl": row["purl"],
        "state": state_of(enr["status"] if enr else None),
        "status": enr["status"] if enr else None,
        "source": enr["source"] if enr else None,
        "confidence": enr["confidence"] if enr else None,
        "mbid": enr["mbid"] if enr else None,
        "cover": cover_art_url(enr["release_id"]) if enr else None,
        "current": {f: row[f] for f in ENRICHABLE},
        "proposed": json.loads(enr["fields"] or "{}") if enr else {},
        "overrides": overrides_for(con, content_key),
        # What an automatic lookup would search for. Shown in the enrich
        # dialog as the starting point for editing it: a person correcting
        # a bad match needs to see the terms that produced it first.
        "search": dict(zip(("artist", "title"), _search_terms(row)),
                       album=row["album"]),
        "album": row["album"],
        "genre": row["genre"],
    }


def summary(con):
    """Counts for the status line: how much of the library is identified."""
    one = lambda q, *a: con.execute(q, a).fetchone()[0]
    out = {
        "tracks": one("SELECT COUNT(*) FROM track WHERE size > 0"),
        "with_isrc": one("SELECT COUNT(*) FROM track WHERE isrc IS NOT NULL"),
        "applied": one("SELECT COUNT(*) FROM enrichment WHERE status='applied'"),
        "candidates": one("SELECT COUNT(*) FROM enrichment WHERE status='candidate'"),
        "rejected": one("SELECT COUNT(*) FROM enrichment WHERE status='rejected'"),
        "unmatched": one("SELECT COUNT(*) FROM enrichment WHERE status='none'"),
        "overrides": one("SELECT COUNT(DISTINCT content_key) FROM track_override"),
    }
    out["states"] = state_counts(con)
    return out


def state_counts(con):
    """How many *files* sit in each of the four states.

    Counted over tracks rather than over enrichment rows, because `raw` is
    the absence of a row: counting the table could never report it, and the
    number a person wants is "how many of my files still need looking at".
    """
    counts = {st: 0 for st in STATES}
    rows = con.execute(
        f"SELECT {STATE_SQL} AS state, COUNT(*) AS n FROM track t "
        "LEFT JOIN enrichment e ON e.content_key = t.content_key "
        "WHERE t.size > 0 GROUP BY state")
    for r in rows:
        counts[r["state"]] = r["n"]
    return counts


def run(con, client=None, root=None, limit=None, redo=False, write_tags=False,
        artwork=False, progress=None, contact=None, use_fingerprint=False,
        acoustid=None):
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
    cfg = get_config(con)
    client = client or MusicBrainz(contact=contact or cfg["contact"] or None)
    # Named use_fingerprint rather than fingerprint: the module-level
    # fingerprint() is what eventually does the work, and shadowing it here
    # would be a trap for the next person to add a call.
    if use_fingerprint and acoustid is None:
        acoustid = AcoustID(cfg["acoustid_key"])
    if not use_fingerprint:
        acoustid = None
    counts = _new_counts()
    counts["backfilled"] = backfill_isrc(con)["matched"]

    rows = pending(con, limit=limit, root=root, redo=redo)
    _process(con, rows, client, acoustid, counts, write_tags, artwork,
             progress)
    return counts


# How many distinct failure messages a run keeps. A run over two thousand
# files that loses the network produces two thousand identical complaints;
# what a person needs is the sentence, once, with a count beside it.
MAX_REPORTED_ERRORS = 8


def _new_counts():
    return dict(backfilled=0, applied=0, candidates=0, unmatched=0,
                written=0, write_failed=0, failed=0, skipped=0, stopped=None,
                errors=[], auto=False)


def _note_error(counts, message, track=None):
    """Record why something failed, folding repeats into one line.

    Without this a failure is a number: `failed: 1` and nothing else, which
    is exactly as useful as no message at all. The reason is what tells the
    difference between "MusicBrainz is down", "that file is not where the
    catalog thinks" and "this container cannot hold tags".
    """
    message = str(message).strip() or "unknown error"
    for e in counts["errors"]:
        if e["message"] == message:
            e["count"] += 1
            return
    if len(counts["errors"]) < MAX_REPORTED_ERRORS:
        counts["errors"].append({"message": message, "count": 1,
                                 "track": track})


def _process(con, rows, client, acoustid, counts, write_tags, artwork,
             progress, query=None, with_lyrics=False):
    """The lookup loop. Shared by a whole-library run and a hand-picked one."""
    total = len(rows)
    consecutive = 0
    for i, row in enumerate(rows, 1):
        try:
            status = enrich_track(con, row, client, acoustid=acoustid,
                                  query=query)
        except LookupError_ as exc:
            counts["failed"] += 1
            _note_error(counts, exc, row["rel_path"])
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
            res = write_back_result(con, row["content_key"], artwork=artwork,
                                    with_lyrics=with_lyrics)
            counts["written" if res["ok"] else "write_failed"] += 1
            if res["reason"]:
                _note_error(counts, res["reason"], row["rel_path"])
        if progress:
            progress(i, total, status)
    return counts


def tracks_for_keys(con, content_keys):
    """One track row per content key, in the order the keys were given.

    One row, not all of them: a content key can name several files - the same
    recording downloaded twice - and looking each copy up separately would
    spend a rate-limited request to get the same answer, which is then stored
    once anyway because the enrichment table is keyed by content key too.
    """
    out, seen = [], set()
    for key in content_keys or []:
        if not key or key in seen:
            continue
        seen.add(key)
        row = con.execute(
            "SELECT * FROM track WHERE content_key = ? AND size > 0 LIMIT 1",
            (key,)).fetchone()
        if row:
            out.append(row)
    return out


# The one hatch out of automatic enrichment, and it is not a user setting:
# it exists so a test suite and a CI box do not make rate-limited calls to
# somebody else's service. Anything a person would reach for lives in the
# interface, not in an environment variable.
AUTO_ENV = "LEMONZEST_AUTO_ENRICH"


def auto_enabled():
    return os.environ.get(AUTO_ENV, "1") != "0"


def auto_after_ingest(con, root=None, content_keys=None, progress=None,
                      contact=None, client=None, acoustid=None):
    """Identify what just arrived, and tag it, without asking anybody.

    Run after a scan and after a download - the two moments the library
    actually changes - so a file is identified as it lands rather than when
    somebody remembers to go and look.

    What it will do on its own:

      * the offline ISRC backfill, which is free;
      * a lookup for everything still ``raw`` in what just arrived;
      * fingerprinting, when ``fpcalc`` and a key are present, because a
        download whose artist is a channel name gives a text search nothing;
      * **writing the tags into the audio files**, cover included.

    What it still will not do, because automation does not get to widen these:

      * touch a **skipped** file, ever;
      * write a match that is not certain. Only an ``applied`` enrichment is
        written to disk - an exact ISRC, or a text match at or above ``AUTO``,
        or a fingerprint the tags agree with. Anything doubtful stays a
        candidate in the review queue and no file is rewritten for it;
      * overwrite a release-derived field that the file already filled in, or
        anything typed by hand.

    Never raises: this runs behind somebody else's scan, and a service being
    down is not a reason for the scan to look like it failed.
    """
    counts = _new_counts()
    counts["auto"] = True
    if not auto_enabled():
        counts["stopped"] = "automatic enrichment is off in this environment"
        return counts
    try:
        cfg = get_config(con)
        client = client or MusicBrainz(contact=contact or cfg["contact"] or None)
        if acoustid is None and fingerprint_status(con)["ready"]:
            # Taken when it is available rather than asked about: it is the
            # rung that answers the download case, and the whole point here
            # is that nobody is being asked.
            acoustid = AcoustID(cfg["acoustid_key"])

        counts["backfilled"] = backfill_isrc(con)["matched"]

        if content_keys is not None:
            rows = tracks_for_keys(con, content_keys)
            rows = _drop_skipped(con, rows, counts)
        else:
            # `pending` has already left the skipped ones out. Counted anyway,
            # so an automatic pass can say what it deliberately did not touch
            # rather than looking as though it missed them.
            rows = pending(con, root=root)
            counts["skipped"] = _count_skipped(con, root)

        _process(con, rows, client, acoustid, counts,
                 True,   # write_tags: the point of the mode
                 True,   # artwork: a download's cover is a video frame
                 progress, with_lyrics=True)
    except Exception as exc:      # noqa: BLE001 - reported, never propagated
        counts["stopped"] = "%s: %s" % (type(exc).__name__, exc)
        _note_error(counts, exc)
    return counts


class IngestStream:
    """Automatic enrichment of arrivals, one file at a time.

    ``auto_after_ingest`` is the batch form of this: hand it everything that
    landed and it works through the list. A download hands its files over as
    yt-dlp finishes each one, and calling the batch form per file would be
    wrong twice over. A fresh :class:`MusicBrainz` per call has no memory of
    when the last request went out - that memory is the whole of how the
    one-request-a-second rule is kept - and ``backfill_isrc`` walks the
    entire catalog, which is a once-per-run cost and not a once-per-file
    one. So the client is built once and kept, the backfill runs once on the
    first arrival, and the counts accumulate across the run.

    The same promises as the batch form: a skipped file is never touched,
    only an ``applied`` match is written into the audio, and nothing raises -
    this runs behind somebody else's download, and MusicBrainz being down is
    not a reason for the download to look like it failed.
    """

    def __init__(self, con, contact=None, client=None, acoustid=None):
        self.con = con
        self.counts = _new_counts()
        self.counts["auto"] = True
        self.enabled = auto_enabled()
        if not self.enabled:
            self.counts["stopped"] = ("automatic enrichment is off in this "
                                      "environment")
        self._contact = contact
        self._client = client
        self._acoustid = acoustid
        self._started = False

    def _start(self):
        """Build the client and pay the once-per-run costs, on first use.

        Deferred rather than done in ``__init__`` so a run where nothing
        arrives - every URL already in the archive - costs nothing at all.
        """
        cfg = get_config(self.con)
        if self._client is None:
            self._client = MusicBrainz(
                contact=self._contact or cfg["contact"] or None)
        if self._acoustid is None and fingerprint_status(self.con)["ready"]:
            # Taken when available rather than asked about, as in the batch
            # form: a download whose artist is a channel name gives a text
            # search nothing to work with.
            self._acoustid = AcoustID(cfg["acoustid_key"])
        self.counts["backfilled"] = backfill_isrc(self.con)["matched"]
        self._started = True

    def client(self):
        """The one MusicBrainz client, built if it has not been yet.

        Lent out so that a hand-picked run - which wants options this does
        not offer - still goes out through the same client, and therefore
        the same one request a second, as everything else.
        """
        if not self._started:
            self._start()
        return self._client

    def add(self, content_key):
        """Identify one arrival. Returns what became of it.

        The result is ``{"state": ..., "detail": ...}``, meant to be shown
        beside the file in a progress view: ``state`` is one of ``applied``,
        ``candidate``, ``none``, ``skipped``, ``failed`` or ``off``, and
        ``detail`` is the sentence a person reads.
        """
        if not self.enabled:
            return {"state": "off", "detail": "identification is off"}
        try:
            if not self._started:
                self._start()
            rows = tracks_for_keys(self.con, [content_key])
            if not rows:
                return {"state": "failed", "detail": "not in the catalog"}
            before = dict(self.counts)
            # The error list is shared and folds repeats into a count, so a
            # copy of it is the only way to tell afterwards which complaint
            # this file made.
            before["errors"] = [dict(e) for e in self.counts["errors"]]
            rows = _drop_skipped(self.con, rows, self.counts)
            if not rows:
                return {"state": "skipped", "detail": "left alone on purpose"}
            _process(self.con, rows, self._client, self._acoustid,
                     self.counts,
                     True,   # write_tags: the point of the mode
                     True,   # artwork: a download's cover is a video frame
                     None, with_lyrics=True)
            return self._outcome(content_key, before)
        except Exception as exc:      # noqa: BLE001 - reported, never raised
            self.counts["failed"] += 1
            _note_error(self.counts, exc)
            return {"state": "failed", "detail": str(exc)}

    def _outcome(self, content_key, before):
        """What ``_process`` did to this one file, as a state and a sentence.

        Read off the counts rather than returned by ``_process``, which
        reports a list. One row went in, so exactly one counter moved.
        """
        for counter, state in (("applied", "applied"),
                               ("candidates", "candidate"),
                               ("unmatched", "none"),
                               ("failed", "failed")):
            if self.counts[counter] > before[counter]:
                return {"state": state,
                        "detail": self._detail(content_key, state, before)}
        return {"state": "none", "detail": "nothing to change"}

    def _latest_error(self, before):
        """The complaint this file made, out of the run's folded list."""
        was = {e["message"]: e["count"] for e in before["errors"]}
        for e in self.counts["errors"]:
            if e["count"] > was.get(e["message"], 0):
                return e["message"]
        return "lookup failed"

    def _detail(self, content_key, state, before):
        if state == "failed":
            return self._latest_error(before)
        if state == "none":
            return "no match found"
        row = self.con.execute(
            "SELECT fields, source, confidence FROM enrichment "
            "WHERE content_key = ?", (content_key,)).fetchone()
        if not row:
            return "identified" if state == "applied" else "needs review"
        try:
            fields = json.loads(row["fields"])
        except (TypeError, ValueError):
            fields = {}
        name = " - ".join(x for x in (fields.get("artist"),
                                      fields.get("title")) if x)
        verb = "identified" if state == "applied" else "possible match"
        return "%s: %s" % (verb, name) if name else verb


def _count_skipped(con, root=None):
    """How many files a sweep is leaving alone on purpose."""
    placeholders = ",".join("?" * len(NEVER_LOOK))
    sql = ("SELECT COUNT(DISTINCT t.content_key) FROM track t "
           "JOIN enrichment e ON e.content_key = t.content_key "
           f"WHERE e.status IN ({placeholders})")
    params = list(NEVER_LOOK)
    if root:
        sql += " AND t.root = ?"
        params.append(root)
    return con.execute(sql, params).fetchone()[0]


def _drop_skipped(con, rows, counts):
    """Remove the files nobody is allowed to look at, and count them."""
    if not rows:
        return []
    placeholders = ",".join("?" * len(NEVER_LOOK))
    blocked = {r["content_key"] for r in con.execute(
        f"SELECT content_key FROM enrichment WHERE status IN ({placeholders})",
        NEVER_LOOK)}
    wanted = [r for r in rows if r["content_key"] not in blocked]
    counts["skipped"] += len(rows) - len(wanted)
    return wanted


def fill_genres(con, client, progress=None, write_tags=True):
    """Give identified tracks with no genre the one MusicBrainz has.

    For files identified before genres came from the artist as well as the
    release, and before a YouTube category stopped counting as one. Asks
    only about identified tracks - the others have no recording to ask
    about - and fills a blank, never replaces a genre. Returns counts.
    """
    rows = con.execute(
        "SELECT t.content_key, e.mbid, e.release_id FROM track t "
        "JOIN enrichment e ON e.content_key = t.content_key "
        "WHERE e.status = 'applied' AND e.mbid IS NOT NULL "
        "AND (t.genre IS NULL OR t.genre = '') AND t.size > 0 "
        "GROUP BY t.content_key").fetchall()
    counts = {"considered": len(rows), "filled": 0, "none": 0, "failed": 0,
              "written": 0, "errors": []}
    consecutive = 0
    for i, r in enumerate(rows, 1):
        try:
            artist_id = None
            names = lookup_genres(client, r["release_id"], None)
            if not names:
                rec = client.recording(r["mbid"]) or {}
                artist_id = recording_fields(rec).get("artist_id")
                names = lookup_genres(client, None, artist_id)
            consecutive = 0
        except LookupError_ as exc:
            counts["failed"] += 1
            _note_error(counts, exc)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        if not names:
            counts["none"] += 1
        else:
            _apply_fields(con, r["content_key"], {"genre": names[0].title()},
                          time.time())
            con.commit()
            counts["filled"] += 1
            if write_tags and write_back_result(con, r["content_key"])["ok"]:
                counts["written"] += 1
        if progress:
            progress(i, len(rows))
    return counts


def run_tracks(con, content_keys, client=None, write_tags=False, artwork=False,
               progress=None, contact=None, use_fingerprint=False,
               acoustid=None, query=None):
    """Enrich a hand-picked set of files. Asking again, by hand.

    New files are identified automatically as they arrive - see
    ``auto_after_ingest``. This is the path for asking a second time about
    files that have already been through it.

    Differs from ``run`` in three ways, all of them because somebody chose
    these files rather than asking for a sweep:

      * the offline ISRC backfill does not run - it is a library-wide pass,
        and quietly rewriting files nobody selected is not what a button
        marked with three track names should do;
      * a file that already has an answer is looked up again, because asking
        for it again is the only reason to select it;
      * a **skipped** file is dropped rather than looked up, and counted, so
        the interface can say so. That is the whole promise of the state, and
        an explicit selection is exactly where it would otherwise be lost.

    ``query`` replaces what is searched for, for every file in the set. It
    is meant for one track at a time - the case it exists for is a person
    looking at "Song (Single Version)" and knowing what to search instead.
    """
    cfg = get_config(con)
    client = client or MusicBrainz(contact=contact or cfg["contact"] or None)
    if use_fingerprint and acoustid is None:
        acoustid = AcoustID(cfg["acoustid_key"])
    if not use_fingerprint:
        acoustid = None

    counts = _new_counts()
    wanted = _drop_skipped(con, tracks_for_keys(con, content_keys), counts)
    _process(con, wanted, client, acoustid, counts, write_tags, artwork,
             progress, query=query)
    return counts


def pending_write(con, content_key):
    """What a write would put into this file, and what it would replace.

    The preview behind the confirmation dialog. Writing tags is the one
    action here that cannot be taken back, so what is about to happen should
    be readable before it happens rather than described afterwards.
    """
    row = con.execute(
        "SELECT t.*, e.release_id, e.status AS enrich_status FROM track t "
        "LEFT JOIN enrichment e ON e.content_key = t.content_key "
        "WHERE t.content_key = ? ORDER BY t.id LIMIT 1",
        (content_key,)).fetchone()
    if not row:
        return None

    fields = {f: row[f] for f in ENRICHABLE if row[f] not in (None, "")}
    on_disk = {}
    real = None
    from .paths import resolve_existing
    from .meta import read_tags
    real = resolve_existing(row["path"])
    if real:
        # Read the file rather than trusting the catalog's copy of it: the
        # whole question a preview answers is whether the two still agree.
        on_disk = read_tags(real) or {}
    changes = {f: {"from": on_disk.get(f), "to": v} for f, v in fields.items()
               if str(on_disk.get(f) or "") != str(v or "")}
    return {
        "content_key": content_key,
        "rel_path": row["rel_path"],
        "missing": real is None,
        "renormalised": bool(real and real != row["path"]),
        "state": state_of(row["enrich_status"]),
        "fields": fields,
        "changes": changes,
        "artwork_available": bool(row["release_id"]),
    }


def write_back_result(con, content_key, artwork=False, with_lyrics=False):
    """Push an applied enrichment into the audio file. Returns a report.

    ``{"ok": bool, "reason": str|None, "wrote": bool}``. A failure is
    reported rather than raised - one file with an exotic container must not
    end a run over two thousand of them - but the reason travels with it, so
    the interface can say *why* instead of showing a silent zero.
    """
    from . import tags as tags_mod   # imported late: enrichment works without it

    def out(ok, reason=None, wrote=False):
        return {"ok": ok, "reason": reason, "wrote": wrote,
                "content_key": content_key}

    # Driven from track, not from enrichment: a hand-typed correction on a
    # file no source could identify is exactly the case that most needs
    # writing out, and it has no enrichment row to join to.
    row = con.execute(
        "SELECT t.*, e.release_id FROM track t LEFT JOIN enrichment e "
        "  ON e.content_key = t.content_key AND e.status = 'applied' "
        "WHERE t.content_key = ? LIMIT 1", (content_key,)).fetchone()
    if not row:
        return out(False, "no track in the catalog with that content key")

    # What goes into the file is what the catalog now says, not the raw
    # proposal. The catalog has already had the fill-only rule and any
    # override applied to it, so taking the proposal instead would write a
    # guessed album into a file the catalog deliberately left alone - and
    # leave the two disagreeing about the same track.
    fields = {f: row[f] for f in ENRICHABLE if row[f] not in (None, "")}
    if not fields:
        return out(False, "the catalog has no values for this file to write - "
                          "identify it or type something in first")

    from .paths import resolve_existing
    if with_lyrics and resolve_existing(row["path"]) \
            and not tags_mod.has_lyrics(row["path"]):
        # Asked of LRCLIB with what the catalog now says, which is only
        # worth asking once the track has been identified - and this is
        # only reached then. Lyrics are a nicety: no answer, or no service,
        # writes the rest of the tags regardless.
        from . import lyrics as lyrics_mod
        try:
            text = lyrics_mod.fetch(row["artist"], row["title"], row["album"],
                                    row["duration"])
        except LookupError_:
            text = None
        if text:
            fields["lyrics"] = text

    cover = None
    cover_note = None
    if artwork:
        if not row["release_id"]:
            # Said out loud rather than passed over: the cover box was
            # ticked, and silently writing no cover is how a user concludes
            # the feature is broken.
            cover_note = ("no cover was fetched: this file is not matched to "
                          "a release yet, so there is none to fetch")
        else:
            try:
                cover = fetch_cover(row["release_id"])
            except LookupError_ as exc:
                cover_note = "cover could not be fetched: %s" % exc

    try:
        new_key = tags_mod.write(row["path"], fields, cover=cover)
    except tags_mod.TagWriteError as exc:
        return out(False, str(exc))
    if new_key is None:
        # write() declines when there is nothing to put in the file. Counting
        # that as a success is how "1 written" ends up describing a file
        # nobody touched.
        return out(False, "nothing to write into this file")
    tags_mod.rekey(con, content_key, row["path"], new_key)
    con.commit()
    return out(True, cover_note, wrote=True)


def write_back(con, content_key, artwork=False):
    """Push an applied enrichment into the audio file. True when it landed."""
    return write_back_result(con, content_key, artwork=artwork)["ok"]


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
