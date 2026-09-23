"""Fetch audio from YouTube (and anything else yt-dlp handles).

yt-dlp is run as a subprocess rather than imported. Three reasons, all of
them practical: it is the interface yt-dlp actually promises to keep stable,
it lets a user upgrade the downloader without touching Lemon Zest, and a
long download that wedges cannot take the catalog's process down with it.

The download lands inside a library root, so the file that arrives is an
ordinary library file: it is indexed by the same probe the scanner uses, and
from there the planner, the device set and the playlists treat it like every
other track. Nothing downstream knows where it came from - except that
``--embed-metadata`` writes the source URL into the ``purl`` tag, which
``meta.py`` already reads, so provenance survives the round trip.

Cookies
-------
YouTube refuses a growing share of requests from a signed-out client: age
gates, the "sign in to confirm" bot check, and members-only material.
yt-dlp can read the cookies straight out of a local Firefox profile, which
is the path that needs no exporting and no file to keep in sync. When there
is no Firefox profile to read - a different browser, a server, a locked-down
machine - a cookies.txt exported by hand is accepted instead. Both are
optional: plenty of videos need no cookies at all, so the default mode tries
Firefox, falls back to the file, and then proceeds without cookies rather
than refusing to start.
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from . import artwork
from . import db
from . import playlists as pl_mod
from . import scan as scan_mod
from .paths import norm

# Markers on stdout. yt-dlp's own progress output is written for humans;
# these two templates are parsed, so they carry a prefix that cannot
# plausibly appear in a video title.
PROGRESS_PREFIX = "[lz-progress]"
FILE_PREFIX = "[lz-file]"
# Printed just before the file line: the release date, which the year tag
# can no longer carry (see tags.py), so it is written into its own tag here.
DATE_PREFIX = "[lz-date]"

# What each progress line carries. Bytes describe the file being fetched;
# the playlist position describes where in the batch it sits, which is the
# thing a person watching a 40-track download actually wants to know and
# the thing the old three-field line could not say.
PROGRESS_TEMPLATE = (
    PROGRESS_PREFIX
    + "%(progress.downloaded_bytes)s|"
    + "%(progress.total_bytes,progress.total_bytes_estimate)s|"
    + "%(info.playlist_index)s|"
    + "%(info.n_entries)s|"
    + "%(progress.speed)s|"
    + "%(progress.eta)s|"
    + "%(info.title)s"
)

# Where a download lands under the library root, in yt-dlp's own output
# syntax: alternates are comma-separated, the fallback follows a pipe.
#
# album_artist comes first because it is the one name a person would look
# under. YouTube's auto-generated art tracks put every credited writer in
# `artist` - which is how the library grew a folder named for seven people -
# and the single performer in `album_artist`. `uploader` stays as the last
# resort, and it is the reason a KIRINJI track can land under the channel
# that posted it; the enricher exists to correct that afterwards, but
# preferring the better field first means it happens less often.
DEFAULT_OUTPUT = (
    "%(album_artist,artist,uploader|Unknown Artist)s/"
    "%(album,playlist_title|Singles)s/%(title)s.%(ext)s"
)

# Kept out of the library walk by the scanner's dotfile rule.
ARCHIVE_NAME = ".lemon-zest-downloads.txt"
# Part files live here rather than beside the finished audio, so a scan that
# runs mid-download never indexes a half-written file.
INCOMPLETE_DIR = ".lz-incomplete"

# YouTube signs its media URLs with a challenge that has to be executed, so
# yt-dlp needs a JavaScript runtime to get a playable format at all. It
# enables only Deno by default and treats the rest as opt-in, which means a
# machine with Node but no Deno fails every video with "The page needs to be
# reloaded". Lemon Zest passes --ignore-config, so the user cannot fix that
# in their own yt-dlp.conf; enabling the others here is the only place it
# can be done. Deno keeps its priority when it is installed, so this changes
# nothing on a machine that already worked.
EXTRA_JS_RUNTIMES = "node,bun,quickjs"
# Highest priority first, which is the order yt-dlp itself picks in.
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")

CONFIG_DEFAULTS = {
    "cookies_mode": "auto",     # auto | firefox | file | none
    "cookies_file": "",
    "firefox_profile": "",      # blank: the most recently used profile
    "root": "",                 # library folder downloads land in
    "output": DEFAULT_OUTPUT,
    "audio_format": "m4a",
    "audio_quality": "0",       # 0 is yt-dlp's best
    "js_runtimes": EXTRA_JS_RUNTIMES,   # blank leaves yt-dlp's deno-only default
}
_PREFIX = "download."


# How much of a run is kept for the log. A playlist of a few hundred videos
# is chatty; this is enough to see the whole of a normal run and the end of
# a long one, and it is held in memory, not on disk.
MAX_LOG = 500


class DownloadError(RuntimeError):
    """yt-dlp failed. The message is written for the person who ran it.

    ``log`` carries the run that produced the failure, so what went wrong
    can be read - and copied out - rather than guessed at from one line.
    """

    def __init__(self, message, log=None):
        super().__init__(message)
        self.log = log or []


# ------------------------------------------------------------------ config

def get_config(con):
    """Read the stored download settings, filled in with the defaults."""
    cfg = dict(CONFIG_DEFAULTS)
    for row in con.execute("SELECT key, value FROM meta WHERE key LIKE ?",
                           (_PREFIX + "%",)):
        key = row["key"][len(_PREFIX):]
        if key in cfg and row["value"] is not None:
            cfg[key] = row["value"]
    return cfg


def set_config(con, **changes):
    """Store settings. Unknown keys and None values are ignored."""
    for key, value in changes.items():
        if key not in CONFIG_DEFAULTS or value is None:
            continue
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_PREFIX + key, str(value)),
        )
    con.commit()
    return get_config(con)


# ------------------------------------------------------------------ yt-dlp

def ytdlp_command():
    """How to invoke yt-dlp here, or None when it is not installed.

    A yt-dlp on PATH wins, so one the user keeps updated is used in
    preference to ours. Then the copy inside the packaged executable, which
    is reached by re-running the binary with ``--yt-dlp`` - a frozen build
    has no ``-m`` to call. Then the module in this interpreter, which is
    what ``pip install lemon-zest[youtube]`` leaves behind.
    """
    from . import bundled

    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if bundled.frozen():
        try:
            import yt_dlp  # noqa: F401
        except Exception:
            return None
        return [sys.executable, "--yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        return None
    return [sys.executable, "-m", "yt_dlp"]


# Asking yt-dlp its version costs a process start, an import of yt-dlp, and
# on a packaged build a second copy of this executable: four seconds,
# measured. The download page asks for it on every visit, so opening that
# page used to take four seconds. It is cached for the life of the process
# and warmed in the background at startup, because the answer changes when
# somebody installs a new yt-dlp - not while they are clicking.
_VERSION = {"cmd": None, "value": None, "at": 0.0}
# Held while the answer is being fetched, so a page opened during the
# warm-up waits for that one subprocess instead of starting a second.
_VERSION_LOCK = threading.Lock()
VERSION_TTL = 900.0


def ytdlp_version(refresh=False):
    cmd = ytdlp_command()
    if not cmd:
        return None

    def cached():
        return (_VERSION["cmd"] == cmd
                and time.time() - _VERSION["at"] < VERSION_TTL)

    if cached() and not refresh:
        return _VERSION["value"]
    with _VERSION_LOCK:
        # Somebody else may have answered it while we waited for the lock.
        if cached() and not refresh:
            return _VERSION["value"]
        return _ytdlp_version_uncached(cmd)


def _ytdlp_version_uncached(cmd):
    try:
        out = subprocess.run(cmd + ["--version"], capture_output=True,
                             text=True, timeout=30, encoding="utf-8",
                             errors="replace")
        value = out.stdout.strip() or None
    except Exception:
        value = None
    _VERSION.update(cmd=cmd, value=value, at=time.time())
    return value


def warm_cache():
    """Ask the slow questions once, off the request path.

    Started at boot by the server. Never raises: this is a cache being
    filled, and a failure only means the first visitor pays what every
    visitor used to.
    """
    try:
        ytdlp_version(refresh=True)
    except Exception:      # noqa: BLE001
        pass


_OPTION_SUPPORT = {}


def supports_option(flag):
    """Whether the installed yt-dlp knows an option.

    Asked once per option per interpreter, off ``--help``, because passing
    an option an older yt-dlp does not know is not a degraded download but
    an immediate usage error - and this project would rather work with the
    yt-dlp that is installed than pin a version.
    """
    cmd = ytdlp_command()
    key = (tuple(cmd or ()), flag)
    if key in _OPTION_SUPPORT:
        return _OPTION_SUPPORT[key]
    ok = False
    if cmd:
        try:
            res = subprocess.run(cmd + ["--help"], capture_output=True,
                                 text=True, timeout=60, encoding="utf-8",
                                 errors="replace")
            ok = flag in (res.stdout or "")
        except Exception:
            ok = False
    _OPTION_SUPPORT[key] = ok
    return ok


def _same_file(a, b):
    """Whether two paths name the same file on disk.

    String equality is not enough on Windows: ``shutil.which`` returns the
    extension in the case PATHEXT carries it - ``deno.EXE`` - while the
    bundle spells it ``deno.exe``, and a page comparing the two decided a
    bundled runtime was somebody else's. ``samefile`` where it works,
    normcase where it does not.
    """
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))


def js_runtime_status(cfg=None):
    """Which JavaScript runtime a download would use, and what is installed.

    Reported next to the cookie source, for the same reason: without one,
    every YouTube video fails, and the failure says nothing about why.
    """
    from . import bundled

    enabled = {"deno"}   # yt-dlp's own default, whatever Lemon Zest passes
    for name in ((cfg or {}).get("js_runtimes") or "").replace(" ", ",").split(","):
        if name.strip():
            enabled.add(name.strip().lower())
    found = []
    for n in JS_RUNTIMES:
        path = shutil.which(n)
        if path:
            found.append({"name": n, "path": norm(path),
                          "bundled": _same_file(path, bundled.tool(n))})
    usable = [f for f in found if f["name"] in enabled]
    chosen = usable[0] if usable else None
    if chosen:
        detail = "solving YouTube's JS challenges with " + chosen["name"]
    elif found:
        detail = ("%s is installed but not enabled here; enable it under "
                  "JavaScript runtimes." % found[0]["name"])
    else:
        detail = ("no JavaScript runtime found. YouTube needs one to hand "
                  "over a playable format - install Deno, or Node.")
    return {"found": found, "chosen": chosen, "detail": detail,
            "enabled": sorted(enabled)}


def _child_env():
    env = dict(os.environ)
    # yt-dlp prints video titles; a cp1252 stdout on Windows would die on
    # the first non-Latin one.
    env["PYTHONIOENCODING"] = "utf-8"
    # Python block-buffers stdout when it is a pipe rather than a terminal,
    # so yt-dlp's output arrives in 8 KB instalments - which on a real
    # download meant the progress bar sat still and the log stayed empty
    # for minutes at a time while files were plainly landing on disk. A
    # stub that exits immediately never shows this, because exiting
    # flushes.
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ----------------------------------------------------------------- cookies

def _firefox_dirs():
    """Every place a Firefox profile lives on this platform."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.join(
            home, "AppData", "Roaming")
        return [os.path.join(appdata, "Mozilla", "Firefox")]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "Firefox")]
    return [os.path.join(home, ".mozilla", "firefox"),
            os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
            os.path.join(home, ".var", "app", "org.mozilla.firefox", ".mozilla",
                         "firefox")]


