"""Make embedded cover art something every player can draw.

Two things Rockbox gets wrong that a desktop player forgives:

  * **Shape.** A YouTube thumbnail is a 16:9 video frame, often a square
    cover letterboxed inside it. Rockbox scales it into a square slot and
    shows the bars. Cropping to the centre square keeps the cover and drops
    the padding.
  * **Colour.** Rockbox's JPEG decoder only draws colour for 4:2:0 and 4:2:2
    chroma subsampling, and only for baseline files; anything else (ffmpeg's
    default 4:4:4, a progressive JPEG) comes out grey. Re-encoding as a
    baseline 4:2:0 JPEG is what every player reads in colour.

Both are one ffmpeg call. Without ffmpeg the picture is returned untouched:
a padded cover is still better than none.
"""
import re
import shutil
import subprocess

from . import bundled

# The centre square of whatever came in. Commas inside the expression are
# escaped because ffmpeg would otherwise read them as a filter separator.
CROP = r"crop=min(iw\,ih):min(iw\,ih)"
# Big enough for any DAP screen, small enough not to bloat every file.
MAX_EDGE = 600
FILTER = CROP + f",scale='min({MAX_EDGE},iw)':-2"
PIX_FMT = "yuvj420p"

# The same conversion, as yt-dlp's ThumbnailsConvertor takes it.
YTDLP_PPA = ("ThumbnailsConvertor+ffmpeg_o:"
             f"-vf \"{FILTER}\" -pix_fmt {PIX_FMT} -q:v 2")


def ffmpeg():
    return bundled.tool("ffmpeg") or shutil.which("ffmpeg")


def normalise(data, mime="image/jpeg", runner=None):
    """Return ``(bytes, mime)``: a square, baseline, 4:2:0 JPEG.

    Falls back to the input when ffmpeg is missing or refuses the picture.
    """
    if not data:
        return data, mime
    exe = ffmpeg()
    if not exe:
        return data, mime
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-vf", FILTER, "-pix_fmt", PIX_FMT, "-q:v", "2",
           "-frames:v", "1", "-f", "mjpeg", "pipe:1"]
    run = runner or subprocess.run
    try:
        res = run(cmd, input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return data, mime
    if res.returncode != 0 or not res.stdout:
        return data, mime
    return res.stdout, "image/jpeg"


def is_player_safe(data):
    """True for a square, baseline JPEG with 4:2:0 or 4:2:2 chroma.

    Read from the JPEG header alone, so checking a whole library does not
    run ffmpeg once per file.
    """
    if not data or data[:2] != b"\xff\xd8":
        return False
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC1 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return False                       # progressive, lossless, ...
        if marker == 0xC0:
            seg = data[i + 4:i + 2 + length]
            if len(seg) < 6:
                return False
            height = int.from_bytes(seg[1:3], "big")
            width = int.from_bytes(seg[3:5], "big")
            count = seg[5]
            if width != height:
                return False
            if count == 1:
                return True                    # really is greyscale
            if count != 3 or len(seg) < 6 + 9:
                return False
            luma = seg[7]
            chroma = (seg[10], seg[13])
            return luma in (0x22, 0x21) and chroma == (0x11, 0x11)
        if marker == 0xDA:
            return False
        i += 2 + length
    return False


_YEAR = re.compile(r"(\d{4})")


def year_only(value):
    """``"20180201"`` or ``"2018-02-01"`` becomes ``"2018"``.

    Rockbox shows the date tag as the year verbatim. Anything without four
    digits in a row is returned as it was.
    """
    if value is None:
        return None
    m = _YEAR.search(str(value))
    return m.group(1) if m else value


_DATE = re.compile(r"^\s*(\d{4})(?:[-/.]?(\d{2}))?(?:[-/.]?(\d{2}))?")


def iso_date(value):
    """``"20180201"`` becomes ``"2018-02-01"``; ``"2018-02"`` stays.

    None when there is no month to add to the year: the year tag already
    says that much, and a release date that is only a year is not one.
    """
    m = _DATE.match(str(value or ""))
    if not m or not m.group(2) or m.group(2) == "00":
        return None
    parts = [m.group(1), m.group(2)]
    if m.group(3) and m.group(3) != "00":
        parts.append(m.group(3))
    return "-".join(parts)
