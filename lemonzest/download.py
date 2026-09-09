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
import re
import shutil
import subprocess
import sys
import time

from . import db
from . import playlists as pl_mod
from . import scan as scan_mod
from .paths import norm

# Markers on stdout. yt-dlp's own progress output is written for humans;
# these two templates are parsed, so they carry a prefix that cannot
# plausibly appear in a video title.
PROGRESS_PREFIX = "[lz-progress]"
FILE_PREFIX = "[lz-file]"

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


def ytdlp_version():
    cmd = ytdlp_command()
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd + ["--version"], capture_output=True,
                             text=True, timeout=30, encoding="utf-8",
                             errors="replace")
    except Exception:
        return None
    return out.stdout.strip() or None


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


def js_runtime_status(cfg=None):
    """Which JavaScript runtime a download would use, and what is installed.

    Reported next to the cookie source, for the same reason: without one,
    every YouTube video fails, and the failure says nothing about why.
    """
    enabled = {"deno"}   # yt-dlp's own default, whatever Lemon Zest passes
    for name in ((cfg or {}).get("js_runtimes") or "").replace(" ", ",").split(","):
        if name.strip():
            enabled.add(name.strip().lower())
    found = [{"name": n, "path": norm(shutil.which(n))}
             for n in JS_RUNTIMES if shutil.which(n)]
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
        "--progress-template",
        ("download:" + PROGRESS_PREFIX
         + "%(progress.downloaded_bytes)s|"
         + "%(progress.total_bytes,progress.total_bytes_estimate)s|"
         + "%(info.title)s"),
        # A WHEN prefix keeps --print from implying --simulate, so this
        # reports the file that was really written, after the audio
        # extraction and the rename.
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
    """Split one progress line into (done, total, title)."""
    parts = line[len(PROGRESS_PREFIX):].split("|", 2)
    return (_number(parts[0]) if parts else None,
            _number(parts[1]) if len(parts) > 1 else None,
            parts[2].strip() if len(parts) > 2 else "")


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


def _requested_track_ids(con, urls, cfg, downloaded, log):
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


def download(con, urls, root=None, playlist=None, cfg=None, on_event=None,
             no_playlist=False, archive=True, output=None, audio_format=None,
             audio_quality=None):
    """Download ``urls`` into a library folder and index what arrives.

    Returns a summary dict. The files that arrive are indexed one by one
    rather than by rescanning: a download is a handful of files, and walking
    a 2,300-file library to find three of them is work nobody asked for.
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

    args = build_args(cfg, urls, root, no_playlist=no_playlist,
                      archive=archive, output=output,
                      audio_format=audio_format, audio_quality=audio_quality)

    def emit(kind, detail, done=0, total=0):
        if on_event:
            on_event(kind, detail, done, total)

    emit("start", "%d URL%s" % (len(urls), "" if len(urls) == 1 else "s"))

    # The whole run, kept for the person reading it afterwards: the command
    # first, because "what did it actually run" is the first question asked
    # of a download that went wrong, and the answer includes which cookie
    # source was chosen.
    log = ["$ " + _printable(args)]
    emit("command", log[0])

    files = []
    tail = []
    skipped = 0      # already in the download archive
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
                done, total, title = _parse_progress(line)
                emit("progress", title or "downloading", done or 0, total or 0)
                continue
            if line.startswith(FILE_PREFIX):
                path = line[len(FILE_PREFIX):].strip()
                if path:
                    files.append(path)
                    log.append("wrote " + path)
                    del log[:-MAX_LOG]
                    emit("file", path, len(files), 0)
                continue
            if _ARCHIVED.search(line):
                skipped += 1
            tail.append(line)
            del tail[:-60]
            log.append(line)
            del log[:-MAX_LOG]
            emit("error" if "ERROR" in line else "output", line)
    finally:
        proc.stdout.close()
        code = proc.wait()

    # A partial success is the normal outcome for a playlist with one dead
    # video in it, so a non-zero exit only aborts when nothing was fetched.
    if code != 0 and not files:
        text = "\n".join(tail)
        last = next((l for l in reversed(tail) if l.strip()), "")
        log.append("yt-dlp exited with status %d, having written nothing"
                   % code)
        raise DownloadError(explain(text) or last
                            or "yt-dlp exited with status %d" % code, log=log)

    arrived = [f for f in files if os.path.isfile(f)]
    emit("index", "%d file%s" % (len(arrived), "" if len(arrived) == 1 else "s"),
         len(arrived), len(arrived))
    counts, track_ids = scan_mod.index_paths(con, root, arrived)
    log.append("yt-dlp exited with status %d" % code)
    log.append("indexed %d added, %d updated, %d not catalogued"
               % (counts["added"], counts["updated"], counts["failed"]))

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

    added_to = None
    if playlist:
        # Everything the request asked for, not merely what this run
        # fetched: the skipped ones belong in the playlist too, and asking
        # the source for its own order beats the order they downloaded in.
        wanted = _requested_track_ids(con, urls, cfg, track_ids, log) \
            if skipped else track_ids
        if wanted:
            added_to = pl_mod.append_tracks(con, playlist, wanted,
                                            origin="download")
            log.append("playlist %s: %d added, %d already there, %d entries"
                       % (added_to["name"], added_to["added"],
                          added_to["skipped"], added_to["entries"]))
    del log[:-MAX_LOG]

    summary = {
        "root": norm(root),
        "files": [norm(f) for f in arrived],
        "downloaded": len(arrived),
        "added": counts["added"],
        "updated": counts["updated"],
        "failed_index": counts["failed"],
        "playlist": added_to,
        "exit_code": code,
        "skipped": skipped,
        "errors": [l for l in tail if "ERROR" in l][-10:],
        "log": log,
        "at": time.time(),
    }
    emit("done", "%d downloaded" % len(arrived), len(arrived), len(arrived))
    return summary