def firefox_profiles():
    """Firefox profiles that actually hold a cookie database.

    Read off the filesystem rather than out of profiles.ini: a profile with
    no cookies.sqlite is of no use here whatever the ini says, and a profile
    that exists but is unlisted still works. Most recently written first,
    because that is the profile the user is signed in to.
    """
    found = []
    seen = set()
    for base in _firefox_dirs():
        for parent in (os.path.join(base, "Profiles"), base):
            try:
                names = sorted(os.listdir(parent))
            except OSError:
                continue
            for name in names:
                path = os.path.join(parent, name)
                cookies = os.path.join(path, "cookies.sqlite")
                if not os.path.isfile(cookies):
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mtime = os.path.getmtime(cookies)
                except OSError:
                    mtime = 0
                found.append({"name": name, "path": norm(path),
                              "cookies": norm(cookies), "modified": mtime})
    found.sort(key=lambda p: -p["modified"])
    return found


def cookie_status(cfg):
    """What cookies this configuration would use, and why.

    Reported to the interface so that "will this work signed in?" can be
    answered before a download rather than after one fails.
    """
    mode = (cfg.get("cookies_mode") or "auto").strip()
    profiles = firefox_profiles()
    cookies_file = (cfg.get("cookies_file") or "").strip()
    file_ok = bool(cookies_file) and os.path.isfile(cookies_file)
    status = {"mode": mode, "profiles": profiles, "cookies_file": cookies_file,
              "file_exists": file_ok, "source": "none", "profile": None,
              "detail": ""}

    if mode == "none":
        status["detail"] = ("cookies are off. Age-restricted, members-only "
                            "and bot-checked videos will be refused.")
        return status
    if mode in ("auto", "firefox") and profiles:
        wanted = (cfg.get("firefox_profile") or "").strip()
        match = next((p for p in profiles
                      if p["name"] == wanted or norm(p["path"]) == norm(wanted)),
                     None) if wanted else None
        status["source"] = "firefox"
        status["profile"] = match or profiles[0]
        status["detail"] = ("reading cookies from the Firefox profile "
                            + status["profile"]["name"])
        return status
    if mode == "firefox" and not profiles:
        status["detail"] = ("no Firefox profile with a cookie database was "
                            "found. Add a cookies.txt instead.")
        return status
    if file_ok:
        status["source"] = "file"
        status["detail"] = "reading cookies from " + cookies_file
        return status
    if cookies_file:
        status["detail"] = ("the cookies file is set but not on disk: "
                            + cookies_file)
        return status
    status["detail"] = ("no cookies available - public videos still download. "
                        "Sign in to Firefox, or export a cookies.txt.")
    return status


