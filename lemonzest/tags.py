"""Write tags back into audio files.

Deliberately separate from ``meta.py``, which promises to be read-only, and
from ``enrich.py``, which only ever touches the database. Applying an
enrichment to the catalog is cheap and reversible; writing it into the file
is neither, so it is its own module, its own flag and its own confirmation.

It has to happen at all, though. A catalog-only correction makes the library
right inside Lemon Zest and leaves every other player - the HiBy, anything
that reads the card directly - still showing "Nemu".

Two hazards this module exists to contain:

  * **A half-written tag is a broken file.** mutagen saves in place, and an
    MP4 atom rewrite that is interrupted leaves a file no decoder will open.
    Every write goes to a copy, is read back, and only then replaces the
    original - the same temp-and-rename discipline the sync executor uses on
    the card.
  * **Writing tags changes the content key.** ``meta.content_key`` digests
    the head of the file, and tags live at the head. Without recomputing it,
    the next scan sees every file it just corrected as a new one, and every
    device manifest entry for it stops matching - which would recopy the
    whole library to the card.
"""
import os
import shutil
import tempfile

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, TALB, TCON, TDRC, TDRL, TIT2, TPE1, TPE2,
                         TPOS, TRCK, TSRC, USLT)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import artwork
from .meta import content_key, read_tags
from .paths import resolve_existing

# One field, three spellings. MP4 uses four-character atoms, ID3 uses frame
# classes, Vorbis comments use plain names - and none of them agree.
_MP4 = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
    "album_artist": "aART", "year": "\xa9day", "genre": "\xa9gen",
    "lyrics": "\xa9lyr",
}
# The full release date has a tag of its own in every format, because the
# year tag is what Rockbox shows as the year and "2018-02-01" is not one.
# The same names MusicBrainz Picard writes: TDRL, RELEASEDATE, and an iTunes
# freeform atom.
MP4_DATE = "----:com.apple.iTunes:RELEASEDATE"
_ID3 = {
    "title": TIT2, "artist": TPE1, "album": TALB,
    "album_artist": TPE2, "year": TDRC, "date": TDRL, "genre": TCON,
}
_VORBIS = {
    "title": "title", "artist": "artist", "album": "album",
    "album_artist": "albumartist", "year": "date", "genre": "genre",
    "isrc": "isrc", "track_no": "tracknumber", "disc_no": "discnumber",
    "date": "releasedate", "lyrics": "lyrics",
}


class TagWriteError(RuntimeError):
    """The file was not modified. The original is untouched."""


def _write_mp4(audio, fields, cover):
    for field, atom in _MP4.items():
        if fields.get(field) is not None:
            audio[atom] = [str(fields[field])]
    if fields.get("date") is not None:
        audio[MP4_DATE] = [str(fields["date"]).encode("utf-8")]
    if fields.get("isrc") is not None:
        # ISRC has no standard atom; iTunes stores it as a freeform one, and
        # meta.py already reads that spelling back.
        audio["----:com.apple.iTunes:ISRC"] = [
            str(fields["isrc"]).encode("utf-8")]
    if fields.get("track_no") is not None:
        audio["trkn"] = [(int(fields["track_no"]), 0)]
    if fields.get("disc_no") is not None:
        audio["disk"] = [(int(fields["disc_no"]), 0)]
    if cover:
        data, mime = cover
        fmt = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(data, imageformat=fmt)]


def _write_id3(audio, fields, cover):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for field, frame in _ID3.items():
        if fields.get(field) is not None:
            tags.setall(frame.__name__, [frame(encoding=3,
                                               text=[str(fields[field])])])
    if fields.get("isrc") is not None:
        tags.setall("TSRC", [TSRC(encoding=3, text=[str(fields["isrc"])])])
    if fields.get("track_no") is not None:
        tags.setall("TRCK", [TRCK(encoding=3, text=[str(fields["track_no"])])])
    if fields.get("disc_no") is not None:
        tags.setall("TPOS", [TPOS(encoding=3, text=[str(fields["disc_no"])])])
    if fields.get("lyrics") is not None:
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="",
                      text=str(fields["lyrics"])))
    if cover:
        data, mime = cover
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))


