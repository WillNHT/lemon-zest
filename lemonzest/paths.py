"""Path normalisation, filesystem-safe naming, and destination templating.

Two hazards this module exists to contain, both observed in the real library:

  * Unicode normalisation. Vietnamese filenames round-trip as NFC on one
    filesystem and NFD on another, so a literal string compare reports a
    missing file that is plainly there. Every path Lemon Zest stores or compares
    goes through ``norm`` first.
  * FAT32/exFAT naming. The card rejects characters Windows and Linux allow,
    dislikes trailing dots and spaces, and is case-insensitive, so two tracks
    that differ only in case collide.
"""
import os
import re
import unicodedata

# Illegal on FAT32/exFAT (and on NTFS), plus control characters.
#
# The backslash is in here as a character in its own right, which it was not
# before: the class used to read [...\|...], where the backslash was only
# escaping the pipe. On Windows a backslash separates path components, so a
# tag reading "AC\DC" split one folder into two - exactly the defect already
# fixed for the forward slash, hiding behind an escape.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# What to put in their place. The full-width forms are ordinary letters as
# far as every filesystem is concerned - FAT32 and exFAT store names as
# UTF-16 - and they are what the title actually said, where an underscore is
# only a record that something was removed. It is also what yt-dlp does, so
# a downloaded file and a file named from its tags agree instead of
# differing by one character.
_SUBSTITUTE = {
    "<": "＜", ">": "＞", ":": "：", '"': "＂",
    "/": "⧸", "\\": "⧵", "|": "｜", "?": "？", "*": "＊",
}
# Names Windows refuses outright, whatever the extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_COMPONENT = 120  # leaves room for a collision suffix inside FAT's 255


def norm(p):
    """Normalise a path for storage and comparison: NFC, forward slashes."""
    if p is None:
        return None
    return unicodedata.normalize("NFC", str(p).replace("\\", "/"))


def resolve_existing(path):
    """Return the on-disk spelling of ``path``, trying NFC and NFD.

    Returns None when no form of it exists.

    The catalog stores NFC, because a path has to have one spelling to be
    comparable. The disk does not agree: NTFS keeps whatever bytes the writer
    used, so a yt-dlp download can land as NFD - "\u304b" plus a combining
    dakuten where the catalog holds "\u304c" - and the stored path then opens
    nothing at all. Same characters to a reader, different bytes to open().

    Whole-path NFC and NFD are tried first because they are one stat each and
    they cover the ordinary case. When they fail, the walk below handles the
    case they cannot: a path whose folders are in one normalisation and whose
    filename is in the other, which is what a folder made by hand containing
    a downloaded file actually looks like.
    """
    if not path:
        return None
    cands = [path,
             unicodedata.normalize("NFC", path),
             unicodedata.normalize("NFD", path)]
    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        if os.path.exists(c):
            return c
    return _resolve_component_wise(path)


def _resolve_component_wise(path):
    """Walk the path, matching each component against what is on disk.

    One directory listing per level, and only ever reached after the cheap
    whole-path attempts have failed, so the cost lands on the paths that are
    actually broken rather than on every lookup.
    """
    path = str(path).replace("\\", "/")
    head, _, tail = path.rpartition("/")
    if not head or not tail:
        return None
    base = resolve_existing(head) if not os.path.isdir(head) else head
    if not base or not os.path.isdir(base):
        return None
    want = unicodedata.normalize("NFC", tail)
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        if unicodedata.normalize("NFC", name) == want:
            found = os.path.join(base, name)
            return found.replace("\\", "/")
    return None


