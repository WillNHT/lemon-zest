"""The tools that ship inside the executable.

The packaged build is meant to need nothing else installed: no ffmpeg on
PATH, no Deno for YouTube's JavaScript challenges, no Chromaprint for
fingerprinting, no separate yt-dlp. Those four are folded into the binary
and unpacked beside it at startup.

Making them *usable* is one line of policy, applied once: put the folder
they live in at the front of PATH. Everything downstream - yt-dlp looking
for ffmpeg, yt-dlp looking for a JS runtime, ``shutil.which("fpcalc")`` -
already asks PATH, so nothing else has to learn about the bundle.

A tool the user installed themselves still wins for yt-dlp, which is
resolved separately and prefers PATH; for ffmpeg and Deno the bundled copy
goes first, because a broken or ancient ffmpeg on PATH is the failure this
whole arrangement exists to remove.
"""
import os
import sys

# Where the staged tools live inside the bundle, and beside an unpacked
# build. One name, used by the spec and by this module.
TOOLS_DIRNAME = "tools"

_installed = False


def frozen():
    """Whether this is the packaged executable rather than a source run."""
    return bool(getattr(sys, "frozen", False))


def _candidates():
    """Every place a bundled tools folder could be, best first."""
    out = []
    # PyInstaller onefile: everything datas-ed in is unpacked here.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, TOOLS_DIRNAME))
    if frozen():
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # A onedir build, and the escape hatch: dropping ffmpeg.exe into a
        # `tools` folder next to the .exe is how someone replaces ours.
        out.append(os.path.join(exe_dir, TOOLS_DIRNAME))
        out.append(exe_dir)
    return out


def tools_dirs():
    return [d for d in _candidates() if os.path.isdir(d)]


def tool(name):
    """The bundled executable called ``name``, or None.

    Asked for by bare name: the extension is this platform's, not the
    caller's problem.
    """
    exts = (".exe", ".bat", "") if os.name == "nt" else ("",)
    for d in tools_dirs():
        for ext in exts:
            path = os.path.join(d, name + ext)
            if os.path.isfile(path):
                return path
    return None


def install():
    """Put the bundled tools at the front of PATH, once per process.

    Called from the entry points rather than at import time, so importing
    lemonzest from a script never rewrites that script's environment.
    """
    global _installed
    if _installed:
        return []
    _installed = True
    dirs = tools_dirs()
    if not dirs:
        return []
    parts = [d for d in dirs]
    existing = os.environ.get("PATH", "")
    if existing:
        parts.append(existing)
    os.environ["PATH"] = os.pathsep.join(parts)
    return dirs


def status():
    """What the bundle provides here - for the interface and the CLI."""
    names = ("ffmpeg", "ffprobe", "deno", "fpcalc")
    return {
        "frozen": frozen(),
        "dirs": tools_dirs(),
        "tools": {n: tool(n) for n in names},
    }
