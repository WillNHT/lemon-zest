"""Fetch the third-party tools that go inside the executable.

The packaged build is meant to need nothing installed: ffmpeg to extract
audio, Deno to run YouTube's JavaScript challenge, fpcalc to fingerprint.
None of them are Python packages, so they are downloaded here at build time
and folded into the binary by the spec. yt-dlp is the fourth, and is not
here: it *is* a Python package, so pip installs it and PyInstaller finds it
through the import graph.

Downloads are cached under ``build/tools-cache`` and keyed by URL, so a
rebuild on the same machine costs nothing. A build that must not reach the
network can point ``LEMONZEST_TOOLS_DIR`` at a folder that already holds
the executables, and nothing is fetched.

Run it on its own to prime the cache:

    python packaging/fetch_tools.py build/tools
"""
import hashlib
import os
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile

# Where the tools sit inside the bundle. Must match lemonzest.bundled's
# TOOLS_DIRNAME - that module is what puts this folder on PATH at startup.
TOOLS_DEST = "tools"

# Per platform, the archives to fetch and which members to keep. Members are
# matched on basename, because every project lays its archive out its own
# way and none of those layouts is worth encoding here.
#
# ffmpeg: BtbN's LGPL build. Chosen over gyan.dev's for two reasons - it is
# LGPL rather than GPL, which is the lighter obligation for shipping a
# binary inside another one, and it is served from GitHub like everything
# else here, so one host's certificate chain cannot break the build.
# Deno: the release yt-dlp itself prefers. fpcalc: the Chromaprint
# project's own binary, pinned - the only one of the three whose "latest"
# URL is not maintained.
SOURCES = {
    "win32": [
        {
            "name": "ffmpeg",
            "url": "https://github.com/BtbN/FFmpeg-Builds/releases/latest/"
                   "download/ffmpeg-master-latest-win64-lgpl-shared.zip",
            "members": ["ffmpeg.exe", "ffprobe.exe"],
            # The shared build: two small executables and the DLLs they
            # link against, which together are a quarter of what two
            # statically linked copies of the same libraries would cost.
            "member_dir": "bin",
        },
        {
            "name": "deno",
            "url": "https://github.com/denoland/deno/releases/latest/download/"
                   "deno-x86_64-pc-windows-msvc.zip",
            "members": ["deno.exe"],
        },
        {
            "name": "fpcalc",
            "url": "https://github.com/acoustid/chromaprint/releases/download/"
                   "v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip",
            "members": ["fpcalc.exe"],
        },
    ],
    "linux": [
        {
            "name": "ffmpeg",
            "url": "https://github.com/BtbN/FFmpeg-Builds/releases/latest/"
                   "download/ffmpeg-master-latest-linux64-lgpl-shared.tar.xz",
            "members": ["ffmpeg", "ffprobe"],
            "member_dir": "bin",
        },
        {
            "name": "deno",
            "url": "https://github.com/denoland/deno/releases/latest/download/"
                   "deno-x86_64-unknown-linux-gnu.zip",
            "members": ["deno"],
        },
        {
            "name": "fpcalc",
            "url": "https://github.com/acoustid/chromaprint/releases/download/"
                   "v1.5.1/chromaprint-fpcalc-1.5.1-linux-x86_64.tar.gz",
            "members": ["fpcalc"],
        },
    ],
}


def platform_key():
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def cache_path(cache_dir, url):
    """A stable filename for a URL, so a rebuild reuses the download."""
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    suffix = ""
    for ext in (".tar.xz", ".tar.gz", ".zip"):
        if url.endswith(ext):
            suffix = ext
    return os.path.join(cache_dir, digest + suffix)


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"     cached {os.path.basename(dest)}")
        return dest
    print(f"     fetching {url}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "lemon-zest-build"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    os.replace(tmp, dest)
    return dest


def _names(archive):
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()]
    with tarfile.open(archive) as tf:
        return [i.name for i in tf.getmembers() if i.isfile()]


def extract(archive, members, out_dir, member_dir=None):
    """Pull files out of ``archive``, flat into ``out_dir``.

    ``members`` names the basenames that must be there. ``member_dir``, when
    given, takes everything in the archive directory of that name as well -
    which is how a shared ffmpeg build ships: one small .exe beside the DLLs
    it needs, and every one of them has to travel with it.
    """
    wanted = set(members)
    if member_dir:
        for name in _names(archive):
            parts = name.replace("\\", "/").split("/")
            if len(parts) > 1 and parts[-2] == member_dir:
                wanted.add(parts[-1])
    # ffplay is a video player. It is 18 MB of things this program will
    # never do, and it travels in the same bin/ folder as the two tools
    # that are actually wanted.
    wanted = {n for n in wanted if not n.startswith("ffplay")}
    found = []
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if base in wanted:
                    target = os.path.join(out_dir, base)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found.append(target)
    else:
        with tarfile.open(archive) as tf:
            for info in tf.getmembers():
                base = os.path.basename(info.name)
                if info.isfile() and base in wanted:
                    target = os.path.join(out_dir, base)
                    src = tf.extractfile(info)
                    with open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found.append(target)
    for path in found:
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)
    missing = wanted - {os.path.basename(p) for p in found}
    if missing:
        raise SystemExit(f"{archive}: no {', '.join(sorted(missing))} inside it")
    return found


def stage(out_dir, cache_dir=None):
    """Put every bundled tool in ``out_dir``. Returns the paths.

    ``LEMONZEST_TOOLS_DIR`` short-circuits the whole thing: whatever is in
    that folder is what ships, which is how an offline or air-gapped build
    supplies its own ffmpeg.
    """
    override = os.environ.get("LEMONZEST_TOOLS_DIR")
    if override:
        if not os.path.isdir(override):
            raise SystemExit(f"LEMONZEST_TOOLS_DIR is not a folder: {override}")
        found = [os.path.join(override, n) for n in sorted(os.listdir(override))]
        print(f"  tools: using {override} ({len(found)} files)")
        return found

    key = platform_key()
    sources = SOURCES.get(key)
    if not sources:
        print(f"  tools: nothing defined for {key}; the build will rely on PATH")
        return []

    cache_dir = cache_dir or os.path.join(os.path.dirname(out_dir), "tools-cache")
    os.makedirs(out_dir, exist_ok=True)
    staged = []
    for src in sources:
        have = [os.path.join(out_dir, m) for m in src["members"]]
        if all(os.path.isfile(p) for p in have):
            print(f"  tools: {src['name']} already staged")
            staged += have
            continue
        print(f"  tools: {src['name']}")
        archive = download(src["url"], cache_path(cache_dir, src["url"]))
        staged += extract(archive, src["members"], out_dir,
                          member_dir=src.get("member_dir"))
    return staged


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join("build", "tools")
    for path in stage(os.path.abspath(target)):
        print(path)
