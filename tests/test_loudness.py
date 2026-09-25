import shutil
import subprocess

import pytest

from lemonzest import loudness, tags
from lemonzest.artwork import ffmpeg

SUMMARY = """
[Parsed_ebur128_0 @ 0000] Summary:

  Integrated loudness:
    I:          -8.0 LUFS
    Threshold: -18.0 LUFS

  True peak:
    Peak:       -0.5 dBFS
"""


def test_parse_summary():
    got = loudness.parse(SUMMARY)
    assert got == {"replaygain_track_gain": "-10.00 dB",
                   "replaygain_track_peak": "0.944061"}


def test_parse_clamps_silence_and_rejects_junk():
    quiet = SUMMARY.replace("-8.0 LUFS", "-70.0 LUFS").replace(
        "-0.5 dBFS", "-inf dBFS")
    assert loudness.parse(quiet) == {"replaygain_track_gain": "+24.00 dB",
                                     "replaygain_track_peak": "0.000000"}
    assert loudness.parse("no summary here") is None


@pytest.mark.skipif(not ffmpeg(), reason="needs ffmpeg")
@pytest.mark.parametrize("ext", ["m4a", "mp3", "opus", "flac"])
def test_measure_and_tag_every_container(tmp_path, ext):
    path = str(tmp_path / ("t." + ext))
    subprocess.run([ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=f=440:d=3", path], check=True)
    assert not tags.has_replaygain(path)
    fields = loudness.measure(path)
    assert fields and fields["replaygain_track_gain"].endswith(" dB")
    tags.write(path, fields)
    assert tags.has_replaygain(path)