def cookie_args(cfg):
    """The yt-dlp arguments for the cookie source this config resolves to."""
    status = cookie_status(cfg)
    if status["source"] == "firefox":
        # "firefox:<profile path>" is yt-dlp's own spelling. Naming the
        # profile removes the ambiguity when several exist.
        return ["--cookies-from-browser", "firefox:" + status["profile"]["path"]]
    if status["source"] == "file":
        return ["--cookies", status["cookies_file"]]
    return []


# -------------------------------------------------------------- the command

def build_args(cfg, urls, root, no_playlist=False, archive=True, output=None,
               audio_format=None, audio_quality=None):
    """Assemble the yt-dlp command line for a download."""
    output = output or cfg.get("output") or DEFAULT_OUTPUT
    fmt = (audio_format or cfg.get("audio_format") or "m4a").lstrip(".")
    quality = str(audio_quality or cfg.get("audio_quality") or "0")

    base = ytdlp_command()
    if not base:
        raise DownloadError(
            "yt-dlp is not installed. Install it with: pip install yt-dlp")
    args = list(base) + [
        # The user's own yt-dlp.conf must not redirect files out of the
        # library or turn the parsed output templates off.
        "--ignore-config",
        "-f", "bestaudio/best",
        "-x", "--audio-format", fmt, "--audio-quality", quality,
        "--embed-metadata", "--embed-thumbnail",
        # YouTube serves thumbnails as WebP. Several players - and some tag
        # readers - show nothing at all for a WebP cover, so the picture is
        # there and invisible. Converting on the way in costs one ffmpeg call
        # and makes the embedded art readable everywhere.
        #
        # What this cannot fix is *which* picture it is: for an art track the
        # thumbnail is the real square cover, and for an ordinary upload it
        # is a 16:9 video frame. Replacing that with the release's own front
        # cover needs the recording identified first - see enrich.py.
        "--convert-thumbnails", "jpg",
        # ...and while converting, crop to the centre square and write a
        # baseline 4:2:0 JPEG: Rockbox shows a 16:9 frame letterboxed and
        # draws any other JPEG in greyscale. See artwork.py.
        "--postprocessor-args", artwork.YTDLP_PPA,
        # yt-dlp writes upload_date (20180201) into the date tag, which
        # Rockbox shows as the year verbatim. Keep only the year.
        "--parse-metadata", "%(release_year,upload_date>%Y)s:%(meta_date)s",
        # yt-dlp fills the genre tag from YouTube's category - "Music",
        # "People & Blogs" - which is not a genre. Left empty, the real one
        # is looked up once the track is identified.
        "--parse-metadata", ":(?P<meta_genre>)",
        # Long titles become path components; keep them inside what the
        # card's filesystem will accept later.
        "--trim-filenames", "120",
        "--no-overwrites",
        "--newline",
        "--no-abort-on-error",   # one dead video must not kill a playlist
        # The template stays relative and the folder is given with -P:
        # yt-dlp ignores every --paths when the output template is itself
        # absolute, which silently put the part files next to the finished
        # audio - where a scan running mid-download would index them.
        "-o", output,
        "-P", "home:" + root,
        "-P", "temp:" + os.path.join(root, INCOMPLETE_DIR),
        "--progress-template", "download:" + PROGRESS_TEMPLATE,
        # A WHEN prefix keeps --print from implying --simulate, so this
        # reports the file that was really written, after the audio
        # extraction and the rename.
        "--print", "after_move:" + DATE_PREFIX
        + "%(release_date,upload_date|)s",
        "--print", "after_move:" + FILE_PREFIX + "%(filepath)s",
        # --print also implies --quiet, and that one the WHEN prefix does
        # not undo: without this the only things yt-dlp says are warnings,
        # errors and the line above - no progress, and nothing to log.
        "--no-quiet",
    ]
    if archive:
        args += ["--download-archive", os.path.join(root, ARCHIVE_NAME)]
    if no_playlist:
        args.append("--no-playlist")
    runtimes = (cfg.get("js_runtimes") or "").replace(" ", ",").split(",")
    if any(r.strip() for r in runtimes) and supports_option("--js-runtimes"):
        for name in runtimes:
            if name.strip():
                args += ["--js-runtimes", name.strip()]
    args += cookie_args(cfg)
    return args + list(urls)