def _write_vorbis(audio, fields, cover):
    for field, name in _VORBIS.items():
        if fields.get(field) is not None:
            audio[name] = [str(fields[field])]
    if cover and isinstance(audio, FLAC):
        data, mime = cover
        pic = Picture()
        pic.data, pic.type, pic.mime = data, 3, mime
        audio.clear_pictures()
        audio.add_picture(pic)


def write(path, fields, cover=None):
    """Write ``fields`` into the file at ``path``. Returns the new content key.

    ``cover`` is an optional ``(bytes, mime)`` pair, as ``enrich.fetch_cover``
    returns it. Only fields present in the mapping are touched; everything
    else in the file is left exactly as it was.

    The original is replaced only after the rewritten copy has been reopened
    and its tags read back, so a crash or a full disk leaves the file it
    started with rather than a truncated one.
    """
    fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    if not fields and not cover:
        return None
    # A full date reads as the year on Rockbox ("20180201"), and a padded or
    # 4:4:4 cover draws letterboxed and grey there. Fix both on the way in.
    if "date" in fields:
        fields["date"] = artwork.iso_date(fields["date"])
        if not fields["date"]:
            del fields["date"]
        elif "year" not in fields:
            fields["year"] = fields["date"]
    if "year" in fields:
        fields["year"] = artwork.year_only(fields["year"])
    if cover:
        cover = artwork.normalise(*cover)
    # The catalog stores NFC; NTFS stores whatever bytes wrote the file, and
    # a yt-dlp download can be NFD. Same name on screen, different bytes to
    # open(), so the stored path opens nothing and the write fails with "no
    # such file" over a file that is plainly sitting there. Resolve to the
    # spelling the filesystem actually has before touching anything.
    real = resolve_existing(path)
    if not real or not os.path.isfile(real):
        raise TagWriteError(f"no such file: {path}")
    path = real

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".lz-tag-",
                               suffix=os.path.splitext(path)[1])
    os.close(fd)
    try:
        shutil.copy2(path, tmp)
        audio = MutagenFile(tmp)
        if audio is None:
            raise TagWriteError(f"unreadable audio: {path}")
        if isinstance(audio, MP4):
            _write_mp4(audio, fields, cover)
        elif isinstance(audio, MP3):
            _write_id3(audio, fields, cover)
        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            _write_vorbis(audio, fields, cover)
        else:
            # Anything else gets the Vorbis spelling if it takes a mapping at
            # all, and is refused rather than silently skipped if it does not.
            try:
                _write_vorbis(audio, fields, None)
            except Exception as exc:
                raise TagWriteError(
                    f"unsupported container for tag writing: {path}") from exc
        audio.save()

        # Read it back before it becomes the original. A file that will not
        # reopen here is one that would not have played on the device.
        check = MutagenFile(tmp)
        if check is None or check.tags is None:
            raise TagWriteError(f"rewritten file did not reopen: {path}")

        os.replace(tmp, path)
    except TagWriteError:
        _discard(tmp)
        raise
    except Exception as exc:
        _discard(tmp)
        raise TagWriteError(f"{type(exc).__name__}: {exc}") from exc
    return content_key(path)


def _discard(tmp):
    try:
        os.remove(tmp)
    except OSError:
        pass