def safe_component(name, max_len=MAX_COMPONENT):
    """Make one path component safe for FAT32/exFAT."""
    name = unicodedata.normalize("NFC", str(name))
    # A control character carries nothing and is dropped; everything else
    # illegal has a full-width twin that reads the same.
    name = _ILLEGAL.sub(lambda m: _SUBSTITUTE.get(m.group(0), ""), name)
    name = name.replace("\t", " ").strip()
    # FAT stores no trailing dot or space; Windows silently drops them.
    name = name.rstrip(". ")
    if not name:
        name = "_"
    stem, dot, ext = name.rpartition(".")
    if dot and stem.upper() in _RESERVED:
        name = stem + "_" + dot + ext
    elif not dot and name.upper() in _RESERVED:
        name = name + "_"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: max_len - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:max_len].rstrip(". ")
    return name


def safe_relpath(rel):
    """Sanitise every component of a relative path."""
    parts = [p for p in norm(rel).split("/") if p not in ("", ".", "..")]
    return "/".join(safe_component(p) for p in parts)


class _Fmt(dict):
    """Formatting map that yields a placeholder rather than raising."""

    def __missing__(self, key):
        return "Unknown"


def _as_component(value, fallback="Unknown"):
    """Sanitise one substituted value *before* it is joined into a path.

    This has to happen per value, not on the finished path: an artist called
    "AC/DC" would otherwise turn a single component into two, and the track
    would land in AC/DC/... instead of AC_DC/... - scattering files into a
    folder tree nobody asked for. The template's own slashes are what
    separate components; a slash arriving from tag data is just a character.
    """
    text = ("" if value is None else str(value)).strip()
    return safe_component(text) if text else fallback


def render_template(template, track, ext=None):
    """Build a destination relative path for one track row.

    ``track`` is any mapping with the catalog's column names. Missing values
    become 'Unknown' rather than blowing up mid-sync.
    """
    keys = track.keys()

    def field(name):
        return track[name] if name in keys else None

    # "{rel_path}" mirrors the library's own folder layout onto the device.
    # Useful when the card was populated from that library already: the
    # destinations then match what is there, and nothing is recopied.
    if template.strip() == "{rel_path}":
        rel = field("rel_path")
        if rel:
            return safe_relpath(rel)

    tno, dno = field("track_no"), field("disc_no")
    values = _Fmt(
        artist=_as_component(field("artist")),
        album_artist=_as_component(field("album_artist") or field("artist")),
        album=_as_component(field("album"), "Unknown Album"),
        title=_as_component(
            field("title"),
            safe_component(os.path.splitext(os.path.basename(track["path"]))[0])),
        track=int(tno) if tno else 0,
        disc=int(dno) if dno else 0,
        year=_as_component(field("year"), ""),
        genre=_as_component(field("genre"), ""),
        ext=ext if ext is not None else os.path.splitext(track["path"])[1],
        # The file's current name, kept verbatim. It exists so that a
        # template can correct the folders without touching a filename it has
        # no business parsing - see organise.DEFAULT_TEMPLATE. Substituted as
        # a marker rather than the name itself, because the name is already
        # a valid path component and running it through the sanitiser again
        # would mangle characters it is entitled to contain.
        filename="__FILENAME__",
    )
    try:
        rendered = template.format_map(values)
    except (ValueError, KeyError, IndexError):
        # A malformed template must not take the whole sync down.
        rendered = "{album_artist}/{album}/{title}{ext}".format_map(values)
    return safe_relpath(rendered)


def dedupe(rel, taken):
    """Resolve a case-insensitive collision by suffixing the stem.

    ``taken`` is a set of already-claimed casefolded paths; it is updated.
    """
    key = rel.casefold()
    if key not in taken:
        taken.add(key)
        return rel
    head, _, tail = rel.rpartition("/")
    stem, dot, ext = tail.rpartition(".")
    if not dot:
        stem, ext = tail, ""
    for n in range(2, 1000):
        cand_tail = f"{stem} ({n}){'.' + ext if ext else ''}"
        cand = f"{head}/{cand_tail}" if head else cand_tail
        if cand.casefold() not in taken:
            taken.add(cand.casefold())
            return cand
    raise RuntimeError(f"cannot deduplicate {rel!r}")