# Order matters: YouTube's age gate and its bot check both open with "Sign
# in to confirm", so the more specific message has to be tried first.
_HINTS = (
    (re.compile(r"age.?restricted|inappropriate for some users"
                r"|confirm your age", re.I),
     "The video is age-restricted, which needs the cookies of a signed-in "
     "account. Point Lemon Zest at a Firefox profile that is signed in, or "
     "export a cookies.txt."),
    (re.compile(r"not a bot|sign in to confirm", re.I),
     "YouTube asked this client to sign in. Point Lemon Zest at a Firefox "
     "profile that is signed in, or export a cookies.txt."),
    (re.compile(r"members-only|join this channel", re.I),
     "The video is members-only, which needs the cookies of an account that "
     "has joined the channel."),
    (re.compile(r"private video|video unavailable", re.I),
     "The video is private or unavailable."),
    # YouTube signs its media URLs with a challenge that has to be run.
    # Without a runtime to run it in, yt-dlp gets no playable format and
    # says "The page needs to be reloaded", which explains nothing.
    (re.compile(r"page needs to be reloaded|signature solving failed"
                r"|n challenge solving failed|javascript runtime"
                r"|challenge solver", re.I),
     "YouTube needs a JavaScript runtime to hand over a playable format, "
     "and none of the ones enabled here is installed. Install Deno "
     "(https://deno.com) or Node, then try again - Lemon Zest already "
     "enables node, bun and quickjs alongside yt-dlp's own default of deno."),
    (re.compile(r"ffmpeg|ffprobe", re.I),
     "yt-dlp needs ffmpeg to extract audio. Install ffmpeg and put it on PATH."),
    (re.compile(r"could not copy .{0,40}cookie|permission denied.{0,40}cookies",
                re.I),
     "The Firefox cookie database could not be read. Close Firefox and try "
     "again, or export a cookies.txt."),
)


def explain(output):
    """Turn yt-dlp's own complaint into something actionable, when we can."""
    for pattern, hint in _HINTS:
        if pattern.search(output or ""):
            return hint
    return None


def probe(url, cfg, timeout=120):
    """Ask yt-dlp what is at a URL, without downloading anything.

    ``--flat-playlist`` so that a 500-track playlist costs one request
    rather than 500.
    """
    import json

    base = ytdlp_command()
    if not base:
        raise DownloadError(
            "yt-dlp is not installed. Install it with: pip install yt-dlp")
    args = (list(base) + ["--ignore-config", "-J", "--flat-playlist",
                          "--no-warnings"] + cookie_args(cfg) + [url])
    # Listing a playlist needs no format, so no JS challenge is solved here;
    # the runtime only matters once something is downloaded.
    try:
        res = subprocess.run(args, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=timeout, env=_child_env())
    except subprocess.TimeoutExpired:
        raise DownloadError("yt-dlp took too long to answer for " + url)
    if res.returncode != 0 or not (res.stdout or "").strip():
        detail = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
        last = next((l for l in reversed(detail.splitlines()) if l.strip()),
                    "yt-dlp could not read that URL")
        raise DownloadError(explain(detail) or last)
    try:
        info = json.loads(res.stdout)
    except ValueError:
        raise DownloadError("yt-dlp returned something that is not JSON")

    entries = info.get("entries") or []
    return {
        "url": url,
        "title": info.get("title") or info.get("id"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "is_playlist": bool(entries),
        "is_album": bool(entries) and pl_mod.is_album(url, info),
        "count": len(entries) if entries else 1,
        "entries": [{"title": e.get("title"), "duration": e.get("duration"),
                     "url": e.get("url") or e.get("id")}
                    for e in entries[:200]],
    }


# ------------------------------------------------------------------ running

def _resolve_root(con, cfg, root):
    """Which library folder a download lands in.

    In order: what was asked for, the configured folder, the folder already
    holding the most tracks, then the only registered library folder there
    is. Downloading into a folder the scanner does not watch would put files
    on disk that never reach the catalog, so guessing a known root beats
    defaulting to the shell's cwd.
    """
    candidate = (root or cfg.get("root") or "").strip()
    if not candidate:
        row = con.execute(
            "SELECT root, COUNT(*) n FROM track GROUP BY root ORDER BY n DESC"
        ).fetchone()
        candidate = row["root"] if row else ""
    if not candidate:
        # A library that has been scanned but holds no music yet: an empty
        # folder is where a library starts, and downloading is how it fills.
        known = db.roots(con)
        if len(known) == 1:
            candidate = known[0]
    if not candidate:
        raise DownloadError(
            "no library folder to download into. Scan one first, or name a "
            "destination folder.")
    candidate = os.path.abspath(candidate)
    if not os.path.isdir(candidate):
        raise DownloadError("not a directory: " + candidate)
    return candidate


def _printable(args):
    """The command as something that can be pasted back into a shell.

    Only arguments that need quoting get it, so the line stays readable;
    this is a debugging aid, not a shell escaper.
    """
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in args)


