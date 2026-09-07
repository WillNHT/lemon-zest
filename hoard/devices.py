"""Device detection and registry.

Identity is the volume label, with a marker file as the tiebreaker.

A personal fleet does not need USB descriptors. The label is what the
operating system already hands us in an ordinary mount listing, on every
platform, with no native API. Its one weakness is that two cards can share a
name (FAT32 formats to "NO NAME" by default), so on first pair Hoard writes
a .hoard-id file holding a generated UUID at the device root. The label is
the everyday lookup; the marker file settles ties and survives a relabel.
"""
import ctypes
import json
import os
import time
import uuid

import psutil

from .paths import norm

MARKER = ".hoard-id"

# Profiles describe what a player will accept. Only the two the MVP supports
# are defined; the shape is ready for more.
PROFILES = {
    "ums": {
        "name": "Generic USB mass storage",
        "playlist_format": "m3u8",
        "playlist_dir": "Music",
        "music_dir": "Music",
        "encode_playlist_paths": False,
        "notes": "Folders and m3u8. No format ceiling enforced.",
    },
    "hiby": {
        "name": "HiBy OS DAP",
        "playlist_format": "m3u8",
        "playlist_dir": "Music",
        "music_dir": "Music",
        "encode_playlist_paths": True,
        "notes": "Reads m3u8 with percent-encoded relative paths from the music folder.",
    },
    "rockbox": {
        "name": "Rockbox",
        "playlist_format": "m3u8",
        "playlist_dir": "Playlists",
        "music_dir": "Music",
        "encode_playlist_paths": False,
        "notes": "Plain relative paths; playlists in their own folder.",
    },
}


def _windows_label(mountpoint):
    """Read a volume label on Windows without shelling out."""
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_unicode_buffer(261)
        fsbuf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(mountpoint), buf, 260,
            None, None, None, fsbuf, 260,
        )
        return buf.value or None if ok else None
    except Exception:
        return None


def _label_for(mountpoint):
    if os.name == "nt":
        return _windows_label(mountpoint)
    # On Linux and macOS the mount point's basename is the label.
    base = os.path.basename(mountpoint.rstrip("/"))
    return base or None


def list_volumes(removable_only=True):
    """Enumerate mounted volumes that could hold a music card."""
    out = []
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        opts = (part.opts or "").lower()
        removable = "removable" in opts or "cdrom" in opts
        if removable_only and not removable:
            continue
        try:
            usage = psutil.disk_usage(mp)
        except Exception:
            continue
        out.append({
            "mountpoint": norm(mp),
            "device": part.device,
            "fstype": part.fstype,
            "label": _label_for(mp),
            "total": usage.total,
            "free": usage.free,
            "removable": removable,
            "hoard_id": read_marker(mp),
        })
    return out


def read_marker(root):
    """Return the hoard id stored at a device root, or None."""
    path = os.path.join(root, MARKER)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("hoard_id")
    except Exception:
        return None


def write_marker(root, hoard_id, name=None):
    """Write the marker file that settles duplicate labels."""
    path = os.path.join(root, MARKER)
    payload = {"hoard_id": hoard_id, "name": name,
               "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return hoard_id


def register(con, root, name=None, profile="ums", label=None):
    """Pair a device. Idempotent: re-pairing the same card updates it."""
    root = norm(os.path.abspath(root))
    if not os.path.isdir(root):
        raise ValueError("device root does not exist: " + root)
    if profile not in PROFILES:
        raise ValueError("unknown profile: " + profile)
    spec = PROFILES[profile]

    hoard_id = read_marker(root) or str(uuid.uuid4())
    if label is None:
        label = _label_for(root)
    name = name or label or os.path.basename(root.rstrip("/")) or "Device"
    write_marker(root, hoard_id, name)

    now = time.time()
    con.execute(
        "INSERT INTO device(hoard_id, label, name, root, profile, music_dir, "
        "playlist_dir, created_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(hoard_id) DO UPDATE SET label=excluded.label, "
        "name=excluded.name, root=excluded.root, profile=excluded.profile, "
        "music_dir=excluded.music_dir, playlist_dir=excluded.playlist_dir, "
        "last_seen=excluded.last_seen",
        (hoard_id, label, name, root, profile, spec["music_dir"],
         spec["playlist_dir"], now, now),
    )
    con.commit()
    return con.execute("SELECT * FROM device WHERE hoard_id=?", (hoard_id,)).fetchone()


def find(con, ref):
    """Look a device up by name, label, hoard id or numeric id."""
    row = None
    if str(ref).isdigit():
        row = con.execute("SELECT * FROM device WHERE id=?", (int(ref),)).fetchone()
    if row is None:
        row = con.execute(
            "SELECT * FROM device WHERE hoard_id=? OR name=? OR label=? "
            "COLLATE NOCASE", (ref, ref, ref)
        ).fetchone()
    if row is None:
        rows = con.execute(
            "SELECT * FROM device WHERE name LIKE ? OR label LIKE ?",
            (f"%{ref}%", f"%{ref}%"),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            names = ", ".join(r["name"] for r in rows)
            raise ValueError(f"{ref!r} matches several devices: {names}")
    return row


def locate(con, device):
    """Find where a registered device is mounted right now.

    Checks the stored root first, then every volume for a matching marker,
    then falls back to the label. Returns a mount point or None.
    """
    root = device["root"]
    if root and os.path.isdir(root) and read_marker(root) == device["hoard_id"]:
        return root
    for vol in list_volumes(removable_only=False):
        if vol["hoard_id"] and vol["hoard_id"] == device["hoard_id"]:
            return vol["mountpoint"]
    if device["label"]:
        for vol in list_volumes(removable_only=False):
            if vol["label"] and vol["label"].casefold() == device["label"].casefold():
                return vol["mountpoint"]
    return None


def touch(con, device_id, root):
    con.execute("UPDATE device SET root=?, last_seen=? WHERE id=?",
                (norm(root), time.time(), device_id))
    con.commit()


def free_space(root):
    u = psutil.disk_usage(root)
    return {"total": u.total, "used": u.used, "free": u.free}
