"""Volume normalization, as ReplayGain tags.

The audio is never touched. ffmpeg's EBU R128 meter measures how loud a
track is, and the difference from the ReplayGain 2 reference (-18 LUFS) is
written into the file as ``replaygain_track_gain`` with the true peak beside
it. Rockbox reads those from MP4 freeform atoms, ID3 TXXX frames and Vorbis
comments alike and applies them on playback - its "replaygain type" setting
defaults to "track shuffle", so they take effect without touching the
player. Re-encoding to a fixed level would cost quality on lossy files and
could not be undone; a tag costs nothing and can be.
"""
import re
import subprocess

from .artwork import ffmpeg

REFERENCE = -18.0
# A near-silent file measures -70 LUFS and would ask for +52 dB. Nothing real
# needs more than this either way.
MAX_GAIN = 24.0

_I = re.compile(r"^\s*I:\s+(-?[\d.]+) LUFS", re.M)
_PEAK = re.compile(r"^\s*Peak:\s+(-?[\d.]+|-inf) dBFS", re.M)


def parse(text):
    """The ReplayGain fields from ebur128's summary, or None."""
    i, peak = _I.findall(text), _PEAK.findall(text)
    if not i or not peak:
        return None
    gain = max(-MAX_GAIN, min(MAX_GAIN, REFERENCE - float(i[-1])))
    lin = 0.0 if peak[-1] == "-inf" else 10 ** (float(peak[-1]) / 20)
    return {"replaygain_track_gain": "%+.2f dB" % gain,
            "replaygain_track_peak": "%.6f" % lin}


def measure(path, runner=None):
    """The ReplayGain fields for one file, or None if it cannot be measured."""
    exe = ffmpeg()
    if not exe:
        return None
    cmd = [exe, "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        res = (runner or subprocess.run)(cmd, capture_output=True, text=True,
                                         errors="replace", timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse(res.stderr) if res.returncode == 0 else None


def fill(con, progress=None, runner=None):
    """Tag every catalogued file that has no ReplayGain yet. Returns counts."""
    from . import tags

    rows = con.execute(
        "SELECT content_key, path FROM track WHERE size > 0 "
        "GROUP BY content_key ORDER BY rel_path").fetchall()
    counts = {"considered": 0, "written": 0, "had": 0, "failed": 0,
              "errors": []}
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows))
        if tags.has_replaygain(r["path"]):
            counts["had"] += 1
            continue
        counts["considered"] += 1
        fields = measure(r["path"], runner=runner)
        if not fields:
            counts["failed"] += 1
            if len(counts["errors"]) < 20:
                counts["errors"].append("could not measure " + r["path"])
            continue
        try:
            new_key = tags.write(r["path"], fields)
        except tags.TagWriteError as exc:
            counts["failed"] += 1
            counts["errors"].append(str(exc))
            continue
        tags.rekey(con, r["content_key"], r["path"], new_key)
        con.commit()
        counts["written"] += 1
    return counts
