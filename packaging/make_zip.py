"""Zip the built folder into the archive a release ships.

The build is a folder - lemon-zest.exe with its libraries and the bundled
ffmpeg, Deno, yt-dlp and fpcalc beside it - so the thing people download is
a zip of it. Extract anywhere, run lemon-zest.exe from inside the extracted
folder, and nothing else is needed.

Deliberately not a onefile .exe: the tools are a quarter of a gigabyte, and
a onefile build unpacks all of it to a temporary directory on every single
launch.

    python packaging/make_zip.py                    # dist/lemon-zest.zip
    python packaging/make_zip.py 1.2.3              # named for a version
"""
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def version():
    init = open(os.path.join(ROOT, "lemonzest", "__init__.py"),
                encoding="utf-8").read()
    match = re.search(r'__version__ = "([^"]+)"', init)
    return match.group(1) if match else "0.0.0"


def platform_tag():
    if sys.platform.startswith("win"):
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos"
    return "linux-x64"


def make(src=None, out=None, ver=None):
    src = src or os.path.join(ROOT, "dist", "lemon-zest")
    if not os.path.isdir(src):
        raise SystemExit(f"no build at {src} - run pyinstaller first")
    ver = ver or version()
    out = out or os.path.join(ROOT, "dist",
                              f"lemon-zest-{ver}-{platform_tag()}.zip")

    # The archive holds one top-level folder, so extracting it never scatters
    # three hundred files into whatever directory the user was in.
    top = f"lemon-zest-{ver}"
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _dirnames, filenames in os.walk(src):
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, src)
                zf.write(path, os.path.join(top, rel))
                total += os.path.getsize(path)
    print(f"{out}  ({os.path.getsize(out) / 2**20:.0f} MB zipped, "
          f"{total / 2**20:.0f} MB extracted)")
    return out


if __name__ == "__main__":
    make(ver=sys.argv[1] if len(sys.argv) > 1 else None)
