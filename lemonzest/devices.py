"""Device detection and registry.

Identity is the volume label, with a marker file as the tiebreaker.

A personal fleet does not need USB descriptors. The label is what the
operating system already hands us in an ordinary mount listing, on every
platform, with no native API. Its one weakness is that two cards can share a
name (FAT32 formats to "NO NAME" by default), so on first pair Lemon Zest writes
a .lemon-zest-id file holding a generated UUID at the device root. The label
is the everyday lookup; the marker file settles ties and survives a relabel.

Cards paired before the rename carry a .hoard-id instead. That file is still
read, and the id inside it is kept, so a rename never re-pairs a card or
re-copies its contents.
"""
import ctypes
import json
import os
import time
import uuid

import psutil

from .paths import norm

MARKER = ".lemon-zest-id"
LEGACY_MARKER = ".hoard-id"

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
            "device_uid": read_marker(mp),
        })
    return out


def _read_one(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return payload.get("device_uid") or payload.get("hoard_id")


def read_marker(root):
    """Return the device id stored at a device root, or None.

    The current marker wins; the pre-rename one is the fallback, so a card
    paired under the old name is recognised untouched.
    """
    return (_read_one(os.path.join(root, MARKER))
            or _read_one(os.path.join(root, LEGACY_MARKER)))


def write_marker(root, device_uid, name=None):
    """Write the marker file that settles duplicate labels.

    Retires a pre-rename marker once the new one is safely in place, and
    only when it holds the same id - two markers disagreeing about which
    device this is would be worse than one stale file.
    """
    path = os.path.join(root, MARKER)
    payload = {"device_uid": device_uid, "name": name,
               "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    legacy = os.path.join(root, LEGACY_MARKER)
    if os.path.exists(legacy) and _read_one(legacy) == device_uid:
        try:
            os.remove(legacy)
        except OSError:
            pass
    return device_uid


def register(con, root, name=None, profile="ums", label=None):
    """Pair a device. Idempotent: re-pairing the same card updates it."""
    root = norm(os.path.abspath(root))
    if not os.path.isdir(root):
        raise ValueError("device root does not exist: " + root)
    if profile not in PROFILES:
        raise ValueError("unknown profile: " + profile)
    spec = PROFILES[profile]

    device_uid = read_marker(root) or str(uuid.uuid4())
    if label is None:
        label = _label_for(root)
    name = name or label or os.path.basename(root.rstrip("/")) or "Device"
    write_marker(root, device_uid, name)

    now = time.time()
    con.execute(
        "INSERT INTO device(device_uid, label, name, root, profile, music_dir, "
        "playlist_dir, created_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(device_uid) DO UPDATE SET label=excluded.label, "
        "name=excluded.name, root=excluded.root, profile=excluded.profile, "
        "music_dir=excluded.music_dir, playlist_dir=excluded.playlist_dir, "
        "last_seen=excluded.last_seen",
        (device_uid, label, name, root, profile, spec["music_dir"],
         spec["playlist_dir"], now, now),
    )
    con.commit()
    return con.execute("SELECT * FROM device WHERE device_uid=?", (device_uid,)).fetchone()


def find(con, ref):
    """Look a device up by name, label, device id or numeric id."""
    row = None
    if str(ref).isdigit():
        row = con.execute("SELECT * FROM device WHERE id=?", (int(ref),)).fetchone()
    if row is None:
        row = con.execute(
            "SELECT * FROM device WHERE device_uid=? OR name=? OR label=? "
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
    if root and os.path.isdir(root) and read_marker(root) == device["device_uid"]:
        return root
    for vol in list_volumes(removable_only=False):
        if vol["device_uid"] and vol["device_uid"] == device["device_uid"]:
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
