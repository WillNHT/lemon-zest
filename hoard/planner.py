"""The planner: turn intent into a list of actions.

Every sync is a diff over three collections. Keeping them distinct is what
makes a replug a no-op instead of a re-copy.

  desired   what the user ticked, from device_set. Intent only, no paths.
  planned   desired run through the device profile: destination paths,
            playlist files, bytes required.
  observed  what is physically on the card, from device_manifest confirmed
            against the filesystem.

planned minus observed is the work; observed minus planned is the deletions.
The manifest stores the *source* content key, so a card that is already
correct produces an empty plan.

The planner touches nothing. It is pure computation over the catalog and a
directory listing, which makes it both the dry run and the thing worth
testing first.
"""
import os

from .paths import dedupe, norm, render_template, resolve_existing
from .playlists import safe_filename as pl_filename


def desired_tracks(con, device_id):
    """Resolve the device's set rules into an ordered list of track rows.

    Rules stack: a track ticked by any rule is in. Playlist membership is
    tracked separately so the writer knows which playlists to emit.
    """
    rules = con.execute(
        "SELECT kind, ref FROM device_set WHERE device_id=? ORDER BY kind, ref",
        (device_id,),
    ).fetchall()

    ids = set()
    playlists = []
    for r in rules:
        kind, ref = r["kind"], r["ref"]
        if kind == "playlist":
            row = con.execute("SELECT id, name FROM playlist WHERE name=?",
                              (ref,)).fetchone()
            if not row:
                continue
            playlists.append(row["name"])
            for e in con.execute(
                "SELECT track_id FROM playlist_entry WHERE playlist_id=? "
                "AND track_id IS NOT NULL", (row["id"],)
            ):
                ids.add(e["track_id"])
        elif kind == "artist":
            for e in con.execute(
                "SELECT id FROM track WHERE artist=? COLLATE NOCASE "
                "OR album_artist=? COLLATE NOCASE", (ref, ref)
            ):
                ids.add(e["id"])
        elif kind == "album":
            for e in con.execute(
                "SELECT id FROM track WHERE album=? COLLATE NOCASE", (ref,)
            ):
                ids.add(e["id"])
        elif kind == "track":
            row = con.execute("SELECT id FROM track WHERE path=?", (ref,)).fetchone()
            if row:
                ids.add(row["id"])

    if not ids:
        return [], playlists
    qmarks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT * FROM track WHERE id IN ({qmarks}) "
        "ORDER BY album_artist, album, disc_no, track_no, title",
        list(ids),
    ).fetchall()
    return rows, playlists


def plan(con, device, root, prune=False):
    """Compute the full action list for one device.

    ``root`` is where the device is mounted right now. ``prune`` widens
    deletion from "files Hoard put there" to "anything under the music
    folder that is not planned" - off by default, because deleting files a
    user placed by hand is not Hoard's call.
    """
    tracks, playlist_names = desired_tracks(con, device["id"])
    music_dir = device["music_dir"].strip("/")
    template = device["path_template"]

    # ---- planned -------------------------------------------------------
    taken = set()
    planned = {}          # dest_rel (below music_dir) -> track row
    plan_by_track = {}    # track id -> dest_rel
    for t in tracks:
        rel = render_template(template, t)
        rel = dedupe(rel, taken)
        planned[rel] = t
        plan_by_track[t["id"]] = rel

    # ---- observed ------------------------------------------------------
    manifest = {
        r["dest_rel"]: r
        for r in con.execute(
            "SELECT * FROM device_manifest WHERE device_id=?", (device["id"],)
        )
    }
    music_root = os.path.join(root, music_dir) if music_dir else root

    on_card = {}
    if os.path.isdir(music_root):
        for dirpath, dirnames, filenames in os.walk(music_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or fn.endswith(".hoard-tmp"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = norm(os.path.relpath(full, music_root))
                try:
                    on_card[rel] = os.path.getsize(full)
                except OSError:
                    continue

    audio_on_card = {k: v for k, v in on_card.items()
                     if not k.lower().endswith((".m3u", ".m3u8"))}

    # ---- diff ----------------------------------------------------------
    copies, unchanged, missing_source = [], [], []
    for rel, t in planned.items():
        src = resolve_existing(t["path"])
        if src is None:
            missing_source.append({"rel": rel, "track": t})
            continue
        m = manifest.get(rel)
        present = rel in on_card
        if m and present and m["content_key"] == t["content_key"] \
                and on_card[rel] == m["size"]:
            unchanged.append(rel)
            continue
        if not present:
            reason = "new" if not m else "gone from card"
        elif not m:
            reason = "untracked"
        elif m["content_key"] != t["content_key"]:
            reason = "source changed"
        else:
            reason = "size mismatch"
        copies.append({"rel": rel, "track": t, "src": src,
                       "size": t["size"], "reason": reason})

    deletes = []
    for rel in manifest:
        if rel not in planned:
            deletes.append({"rel": rel, "reason": "removed from set",
                            "size": on_card.get(rel, 0),
                            "present": rel in on_card})
    if prune:
        known = set(planned) | set(manifest)
        for rel, size in audio_on_card.items():
            if rel not in known:
                deletes.append({"rel": rel, "reason": "not in set (prune)",
                                "size": size, "present": True})

    # ---- playlists -----------------------------------------------------
    playlist_plan = []
    for name in playlist_names:
        row = con.execute("SELECT * FROM playlist WHERE name=?", (name,)).fetchone()
        if not row:
            continue
        entries, skipped = [], 0
        for e in con.execute(
            "SELECT e.*, t.title, t.artist FROM playlist_entry e "
            "LEFT JOIN track t ON t.id = e.track_id "
            "WHERE e.playlist_id=? ORDER BY e.pos", (row["id"],)
        ):
            dest = plan_by_track.get(e["track_id"]) if e["track_id"] else None
            if not dest:
                skipped += 1
                continue
            title = e["title_hint"] or (
                f"{e['artist']} - {e['title']}" if e["artist"] else e["title"])
            entries.append((dest, title, e["duration"], e["source_uri"]))
        playlist_plan.append({"name": name, "source_uri": row["source_uri"],
                              "entries": entries, "skipped": skipped,
                              "filename": pl_filename(name)})

    # Playlist files Hoard wrote that this plan no longer includes.
    planned_files = {p["filename"] for p in playlist_plan}
    playlist_deletes = [
        {"filename": r["filename"], "name": r["name"]}
        for r in con.execute(
            "SELECT filename, name FROM device_playlist WHERE device_id=?",
            (device["id"],),
        )
        if r["filename"] not in planned_files
    ]

    bytes_in = sum(c["size"] for c in copies)
    bytes_out = sum(d["size"] for d in deletes if d["present"])

    return {
        "device": device,
        "root": root,
        "music_dir": music_dir,
        "tracks_desired": len(tracks),
        "copies": copies,
        "deletes": deletes,
        "unchanged": unchanged,
        "missing_source": missing_source,
        "playlists": playlist_plan,
        "playlist_deletes": playlist_deletes,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "net_bytes": bytes_in - bytes_out,
    }


def check_space(plan_result, free_bytes, headroom=64 * 1024 * 1024):
    """Would this plan fit? Deletions are not counted as available.

    Deletions happen after copies in the executor only when they free space
    the copies need; assuming otherwise is how a sync fills a card and
    fails halfway. ``headroom`` keeps a little slack for filesystem
    overhead and the playlist files.
    """
    need = plan_result["bytes_in"] + headroom
    return {"need": need, "free": free_bytes, "fits": need <= free_bytes,
            "shortfall": max(0, need - free_bytes)}
