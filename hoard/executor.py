"""The executor: carry out a plan, safely and resumably.

Rules this module keeps:

  * Nothing is written outside the device root. Every destination is checked
    against it before a byte moves.
  * Every copy lands on a temporary name, is flushed and fsynced, then
    renamed into place. An interrupted sync therefore never leaves a
    half-written file where the player will index it.
  * The manifest row is written only after the rename succeeds, so a crash
    leaves the file to be recopied rather than recorded as done.
  * Playlists are always rewritten, never appended. This is the fix for the
    duplication that had grown a 101-track playlist to 1,817 lines.
  * Deletions are checked against the plan again at execution time.
"""
import os
import time

from . import playlists as pl_mod
from .db import log
from .devices import PROFILES, free_space
from .paths import norm

COPY_CHUNK = 4 * 1024 * 1024


class Aborted(Exception):
    """Raised when the device disappears mid-sync."""


def _inside(root, path):
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    return os.path.commonpath([root_abs, path_abs]) == root_abs


def _copy_one(src, dst, expect_size=None):
    """Copy with a temp name and an atomic rename. Returns bytes written."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".hoard-tmp"
    written = 0
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            while True:
                chunk = fsrc.read(COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                written += len(chunk)
            fdst.flush()
            os.fsync(fdst.fileno())
        if expect_size is not None and written != expect_size:
            raise IOError(f"short copy: {written} of {expect_size} bytes")
        os.replace(tmp, dst)
    except BaseException:
        # Leave no partial file behind for the player to find.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return written


def execute(con, plan, prune=False, on_event=None, dry_run=False):
    """Run a plan. Returns a summary dict.

    ``on_event(kind, detail, done, total)`` is called as work proceeds so a
    caller can draw a progress bar; the executor itself prints nothing.
    """
    device = plan["device"]
    root = plan["root"]
    music_dir = plan["music_dir"]
    music_root = os.path.join(root, music_dir) if music_dir else root
    profile = PROFILES.get(device["profile"], PROFILES["ums"])

    def emit(kind, detail, done=0, total=0, size=None):
        if on_event:
            on_event(kind, detail, done, total)

    if dry_run:
        raise ValueError("execute() does not do dry runs; use planner.plan()")

    if not os.path.isdir(root):
        raise Aborted(f"device root is not mounted: {root}")

    summary = {"copied": 0, "bytes": 0, "deleted": 0, "playlists": 0,
               "failed": 0, "skipped_unchanged": len(plan["unchanged"]),
               "errors": []}
    log(con, device["id"], "start",
        f"{len(plan['copies'])} to copy, {len(plan['deletes'])} to remove")
    con.commit()

    # ---- copies --------------------------------------------------------
    total = len(plan["copies"])
    for i, c in enumerate(plan["copies"], 1):
        dst = os.path.join(music_root, c["rel"].replace("/", os.sep))
        if not _inside(root, dst):
            summary["errors"].append(f"refused path outside device: {c['rel']}")
            summary["failed"] += 1
            continue
        if not os.path.isdir(root):
            raise Aborted("device disappeared during sync")
        try:
            written = _copy_one(c["src"], dst, expect_size=c["size"])
        except Aborted:
            raise
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{c['rel']}: {exc}")
            log(con, device["id"], "error", f"{c['rel']}: {exc}")
            con.commit()
            emit("error", c["rel"], i, total)
            continue

        con.execute(
            "INSERT INTO device_manifest(device_id, dest_rel, track_id, "
            "content_key, size, written_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(device_id, dest_rel) DO UPDATE SET "
            "track_id=excluded.track_id, content_key=excluded.content_key, "
            "size=excluded.size, written_at=excluded.written_at",
            (device["id"], c["rel"], c["track"]["id"], c["track"]["content_key"],
             written, time.time()),
        )
        log(con, device["id"], "copy", c["rel"], written)
        con.commit()
        summary["copied"] += 1
        summary["bytes"] += written
        emit("copy", c["rel"], i, total)

    # ---- deletions -----------------------------------------------------
    for d in plan["deletes"]:
        dst = os.path.join(music_root, d["rel"].replace("/", os.sep))
        if not _inside(root, dst):
            summary["errors"].append(f"refused delete outside device: {d['rel']}")
            continue
        if d["reason"].endswith("(prune)") and not prune:
            continue
        try:
            if os.path.exists(dst):
                os.remove(dst)
            con.execute(
                "DELETE FROM device_manifest WHERE device_id=? AND dest_rel=?",
                (device["id"], d["rel"]),
            )
            log(con, device["id"], "delete", d["rel"], d["size"])
            con.commit()
            summary["deleted"] += 1
            emit("delete", d["rel"])
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"delete {d['rel']}: {exc}")

    _prune_empty_dirs(music_root)

    # ---- playlists -----------------------------------------------------
    pl_dir_rel = device["playlist_dir"].strip("/")
    pl_root = os.path.join(root, pl_dir_rel) if pl_dir_rel else root
    # Remove playlist files this plan no longer includes, before writing
    # the current ones - an unticked playlist should not linger on the card.
    for pd in plan.get("playlist_deletes", []):
        stale = os.path.join(pl_root, pd["filename"])
        if not _inside(root, stale):
            continue
        try:
            if os.path.exists(stale):
                os.remove(stale)
            con.execute(
                "DELETE FROM device_playlist WHERE device_id=? AND filename=?",
                (device["id"], pd["filename"]),
            )
            log(con, device["id"], "delete", "playlist " + pd["filename"])
            con.commit()
            summary["deleted"] += 1
            emit("delete", pd["filename"])
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append("playlist " + pd["filename"] + ": " + str(exc))

    for p in plan["playlists"]:
        fname = p.get("filename") or pl_mod.safe_filename(p["name"])
        dst = os.path.join(pl_root, fname)
        if not _inside(root, dst):
            summary["errors"].append(f"refused playlist outside device: {fname}")
            continue
        # Track paths are relative to the music folder; the playlist may sit
        # elsewhere, so re-base them against the playlist's own folder.
        rows = []
        for rel, title, duration, uri in p["entries"]:
            target = os.path.join(music_root, rel.replace("/", os.sep))
            rebased = norm(os.path.relpath(target, pl_root))
            rows.append((rebased, title, duration, uri))
        try:
            n = pl_mod.write(dst, p["name"], rows, source_uri=p["source_uri"],
                             encode_paths=profile["encode_playlist_paths"])
            con.execute(
                "INSERT INTO device_playlist(device_id, filename, name, "
                "entries, written_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(device_id, filename) DO UPDATE SET "
                "name=excluded.name, entries=excluded.entries, "
                "written_at=excluded.written_at",
                (device["id"], fname, p["name"], n, time.time()),
            )
            log(con, device["id"], "playlist", f"{p['name']} ({n} entries)")
            con.commit()
            summary["playlists"] += 1
            emit("playlist", f"{p['name']} - {n} entries")
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"playlist {p['name']}: {exc}")

    con.execute("UPDATE device SET last_sync=?, root=? WHERE id=?",
                (time.time(), norm(root), device["id"]))
    log(con, device["id"], "done",
        f"{summary['copied']} copied, {summary['deleted']} removed, "
        f"{summary['playlists']} playlists")
    con.commit()

    try:
        summary["free_after"] = free_space(root)["free"]
    except Exception:
        summary["free_after"] = None
    return summary


def _prune_empty_dirs(root):
    """Remove directories left empty by deletions. Never removes root."""
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