def _number(text):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_progress(line):
    """One progress line, as a dict.

    Seven fields now, three before. The short form is still read rather than
    dropped: a yt-dlp launched by an older Lemon Zest, or a stub in a test,
    still moves the bar it knows how to move.
    """
    body = line[len(PROGRESS_PREFIX):]
    parts = body.split("|")
    out = {"bytes": None, "bytes_total": None, "index": None, "count": None,
           "speed": None, "eta": None, "title": ""}
    if len(parts) >= 7:
        keys = ("bytes", "bytes_total", "index", "count", "speed", "eta")
        for key, raw in zip(keys, parts):
            out[key] = _number(raw)
        out["title"] = "|".join(parts[6:]).strip()
    else:
        out["bytes"] = _number(parts[0]) if parts else None
        out["bytes_total"] = _number(parts[1]) if len(parts) > 1 else None
        out["title"] = "|".join(parts[2:]).strip()
    return out


_ARCHIVED = re.compile(r"has already been recorded in the archive", re.I)
_VIDEO_ID = re.compile(r"(?:v=|/)([A-Za-z0-9_-]{11})(?:[&?#]|$)")


def video_id(text):
    """The eleven-character id inside a YouTube URL, or the id itself."""
    text = (text or "").strip()
    if not text:
        return None
    match = _VIDEO_ID.search(text)
    if match:
        return match.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) else None


def _requested_track_ids(con, urls, cfg, downloaded, log, probes=None):
    """Catalog ids for everything the request named, in the source's order.

    ``--embed-metadata`` writes the source URL into the ``purl`` tag and the
    scanner reads it, so a track downloaded by an earlier run can be found
    again by its video id. That is what makes a resumed download put the
    whole playlist in the playlist, rather than only the part that this run
    happened to fetch.

    Falls back to what this run downloaded whenever the source cannot be
    listed - a playlist missing its older half is a worse answer than one
    built from what is certain.
    """
    ids = []
    for url in urls:
        # Listed once at the start of the run to count the batch; reused
        # here rather than fetched again.
        info = (probes or {}).get(url)
        if info is None:
            try:
                info = probe(url, cfg)
            except DownloadError as exc:
                log.append("could not list %s to order the playlist: %s"
                           % (url, exc))
                return downloaded
        entries = info["entries"] or [{"url": url}]
        for entry in entries:
            vid = video_id(entry.get("url"))
            if vid and vid not in ids:
                ids.append(vid)

    by_video = {}
    for row in con.execute(
            "SELECT id, purl FROM track WHERE purl IS NOT NULL"):
        vid = video_id(row["purl"])
        if vid:
            by_video.setdefault(vid, row["id"])

    found = [by_video[v] for v in ids if v in by_video]
    missing = len(ids) - len(found)
    log.append("playlist order: %d of %d items are in the catalog%s"
               % (len(found), len(ids),
                  ", %d are not" % missing if missing else ""))
    # Anything downloaded but without a usable purl still belongs there.
    for tid in downloaded:
        if tid not in found:
            found.append(tid)
    return found or downloaded


# One item of a batch is finished when its file is written, when yt-dlp
# says it is already in the archive, or when it fails. Those three are what
# `done` counts - not bytes, which only ever describe the file currently
# being fetched and reset to zero forty times over a playlist.
def _new_batch(urls):
    return {"total": 0, "done": 0, "downloaded": 0, "skipped": 0,
            "failed": 0, "urls": len(urls), "listed": False,
            "started": time.time(), "finished": None,
            "current": None,
            # One entry per file that arrives, carrying what has happened to
            # it since. A download used to be a single bar and a log; the
            # thing a person actually wants to know is which of their forty
            # tracks are finished, and finished means identified and tagged,
            # not merely written to disk.
            "items": []}


def _count_items(urls, cfg, log):
    """How many items this batch will attempt, and what is in each URL.

    One flat listing per URL, before anything is fetched: it is the only way
    to say "12 of 47" rather than "12 so far, of who knows". A listing that
    fails is not fatal - the URL counts as one item, which is what it is
    unless it turns out to be a playlist.

    The listings are handed back so the playlist ordering at the end of the
    run does not pay for them a second time.
    """
    total, probes = 0, {}
    for url in urls:
        try:
            info = probe(url, cfg)
        except DownloadError as exc:
            log.append("could not list %s: %s" % (url, exc))
            total += 1
            continue
        probes[url] = info
        total += max(1, int(info.get("count") or 1))
    return total, probes


_ERROR_LINE = re.compile(r"^\s*ERROR:", re.I)


def _write_date(path, released, log):
    """Put the release date yt-dlp knows into the file's own date tag.

    Before the file is catalogued, so the catalog reads it back like any
    other tag. A file that will not take it keeps its year and says so.
    """
    from . import tags
    if not artwork.iso_date(released):
        return
    try:
        tags.write(path, {"date": released})
    except tags.TagWriteError as exc:
        log.append("could not write the release date into %s: %s"
                   % (path, exc))


def remember_url(con, url, info=None, playlist_name=None, root=None,
                 used=True):
    """Write down that this URL was asked for, and what was at it.

    Two things read this back: the short list of recent URLs on the download
    page, which is what stops a person going to find the link a second time,
    and the kept playlists, which are the same rows with a flag set.

    What was at the URL is only known when it could be listed, so a probe
    that failed leaves the title alone rather than blanking one an earlier
    run learned.
    """
    now = time.time()
    con.execute(
        "INSERT INTO download_url(url, first_used, last_used, uses) "
        "VALUES (?,?,?,0) ON CONFLICT(url) DO NOTHING", (url, now, now))
    sets, params = [], []
    if info:
        sets += ["title = ?", "uploader = ?", "is_playlist = ?",
                 "item_count = ?"]
        params += [info.get("title"), info.get("uploader"),
                   1 if info.get("is_playlist") else 0, info.get("count")]
    if playlist_name:
        sets.append("playlist_name = ?")
        params.append(playlist_name)
    if root:
        sets.append("root = ?")
        params.append(root)
    if used:
        sets += ["last_used = ?", "uses = uses + 1",
                 "seq = (SELECT COALESCE(MAX(seq), 0) + 1 FROM download_url)"]
        params.append(now)
    if sets:
        con.execute("UPDATE download_url SET " + ", ".join(sets)
                    + " WHERE url = ?", params + [url])
    con.commit()


