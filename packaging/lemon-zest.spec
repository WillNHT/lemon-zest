# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone Lemon Zest executable.

Build it from the repository root:

    pip install -e . -r packaging/requirements-build.txt
    pyinstaller --clean --noconfirm packaging/lemon-zest.spec

The result is dist/lemon-zest.exe on Windows, dist/lemon-zest elsewhere.

Everything the build needs to know is derived here rather than passed in, so
the same command works on a laptop and on a CI runner: the version comes
from the package, and the interface is staged and stamped as part of the
spec's own execution.
"""
import os
import re
import sys

# PyInstaller runs a spec with exec(), so __file__ is not defined; SPECPATH
# is the directory the spec lives in.
HERE = SPECPATH
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import stage_frontend  # noqa: E402
import version_info  # noqa: E402

init = open(os.path.join(ROOT, "lemonzest", "__init__.py"),
            encoding="utf-8").read()
match = re.search(r'__version__ = "([^"]+)"', init)
if not match:
    raise SystemExit("could not read __version__ from lemonzest/__init__.py")
VERSION = match.group(1)

# The interface is copied out of the tree and stamped with the version
# before it is folded in, so the running binary can tell you what it is.
WORK = os.path.join(ROOT, "build", "frontend")
stage_frontend.stage(os.path.join(ROOT, "lemonzest", "web"), WORK, VERSION)

# The version resource is Windows-only; elsewhere PyInstaller ignores it.
VERSION_FILE = None
if sys.platform == "win32":
    VERSION_FILE = version_info.write(
        VERSION, os.path.join(ROOT, "build", "file_version_info.txt"))

# The interface is served off disk at runtime, so the staged frontend ships
# with the binary under the package path the server looks in.
datas = [(WORK, os.path.join("lemonzest", "web"))]

# Flask and Click are imported directly; the rest are pulled in by name at
# runtime and would otherwise be missed by the import graph.
hiddenimports = [
    # Imported inside functions rather than at module scope - the interface
    # so a CLI-only run does not pay for Flask, the rest so enrichment stays
    # optional - which is exactly the shape the import graph does not always
    # follow.
    "lemonzest.server",
    "lemonzest.enrich",
    "lemonzest.organise",
    "lemonzest.tags",
    "mutagen",
    "mutagen.easyid3",
    "mutagen.flac",
    "mutagen.id3",
    "mutagen.mp3",
    "mutagen.mp4",
    "mutagen.oggopus",
    "mutagen.oggvorbis",
    "psutil",
]

excludes = [
    "tkinter",
    "pytest",
    "setuptools",
    "unittest",
]

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lemon-zest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # The binary is the CLI as well as the interface, so it keeps a console.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_FILE,
)
