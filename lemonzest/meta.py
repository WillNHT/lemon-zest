"""Read tags and compute content keys.

Uses mutagen rather than an ffprobe subprocess per file: on a 2,300-file
library that is the difference between a scan measured in minutes and one
measured in seconds. ffmpeg is still the right tool for loudness analysis
and transcoding, neither of which the MVP does.
"""
import hashlib
import os
import re

from mutagen import File as MutagenFile

AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".aac", ".wav", ".alac", ".m4b"}

_CHUNK = 256 * 1024  # head and tail sampled for the content key

# YouTube's video categories. yt-dlp writes the category into the genre tag,
# so every download arrived with a genre of "Music" or "People & Blogs" -
# which is not a genre, and which stopped the real one ever being looked
# up, because a genre is only asked for when the file has none.
NOT_GENRES = {
    "music", "people & blogs", "entertainment", "film & animation",
    "gaming", "comedy", "education", "howto & style", "news & politics",
    "nonprofits & activism", "science & technology", "sports",
    "travel & events", "autos & vehicles", "pets & animals",
}


_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")


def source_id(url):
    """What a file is, whatever it has been renamed or re-tagged to since.

    ``youtube:<video id>``, read off the source URL yt-dlp writes into the
    ``purl`` tag. The tag travels with the file and no tag write of ours
    touches it, so it is the one identity a download keeps for life.
    """
    m = _YOUTUBE_ID.search(url or "")
    return "youtube:" + m.group(1) if m else None


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
        "date": None, "genre": None, "isrc": None, "purl": None, "codec": None,
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
    # The full release date lives in a tag of its own, because Rockbox shows
    # the year tag verbatim and "20180201" is not a year. See tags.py.
    out["date"] = g("----:com.apple.itunes:releasedate", "tdrl",
                    "releasedate")
    out["genre"] = g("\xa9gen", "tcon", "genre")
    if out["genre"] and out["genre"].strip().lower() in NOT_GENRES:
        out["genre"] = None
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

    from .artwork import iso_date, year_only
    # A file written before the split may still carry a full date in the
    # year tag; that is the date, and the year is its first four digits.
    out["date"] = iso_date(out["date"]) or iso_date(out["year"])
    if out["year"]:
        out["year"] = year_only(str(out["year"]))
    return out


def probe(path):
    """Full record for one file: stat, content key and tags."""
    st = os.stat(path)
    rec = {"size": st.st_size, "mtime": st.st_mtime,
           "ext": os.path.splitext(path)[1].lower(),
           "content_key": content_key(path, st.st_size)}
    rec.update(read_tags(path))
    rec["source_id"] = source_id(rec["purl"])
    return rec


def artwork(path):
    """The picture embedded in ``path``, as ``(bytes, mime)``, or None.

    Every container hides it somewhere different - an ID3 APIC frame, an
    MP4 ``covr`` atom, a FLAC picture block, a base64 Vorbis comment - and
    the interface only wants the first one it can show. Read on demand
    rather than cached in the catalog: artwork is between one and ten
    megabytes a file, and a library of two thousand of them is not a thing
    to keep in SQLite.
    """
    try:
        mf = MutagenFile(path)
    except Exception:
        return None
    if mf is None:
        return None

    # FLAC and anything else exposing pictures directly.
    for pic in (getattr(mf, "pictures", None) or []):
        data = getattr(pic, "data", None)
        if data:
            return data, getattr(pic, "mime", None) or "image/jpeg"

    tags = getattr(mf, "tags", None)
    if not tags:
        return None

    # MP4/M4A: the covr atom carries its format in a flag.
    try:
        covr = tags.get("covr")
    except Exception:
        covr = None
    if covr:
        first = covr[0]
        fmt = getattr(first, "imageformat", None)
        mime = "image/png" if fmt == 14 else "image/jpeg"
        return bytes(first), mime

    # ID3: any APIC frame, front cover preferred.
    try:
        apics = tags.getall("APIC")
    except Exception:
        apics = []
    if apics:
        front = next((a for a in apics if getattr(a, "type", 3) == 3), apics[0])
        if front.data:
            return front.data, front.mime or "image/jpeg"

    # Vorbis comments: a base64 FLAC picture block in a text field.
    try:
        blocks = tags.get("metadata_block_picture") or []
    except Exception:
        blocks = []
    for block in blocks:
        try:
            import base64

            from mutagen.flac import Picture

            pic = Picture(base64.b64decode(block))
        except Exception:
            continue
        if pic.data:
            return pic.data, pic.mime or "image/jpeg"
    return None