def recent_urls(con, limit=5):
    """The last few URLs, newest first. What the download page offers back."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM download_url WHERE uses > 0 "
        "ORDER BY seq DESC LIMIT ?", (limit,))]


def kept_urls(con):
    """The playlist URLs somebody asked to keep, with where they stand."""
    out = []
    for row in con.execute("SELECT * FROM download_url WHERE kept = 1 "
                           "ORDER BY COALESCE(title, url)"):
        row = dict(row)
        got = con.execute(
            "SELECT p.id, COUNT(e.pos) AS entries FROM playlist p "
            "LEFT JOIN playlist_entry e ON e.playlist_id = p.id "
            "WHERE p.name = ? GROUP BY p.id",
            (row["playlist_name"] or "",)).fetchone()
        row["entries"] = got["entries"] if got else 0
        out.append(row)
    return out


def keep_url(con, url, kept=True, info=None, playlist_name=None, root=None):
    """Keep a playlist URL on the page, or stop keeping it."""
    remember_url(con, url, info=info, playlist_name=playlist_name, root=root,
                 used=False)
    con.execute("UPDATE download_url SET kept = ? WHERE url = ?",
                (1 if kept else 0, url))
    con.commit()
    return con.execute("SELECT * FROM download_url WHERE url = ?",
                       (url,)).fetchone()


def _auto_playlists(con, urls, cfg, probes, downloaded, log):
    """A playlist URL becomes a playlist, without being asked.

    The point of downloading somebody's playlist is usually to have that
    playlist, and a playlist is what a player syncs. Naming one by hand was
    the only way to get it, so a run that forgot to left forty tracks in the
    library with nothing tying them together.

    Only a URL that listed as a playlist makes one, and only under the name
    the source gives it. A single video does not become a playlist of one,
    and an album or EP does not become a playlist at all.
    """
    made = []
    for url in urls:
        info = (probes or {}).get(url)
        if not info or not info.get("is_playlist"):
            continue
        if pl_mod.is_album(url, info):
            log.append("%s is an album, not a playlist: no playlist made"
                       % (info.get("title") or url))
            continue
        name = pl_mod.norm_name(info.get("title") or "")
        if not name:
            continue
        # In the source's order, and the whole of it: the half this run
        # skipped as already downloaded belongs in the playlist too.
        wanted = _requested_track_ids(
            con, [url], cfg, downloaded if len(urls) == 1 else [], log,
            probes=probes)
        # Even with nothing new to add: the playlist is what the source
        # says it is, and a run that turned up nothing still ends with the
        # file on disk agreeing with the catalog.
        # Named for the service rather than for how it arrived: "download"
        # says what this program did, and what a person wants to see beside
        # a playlist is where it came from.
        got = pl_mod.append_tracks(con, name, wanted,
                                   origin=pl_mod.origin_of(url) if
                                   pl_mod.origin_of(url) != "local"
                                   else "download",
                                   source_uri=url)
        got["url"] = url
        made.append(got)
    return made


def download(con, urls, root=None, playlist=None, cfg=None, on_event=None,
             on_batch=None, no_playlist=False, archive=True, output=None,
             audio_format=None, audio_quality=None, pipeline=True):
    """Download ``urls`` into a library folder and index what arrives.

    Returns a summary dict. The files that arrive are indexed one by one
    rather than by rescanning: a download is a handful of files, and walking
    a 2,300-file library to find three of them is work nobody asked for.

    Each file is also carried the rest of the way - catalogued, identified
    and tagged - as soon as yt-dlp has finished writing it, on a worker
    thread beside the download. That is what ``pipeline`` turns off. The
    batch form it replaces made the whole run feel unfinished until the last
    second of it: forty tracks would sit in the library with channel names
    for artists and video frames for covers until the fortieth had been
    fetched, and only then begin a rate-limited pass that took as long
    again. Pass ``pipeline=False`` to index at the end instead and leave
    identification to the caller.
    """
    cfg = cfg or get_config(con)
    urls = [u.strip() for u in ([urls] if isinstance(urls, str) else urls)
            if u and u.strip()]
    if not urls:
        raise DownloadError("no URL given")
    root = _resolve_root(con, cfg, root)
    # Downloading into a folder is a claim that it is a library folder, so
    # it shows up in the picker next time even before it has been scanned.
    db.add_root(con, root)

    def emit(kind, detail, done=0, total=0):
        if on_event:
            on_event(kind, detail, done, total)

    batch = _new_batch(urls)
    # Two threads write to the batch now - this one as yt-dlp talks, and the
    # pipeline worker as each file is finished - so the snapshot is taken
    # under a lock. Without it a copy taken mid-append raises rather than
    # returning a slightly stale picture, which is all a progress view ever
    # needs.
    lock = threading.Lock()

    def report():
        """Hand the caller a copy: this dict keeps changing under them."""
        if on_batch:
            with lock:
                snapshot = dict(batch)
                snapshot["current"] = (dict(batch["current"])
                                       if batch["current"] else None)
                snapshot["items"] = [dict(i) for i in batch["items"]]
            on_batch(snapshot)

    emit("start", "%d URL%s" % (len(urls), "" if len(urls) == 1 else "s"))
    report()

    # Counted before anything is fetched, so the progress bar has a
    # denominator from the first second rather than from the last one.
    listing_log = []
    if no_playlist:
        batch["total"], probes = len(urls), {}
    else:
        emit("listing", "listing what is at %d URL%s"
             % (len(urls), "" if len(urls) == 1 else "s"))
        batch["total"], probes = _count_items(urls, cfg, listing_log)
    batch["listed"] = True
    report()

    args = build_args(cfg, urls, root, no_playlist=no_playlist,
                      archive=archive, output=output,
                      audio_format=audio_format, audio_quality=audio_quality)

    # The whole run, kept for the person reading it afterwards: the command
    # first, because "what did it actually run" is the first question asked
    # of a download that went wrong, and the answer includes which cookie
    # source was chosen.
    log = ["$ " + _printable(args)]
    emit("command", log[0])
    log.extend(listing_log)
    if batch["total"]:
        line = "batch: %d item%s to fetch" % (batch["total"],
                                              "" if batch["total"] == 1 else "s")
        log.append(line)
        emit("output", line)

    files = []
    tail = []
    skipped = 0      # already in the download archive

    # Everything each finished file still needs doing to it, on a thread of
    # its own: catalogued here, and handed to the identification queue,
    # which is the only thing in the program that talks to MusicBrainz and
    # therefore the only thing that can keep to one request a second.
    db_path = db.path_of(con)
    arrivals = queue.Queue()
    indexed = {"added": 0, "updated": 0, "failed": 0}
    track_ids = []
    box = {"queue": None}

    def set_state(item, state, detail, **fields):
        with lock:
            item["state"] = state
            item["detail"] = detail
            item.update(fields)
        report()

    def carry_on(item, c2):
        """Catalog one arrival, then hand it to the identification queue."""
        set_state(item, "indexing", "adding to the catalog")
        counts_one, ids = scan_mod.index_paths(c2, root, [item["path"]])
        for key in indexed:
            indexed[key] += counts_one[key]
        track_ids.extend(ids)
        if not ids:
            # Outside the library root, or unreadable: the catalog has no
            # row for it, so there is nothing to look up.
            set_state(item, "failed", "could not be catalogued")
            return
        row = c2.execute("SELECT title, artist, content_key FROM track "
                         "WHERE id = ?", (ids[0],)).fetchone()
        name = " - ".join(x for x in (row["artist"], row["title"]) if x)
        set_state(item, "queued", "waiting to be identified",
                  **({"title": name} if name else {}))

        def on_state(key, state, detail, enrich=None):
            # The queue works on its own clock, so these arrive after the
            # download itself has finished. The batch keeps updating, which
            # is the point: the run is not over until the last track has
            # been looked at.
            set_state(item, state, detail,
                      **({"enrich": enrich} if enrich else {}))

        box["queue"].submit([row["content_key"]], priority="arrival",
                            label=name or item["title"], on_state=on_state)

    def pipeline_worker():
        from . import enrichq
        try:
            c2 = db.connect(db_path)
            box["queue"] = enrichq.get_queue(db_path)
        except Exception as exc:      # noqa: BLE001
            # Nothing was catalogued, so the run falls back to indexing at
            # the end. A download whose files never reach the catalog is a
            # download that did not happen; a download that is not tagged
            # as promptly as it might be is merely disappointing.
            box["broken"] = exc
            return
        try:
            for item in iter(arrivals.get, None):
                try:
                    carry_on(item, c2)
                except Exception as exc:      # noqa: BLE001
                    # A download does not fail because one file could not be
                    # identified: the audio is on disk either way.
                    set_state(item, "failed", str(exc))
        finally:
            c2.close()

    worker = None
    if pipeline and db_path:
        worker = threading.Thread(target=pipeline_worker, daemon=True)
        worker.start()

    released = None
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            env=_child_env())
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith(PROGRESS_PREFIX):
                p = _parse_progress(line)
                batch["current"] = {
                    "title": p["title"] or "downloading",
                    "index": p["index"], "count": p["count"],
                    "bytes": p["bytes"] or 0,
                    "bytes_total": p["bytes_total"] or 0,
                    "speed": p["speed"], "eta": p["eta"],
                }
                # A playlist yt-dlp is further into than our own counters
                # believe - the archive skips it printed before we started
                # counting, say - moves the batch forward rather than
                # letting it sit still while the numbers disagree.
                if p["index"] and len(urls) == 1:
                    batch["done"] = max(batch["done"], p["index"] - 1)
                emit("progress", p["title"] or "downloading",
                     batch["done"], batch["total"])
                report()
                continue
            if line.startswith(DATE_PREFIX):
                released = line[len(DATE_PREFIX):].strip()
                continue
            if line.startswith(FILE_PREFIX):
                path = line[len(FILE_PREFIX):].strip()
                if path:
                    _write_date(path, released, log)
                    released = None
                    files.append(path)
                    log.append("wrote " + path)
                    del log[:-MAX_LOG]
                    batch["downloaded"] += 1
                    batch["done"] += 1
                    batch["total"] = max(batch["total"], batch["done"])
                    batch["current"] = None
                    item = {"n": len(batch["items"]) + 1,
                            "title": os.path.splitext(
                                os.path.basename(path))[0],
                            "path": norm(path), "state": "queued",
                            "detail": "waiting to be identified",
                            "enrich": None}
                    with lock:
                        batch["items"].append(item)
                    if worker:
                        arrivals.put(item)
                    emit("file", path, batch["done"], batch["total"])
                    report()
                continue
            if _ARCHIVED.search(line):
                skipped += 1
                batch["skipped"] += 1
                batch["done"] += 1
                batch["total"] = max(batch["total"], batch["done"])
                report()
            elif _ERROR_LINE.search(line):
                # One item, one failure: yt-dlp prints its ERROR line once
                # per item it gives up on, and the continuation lines that
                # sometimes follow do not start with ERROR.
                batch["failed"] += 1
                batch["done"] += 1
                batch["total"] = max(batch["total"], batch["done"])
                report()
            tail.append(line)
            del tail[:-60]
            log.append(line)
            del log[:-MAX_LOG]
            emit("error" if "ERROR" in line else "output", line)
    finally:
        proc.stdout.close()
        code = proc.wait()

    # yt-dlp is done; the worker may not be. The last file to arrive is
    # still being looked up, and the run is not finished until it is - the
    # whole point being that the library is never left half-tagged.
    batch["current"] = None
    report()
    if worker:
        arrivals.put(None)
        if batch["items"]:
            emit("index", "the last of them", batch["done"], batch["total"])
        worker.join()
        # The files are on disk and in the catalog; identifying them is the
        # queue's business now, and it goes on after this returns. Waiting
        # for it here would put the download back behind a rate limit it
        # does not need to be behind.

    # A partial success is the normal outcome for a playlist with one dead
    # video in it, so a non-zero exit only aborts when nothing was fetched.
    if code != 0 and not files:
        text = "\n".join(tail)
        last = next((l for l in reversed(tail) if l.strip()), "")
        log.append("yt-dlp exited with status %d, having written nothing"
                   % code)
        raise DownloadError(explain(text) or last
                            or "yt-dlp exited with status %d" % code, log=log)

    batch["finished"] = time.time()
    report()

    arrived = [f for f in files if os.path.isfile(f)]
    if worker and not box.get("broken"):
        # Already done, one file at a time, as they landed.
        counts = indexed
    else:
        emit("index", "%d file%s" % (len(arrived),
                                     "" if len(arrived) == 1 else "s"),
             len(arrived), len(arrived))
        counts, track_ids = scan_mod.index_paths(con, root, arrived)
    if box.get("broken"):
        log.append("identifying arrivals as they landed could not be "
                   "started (%s); indexed at the end instead" % box["broken"])
    log.append("yt-dlp exited with status %d" % code)
    log.append("indexed %d added, %d updated, %d not catalogued"
               % (counts["added"], counts["updated"], counts["failed"]))
    queued = sum(1 for i in batch["items"]
                 if i["state"] not in ("failed", "skipped"))
    if queued:
        log.append("%d file%s queued for identification"
                   % (queued, "" if queued == 1 else "s"))

    # A run that was interrupted leaves audio on disk and a line in the
    # archive, but no catalog row - the indexing happens here, after yt-dlp
    # exits. Running it again skips those files, and says so rather than
    # naming them, so without this they would sit in the library
    # permanently invisible. A rescan of the root is stat-only for the
    # thousands of files that have not changed, so it costs little and is
    # the one thing that cannot miss them.
    if skipped:
        log.append("%d already in the download archive; rescanning %s to "
                   "catalog anything an earlier run left behind"
                   % (skipped, root))
        emit("index", "rescanning the library folder", 0, 0)
        rescan = scan_mod.scan(con, root)
        log.append("rescan: %d added, %d updated, %d unchanged"
                   % (rescan["added"], rescan["updated"], rescan["unchanged"]))

    made = []
    if playlist:
        # Everything the request asked for, not merely what this run
        # fetched: the skipped ones belong in the playlist too, and asking
        # the source for its own order beats the order they downloaded in.
        wanted = _requested_track_ids(con, urls, cfg, track_ids, log,
                                      probes=probes) \
            if skipped else track_ids
        if wanted:
            made.append(pl_mod.append_tracks(con, playlist, wanted,
                                             origin="download"))
    else:
        # Nobody named one, but a playlist URL is a playlist: it gets one
        # under the name the source gives it.
        made = _auto_playlists(con, urls, cfg, probes, track_ids, log)
    for got in made:
        log.append("playlist %s: %d added, %d already there, %d entries"
                   % (got["name"], got["added"], got["skipped"],
                      got["entries"]))
        # And on disk beside the library, not only in the catalog. The
        # device copies are written at sync time against that device's own
        # layout; this is the one a player pointed at the library reads.
        try:
            wrote = pl_mod.write_local(con, got["name"], root)
            got["file"] = wrote["path"]
            log.append("wrote %s (%d entries)"
                       % (wrote["path"], wrote["entries"]))
        except Exception as exc:      # noqa: BLE001
            # The catalog has the playlist either way; a folder that cannot
            # be written is not a reason to fail a download that worked.
            got["file"] = None
            log.append("could not write the playlist file: %s" % exc)
    added_to = made[0] if made else None

    # Written down last, when what was at each URL and what it fed is known.
    for url in urls:
        by_url = next((m for m in made if m.get("url") == url), None)
        fed = by_url or (added_to if playlist else None)
        remember_url(con, url, info=(probes or {}).get(url),
                     playlist_name=(fed or {}).get("name"), root=norm(root))
    del log[:-MAX_LOG]

    summary = {
        "root": norm(root),
        "files": [norm(f) for f in arrived],
        "downloaded": len(arrived),
        "added": counts["added"],
        "updated": counts["updated"],
        "failed_index": counts["failed"],
        "playlist": added_to,
        # Every playlist this run touched, which is more than one when
        # several playlist URLs were fetched at once. ``playlist`` is the
        # first of them, kept for callers that only ever expected one.
        "playlists": made,
        # How many arrivals went to the identification queue. Zero with
        # the pipeline turned off, and then the caller still owes these
        # files a look.
        "queued": queued if worker and not box.get("broken") else 0,
        # Whether the run had to fall back to a full rescan, which can turn
        # up files an interrupted earlier run left uncatalogued. Those never
        # went past the pipeline, so they are still owed a look.
        "rescanned": bool(skipped),
        "exit_code": code,
        "skipped": skipped,
        "errors": [l for l in tail if "ERROR" in l][-10:],
        "log": log,
        "at": time.time(),
        # What the run was, as counts: how many items it set out to fetch
        # and what became of each one. The interface shows this while the
        # run is going and keeps it afterwards, which is the difference
        # between a log you have to read and a batch you can see.
        "batch": batch,
    }
    emit("done", "%d downloaded" % len(arrived), len(arrived), len(arrived))
    return summary
