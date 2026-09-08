"""Read tags and compute content keys.

Uses mutagen rather than an ffprobe subprocess per file: on a 2,300-file
library that is the difference between a scan measured in minutes and one
measured in seconds. ffmpeg is still the right tool for loudness analysis
and transcoding, neither of which the MVP does.
"""
import hashlib
import os

from mutagen import File as MutagenFile

AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".aac", ".wav", ".alac", ".m4b"}

_CHUNK = 256 * 1024  # head and tail sampled for the content key


def is_audio(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def content_key(path, size=None):
    """Cheap, stable fingerprint: size plus a digest of the head and tail.

    Full-file hashing of 20 GB on every scan is not worth it here. Size plus
    two 256 KB windows catches every realistic case (re-encode, re-tag,
    truncation) while keeping a rescan I/O-bound on metadata, not content.
    """
    if size is None:
        size = os.path.getsize(path)
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(_CHUNK))
        if size > _CHUNK * 2:
            fh.seek(-_CHUNK, os.SEEK_END)
            h.update(fh.read(_CHUNK))
    return f"{size}-{h.hexdigest()}"


def _first(tags, *keys):
    for k in keys:
        v = tags.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            v = v[0]
        # MP4 freeform atoms (ISRC among them) come back as raw bytes.
        if isinstance(v, (bytes, bytearray)):
            try:
                v = v.decode("utf-8", "replace")
            except Exception:
                continue
        v = str(v).strip().strip("\x00")
        if v:
            return v
    return None


def _num(value):
    """Parse '3', '3/12', (3, 12) into 3."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, tuple):
        value = value[0] if value else None
    s = str(value).strip()
    if not s:
        return None
    s = s.split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def read_tags(path):
    """Extract the fields the catalog stores. Never raises on a bad file."""
    out = {
        "duration": None, "title": None, "artist": None, "album": None,
        "album_artist": None, "track_no": None, "disc_no": None, "year": None,
        "genre": None, "isrc": None, "purl": None, "codec": None,
        "bitrate": None, "sample_rate": None,
    }
    try:
        mf = MutagenFile(path)
    except Exception:
        return out
    if mf is None:
        return out

    info = getattr(mf, "info", None)
    if info is not None:
        out["duration"] = getattr(info, "length", None)
        out["bitrate"] = getattr(info, "bitrate", None)
        out["sample_rate"] = getattr(info, "sample_rate", None)
        out["codec"] = type(info).__module__.rsplit(".", 1)[-1]

    tags = getattr(mf, "tags", None)
    if not tags:
        return out
    try:
        keys = {k.lower(): k for k in tags.keys()}
    except Exception:
        return out

    def g(*names):
        for n in names:
            real = keys.get(n.lower())
            if real is not None:
                return _first(tags, real)
        return None

    # MP4/M4A atoms, ID3 frames and Vorbis comments, in that order of preference.
    out["title"] = g("\xa9nam", "tit2", "title")
    out["artist"] = g("\xa9art", "tpe1", "artist")
    out["album"] = g("\xa9alb", "talb", "album")
    out["album_artist"] = g("aart", "tpe2", "albumartist", "album_artist", "album artist")
    out["year"] = g("\xa9day", "tdrc", "tyer", "date", "year")
    out["genre"] = g("\xa9gen", "tcon", "genre")
    out["isrc"] = g("tsrc", "isrc", "----:com.apple.itunes:isrc")
    # yt-dlp writes the source URL here; on MP3 it lands in a TXXX/WXXX frame.
    out["purl"] = g("purl", "txxx:purl", "wxxx:purl", "comment", "\xa9cmt")
    if out["purl"] and not str(out["purl"]).startswith("http"):
        out["purl"] = None

    for dest, names in (("track_no", ("trkn", "trck", "tracknumber", "track")),
                        ("disc_no", ("disk", "tpos", "discnumber", "disc"))):
        for n in names:
            real = keys.get(n.lower())
            if real is None:
                continue
            try:
                out[dest] = _num(tags[real])
            except Exception:
                out[dest] = None
            if out[dest] is not None:
                break

    if out["year"]:
        out["year"] = str(out["year"])[:10]
    return out


def probe(path):
    """Full record for one file: stat, content key and tags."""
    st = os.stat(path)
    rec = {"size": st.st_size, "mtime": st.st_mtime,
           "ext": os.path.splitext(path)[1].lower(),
           "content_key": content_key(path, st.st_size)}
    rec.update(read_tags(path))
    return rec