def rekey(con, old_key, path, new_key):
    """Point every row that referenced the old content key at the new one.

    Called after a successful write. Three tables key off the content key and
    all three have to move together:

      * ``track``, or the next scan treats a corrected file as a new one;
      * ``enrichment`` and ``track_override``, or the enrichment that was
        just applied becomes an orphan and the file looks untouched - so it
        would be looked up again on the next run, and a hand-typed
        correction would stop outranking the source;
      * ``device_manifest``, which stores the *source* file's key so that
        replugging a correct card is a no-op. Leaving it stale would make the
        next sync recopy every file whose tags were just fixed.

    Two files sharing a key after the write would be two byte-identical
    files, which is not a state a tag write can create; the guard is there so
    that if it ever happens the write is still reported as done.
    """
    if not new_key or new_key == old_key:
        return
    con.execute("UPDATE track SET content_key=? WHERE content_key=? AND path=?",
                (new_key, old_key, path))
    con.execute(
        "UPDATE OR IGNORE enrichment SET content_key=?, applied_key=? "
        "WHERE content_key=?", (new_key, new_key, old_key))
    con.execute(
        "UPDATE OR IGNORE track_override SET content_key=? WHERE content_key=?",
        (new_key, old_key))
    con.execute("UPDATE device_manifest SET content_key=? WHERE content_key=?",
                (new_key, old_key))


def read_cover(path):
    """The first embedded picture as ``(bytes, mime)``, or None."""
    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return None
    if isinstance(audio, MP4):
        pics = audio.tags.get("covr") or []
        if not pics:
            return None
        pic = pics[0]
        png = getattr(pic, "imageformat", None) == MP4Cover.FORMAT_PNG
        return bytes(pic), "image/png" if png else "image/jpeg"
    if isinstance(audio, FLAC):
        return ((audio.pictures[0].data, audio.pictures[0].mime)
                if audio.pictures else None)
    apic = audio.tags.getall("APIC") if hasattr(audio.tags, "getall") else []
    return (apic[0].data, apic[0].mime) if apic else None


def has_lyrics(path):
    """Whether the file already carries lyrics, in any format's tag."""
    real = resolve_existing(path)
    try:
        audio = MutagenFile(real) if real else None
    except Exception:      # noqa: BLE001 - unreadable is "no"
        return False
    tags = getattr(audio, "tags", None)
    if tags is None:
        return False
    if hasattr(tags, "getall"):
        return bool(tags.getall("USLT") or tags.getall("SYLT"))
    return bool(_raw_tag(audio, ("\xa9lyr", "lyrics", "unsyncedlyrics")))


def _raw_tag(audio, keys):
    """The first of ``keys`` the file carries, as text, or None."""
    tags = getattr(audio, "tags", None)
    for key in keys:
        try:
            got = tags.get(key) if tags is not None else None
        except Exception:      # noqa: BLE001 - a tag we cannot read is none
            got = None
        if got:
            got = got[0] if isinstance(got, list) else got
            if isinstance(got, bytes):
                got = got.decode("utf-8", "replace")
            if hasattr(got, "text"):      # an ID3 frame
                got = got.text[0] if got.text else ""
            return str(got)
    return None


def fix_for_players(con, row):
    """Trim the year and square the cover of one catalogued file.

    ``row`` needs ``path``, ``content_key`` and ``year``. Returns what was
    changed, as a list of words; an empty list means nothing needed doing.
    """
    real = resolve_existing(row["path"])
    if not real:
        raise TagWriteError(f"no such file: {row['path']}")
    fields, changed = {}, []
    audio = MutagenFile(real)
    # The tag as written, not as read_tags reports it: that one is already
    # trimmed to the year, which is the very thing being checked for.
    current = _raw_tag(audio, ("\xa9day", "TDRC", "date"))
    if current and artwork.year_only(current) != current:
        fields["year"] = current
        # The date the year tag held is kept, in the tag meant for it.
        if not _raw_tag(audio, (MP4_DATE, "TDRL", "releasedate")):
            fields["date"] = current
        changed.append("year")
    cover = read_cover(real)
    if cover and not artwork.is_player_safe(cover[0]):
        changed.append("cover")
    else:
        cover = None
    if not changed:
        return []
    new_key = write(real, fields, cover=cover)
    if new_key:
        rekey(con, row["content_key"], row["path"], new_key)
        if "year" in fields:
            con.execute("UPDATE track SET year=?, date=COALESCE(?, date) "
                        "WHERE path=?",
                        (artwork.year_only(current),
                         artwork.iso_date(current), row["path"]))
        con.commit()
    return changed

