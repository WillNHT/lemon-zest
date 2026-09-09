"""Command line interface."""
import os
import sys
import time

import click
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn,
                           TimeRemainingColumn)
from rich.table import Table

from . import devices as dev_mod
from . import download as dl_mod
from . import executor, planner, playlists, scan
from . import organise as org_mod
from .db import connect, default_db_path

def _utf8_console():
    """Print Vietnamese on a Windows console without dying.

    The library this is built for is full of Vietnamese titles, and the
    device's own playlists are named after its owner. A default Windows
    console encodes stdout as cp1252, so the first attempt to print one of
    those names raises UnicodeEncodeError and takes the command down with
    it - a crash for the exact data the project exists to handle. Ask for
    UTF-8, and fall back to replacing what the terminal cannot draw, since
    an approximate character is a better outcome than no output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return Console()


console = _utf8_console()


def gb(n):
    return f"{n / 2**30:.2f} GB"


def mb(n):
    return f"{n / 2**20:.1f} MB"


def human(n):
    return gb(n) if n >= 2**30 else mb(n)


def dur_text(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_path", default=None, help="Catalog path.")
@click.pass_context
def cli(ctx, db_path):
    """Lemon Zest - sync a local music library to portable players."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path or default_db_path()


def _con(ctx):
    return connect(ctx.obj["db_path"])


# ---------------------------------------------------------------- library

@cli.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--full", is_flag=True, help="Re-read tags even for unchanged files.")
@click.option("--workers", default=8, show_default=True)
@click.pass_context
def scan_cmd(ctx, root, full, workers):
    """Index a library folder."""
    con = _con(ctx)
    with Progress(SpinnerColumn(), TextColumn("[cyan]scanning"), BarColumn(),
                  TextColumn("{task.completed}/{task.total}"),
                  TimeRemainingColumn(), console=console) as prog:
        task = prog.add_task("scan", total=1)

        def cb(done, total):
            prog.update(task, completed=done, total=total)

        counts = scan.scan(con, root, workers=workers, progress=cb, full=full)
    console.print(
        f"[green]{counts['seen']}[/] files - "
        f"{counts['added']} added, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged, {counts['removed']} removed, "
        f"{counts['failed']} failed"
    )


cli.add_command(scan_cmd, name="scan")


@cli.command()
@click.pass_context
def stats(ctx):
    """Summarise the catalog."""
    con = _con(ctx)
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("", justify="right")
    t.add_row("tracks", f"{q('SELECT COUNT(*) FROM track'):,}")
    t.add_row("total size", human(q("SELECT COALESCE(SUM(size),0) FROM track")))
    t.add_row("artists", f"{q('SELECT COUNT(DISTINCT artist) FROM track'):,}")
    t.add_row("albums", f"{q('SELECT COUNT(DISTINCT album) FROM track'):,}")
    t.add_row("with ISRC", f"{q('SELECT COUNT(*) FROM track WHERE isrc IS NOT NULL'):,}")
    t.add_row("with source URL", f"{q('SELECT COUNT(*) FROM track WHERE purl IS NOT NULL'):,}")
    # Kept out of the f-strings: a backslash inside one is a syntax error
    # before Python 3.12, and this project supports 3.10.
    enriched = q("SELECT COUNT(*) FROM enrichment WHERE status = 'applied'")
    waiting = q("SELECT COUNT(*) FROM enrichment WHERE status = 'candidate'")
    t.add_row("identified", f"{enriched:,}")
    t.add_row("awaiting review", f"{waiting:,}")
    t.add_row("playlists", f"{q('SELECT COUNT(*) FROM playlist'):,}")
    t.add_row("playlist entries", f"{q('SELECT COUNT(*) FROM playlist_entry'):,}")
    t.add_row("unmatched entries",
              f"{q('SELECT COUNT(*) FROM playlist_entry WHERE track_id IS NULL'):,}")
    t.add_row("devices", f"{q('SELECT COUNT(*) FROM device'):,}")
    console.print(t)


# -------------------------------------------------------------- downloads

def _cookie_overrides(cookies, firefox_profile):
    """Turn --cookies/--firefox-profile into config overrides.

    ``--cookies`` takes a mode or a path, because those are the two things
    anyone actually wants to say, and a path is unambiguous: no mode looks
    like one.
    """
    out = {}
    if cookies:
        value = cookies.strip()
        if value.lower() in ("auto", "firefox", "none"):
            out["cookies_mode"] = value.lower()
        else:
            out["cookies_mode"] = "file"
            out["cookies_file"] = os.path.abspath(os.path.expanduser(value))
    if firefox_profile:
        out["firefox_profile"] = firefox_profile
        out.setdefault("cookies_mode", "firefox")
    return out


@cli.command("download")
@click.argument("urls", nargs=-1, required=True)
@click.option("--to", "root", default=None,
              help="Library folder to download into. Defaults to the busiest "
                   "indexed folder.")
@click.option("--playlist", "playlist_name", default=None,
              help="Also add what arrives to this catalog playlist.")
@click.option("--format", "audio_format", default=None,
              help="Audio format to extract, e.g. m4a, mp3, opus.")
@click.option("--quality", "audio_quality", default=None,
              help="yt-dlp audio quality; 0 is best.")
@click.option("--cookies", default=None,
              help="auto, firefox, none, or the path to a cookies.txt.")
@click.option("--firefox-profile", default=None,
              help="Which Firefox profile to read cookies from.")
@click.option("--no-playlist", "single", is_flag=True,
              help="Fetch only the video, when the URL also names a playlist.")
@click.option("--no-archive", is_flag=True,
              help="Do not consult the download archive, so a URL already "
                   "fetched is fetched again.")
@click.option("--info", is_flag=True, help="Show what is at the URL and stop.")
@click.pass_context
def download_cmd(ctx, urls, root, playlist_name, audio_format, audio_quality,
                 cookies, firefox_profile, single, no_archive, info):
    """Download audio from YouTube into the library.

    The file lands in a library folder and is indexed on the spot, so it is
    ready to tick onto a device without a rescan.
    """
    con = _con(ctx)
    cfg = dict(dl_mod.get_config(con))
    cfg.update(_cookie_overrides(cookies, firefox_profile))

    if not dl_mod.ytdlp_command():
        raise click.ClickException(
            "yt-dlp is not installed. Install it with: pip install yt-dlp")

    status = dl_mod.cookie_status(cfg)
    colour = "green" if status["source"] != "none" else "yellow"
    console.print(f"[{colour}]cookies:[/] {status['detail']}")
    js = dl_mod.js_runtime_status(cfg)
    console.print(f"[{'green' if js['chosen'] else 'yellow'}]javascript:[/] "
                  f"{js['detail']}")

    if info:
        for url in urls:
            try:
                got = dl_mod.probe(url, cfg)
            except dl_mod.DownloadError as exc:
                raise click.ClickException(str(exc))
            head = f"[bold]{got['title']}[/]"
            if got["uploader"]:
                head += f"  [dim]{got['uploader']}[/]"
            console.print(head)
            if got["is_playlist"]:
                console.print(f"  [dim]{got['count']} items[/]")
                for e in got["entries"][:10]:
                    console.print(f"    {e['title'] or ''}  "
                                  f"[dim]{dur_text(e['duration'])}[/]")
                if got["count"] > 10:
                    console.print(f"    [dim]... {got['count'] - 10} more[/]")
            else:
                console.print(f"  [dim]{dur_text(got['duration'])}[/]")
        return

    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                  BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                  console=console) as prog:
        task = prog.add_task("starting", total=1)
        done_files = []

        def on_event(kind, detail, done, total):
            if kind == "progress":
                prog.update(task, description=detail[:48],
                            completed=done, total=max(total, done, 1))
            elif kind == "file":
                done_files.append(detail)
                prog.update(task, description=os.path.basename(detail)[:48])
            elif kind == "error":
                prog.console.print(f"[red]{detail}[/]")
            elif kind == "index":
                prog.update(task, description="indexing " + detail,
                            completed=1, total=1)

        try:
            summary = dl_mod.download(
                con, list(urls), root=root, playlist=playlist_name, cfg=cfg,
                on_event=on_event, no_playlist=single, archive=not no_archive,
                audio_format=audio_format, audio_quality=audio_quality)
        except dl_mod.DownloadError as exc:
            # The last of the run, so the failure can be read rather than
            # reproduced with a second download to find out what it said.
            for line in getattr(exc, "log", [])[-15:]:
                console.print(f"[dim]{line}[/]")
            raise click.ClickException(str(exc))

    if not summary["downloaded"]:
        console.print("[yellow]nothing new[/] - every URL was already in the "
                      "download archive, or nothing could be fetched.")
    else:
        console.print(f"\n[green]{summary['downloaded']}[/] downloaded into "
                      f"{summary['root']} - {summary['added']} added to the "
                      f"catalog, {summary['updated']} updated")
        for f in summary["files"][:10]:
            console.print(f"  [green]+[/] {os.path.relpath(f, summary['root'])}")
        if len(summary["files"]) > 10:
            console.print(f"  [dim]... {len(summary['files']) - 10} more[/]")
    if summary["failed_index"]:
        console.print(f"[yellow]{summary['failed_index']}[/] of the files "
                      "yt-dlp wrote could not be catalogued - the output "
                      "template puts them outside the library folder.")
    if summary["playlist"]:
        p = summary["playlist"]
        console.print(f"[cyan]playlist[/] {p['name']}: {p['added']} added"
                      + (f", {p['skipped']} already there" if p["skipped"] else "")
                      + f", {p['entries']} entries")
    for e in summary["errors"]:
        console.print(f"[red]{e}[/]")


@cli.command("download-config")
@click.option("--cookies", default=None,
              help="auto, firefox, none, or the path to a cookies.txt.")
@click.option("--firefox-profile", default=None,
              help="Which Firefox profile to read cookies from.")
@click.option("--to", "root", default=None, help="Default download folder.")
@click.option("--output", default=None,
              help="yt-dlp output template, relative to the download folder.")
@click.option("--format", "audio_format", default=None)
@click.option("--quality", "audio_quality", default=None)
@click.option("--js-runtimes", "js_runtimes", default=None,
              help="JavaScript runtimes to enable beyond yt-dlp's own "
                   "default of deno, comma separated. YouTube needs one to "
                   "hand over a playable format.")
@click.pass_context
def download_config(ctx, cookies, firefox_profile, root, output, audio_format,
                    audio_quality, js_runtimes):
    """Show or change how downloads are fetched."""
    con = _con(ctx)
    changes = _cookie_overrides(cookies, firefox_profile)
    changes.update({"root": root, "output": output,
                    "audio_format": audio_format,
                    "audio_quality": audio_quality,
                    "js_runtimes": js_runtimes})
    changes = {k: v for k, v in changes.items() if v is not None}
    cfg = dl_mod.set_config(con, **changes) if changes else dl_mod.get_config(con)

    version = dl_mod.ytdlp_version()
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("")
    t.add_row("yt-dlp", version or "[red]not installed[/]")
    for key in ("cookies_mode", "firefox_profile", "cookies_file", "root",
                "output", "audio_format", "audio_quality", "js_runtimes"):
        t.add_row(key, str(cfg[key] or "-"))
    console.print(t)

    js = dl_mod.js_runtime_status(cfg)
    console.print()
    console.print(f"[{'green' if js['chosen'] else 'yellow'}]{js['detail']}[/]")
    if js["found"]:
        for r in js["found"]:
            console.print(f"  {r['name']}  [dim]{r['path']}[/]")

    status = dl_mod.cookie_status(cfg)
    colour = "green" if status["source"] != "none" else "yellow"
    console.print(f"\n[{colour}]{status['detail']}[/]")
    if status["profiles"]:
        console.print("\n[bold]Firefox profiles with cookies[/]")
        for p in status["profiles"]:
            mark = ("[green] <- in use[/]"
                    if status["profile"] and p["path"] == status["profile"]["path"]
                    else "")
            console.print(f"  {p['name']}{mark}\n    [dim]{p['path']}[/]")
    else:
        console.print("[dim]no Firefox profile with a cookie database found "
                      "on this machine.[/]")


# -------------------------------------------------------------- playlists

@cli.group()
def playlist():
    """Work with playlists."""


@playlist.command("import")
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--recursive", "-r", is_flag=True)
@click.pass_context
def playlist_import(ctx, directory, recursive):
    """Import m3u/m3u8 files from a folder."""
    con = _con(ctx)
    results = playlists.import_dir(con, directory, recursive=recursive)
    t = Table("playlist", "entries", "matched", "unmatched", "mode", box=None)
    for r in results:
        t.add_row(r["name"], str(r["total"]), str(r["matched"]),
                  f"[yellow]{r['unmatched']}[/]" if r["unmatched"] else "0",
                  r["mode"] + (f" +{r['enriched']} uris" if r["enriched"] else ""))
    console.print(t)
    tot = sum(r["total"] for r in results)
    mat = sum(r["matched"] for r in results)
    console.print(f"[green]{len(results)}[/] playlists, {tot} entries, "
                  f"{mat} matched ({100 * mat / max(tot, 1):.1f}%)")


@playlist.command("list")
@click.pass_context
def playlist_list(ctx):
    """List catalogued playlists."""
    con = _con(ctx)
    t = Table("name", "entries", "unmatched", "origin", "source", box=None)
    for r in con.execute(
        "SELECT p.name, p.origin, p.source_uri, "
        "(SELECT COUNT(*) FROM playlist_entry e WHERE e.playlist_id=p.id) n, "
        "(SELECT COUNT(*) FROM playlist_entry e WHERE e.playlist_id=p.id "
        " AND e.track_id IS NULL) u "
        "FROM playlist p ORDER BY n DESC"
    ):
        t.add_row(r["name"], str(r["n"]),
                  f"[yellow]{r['u']}[/]" if r["u"] else "0",
                  r["origin"] or "", (r["source_uri"] or "")[:46])
    console.print(t)


@playlist.command("unmatched")
@click.pass_context
def playlist_unmatched(ctx):
    """Show playlist entries that resolve to no file."""
    con = _con(ctx)
    rows = con.execute(
        "SELECT p.name, e.pos, e.title_hint, e.raw_path FROM playlist_entry e "
        "JOIN playlist p ON p.id=e.playlist_id WHERE e.track_id IS NULL "
        "ORDER BY p.name, e.pos"
    ).fetchall()
    if not rows:
        console.print("[green]every playlist entry resolves to a file.[/]")
        return
    t = Table("playlist", "#", "track", "path in playlist", box=None)
    for r in rows:
        t.add_row(r["name"], str(r["pos"]), r["title_hint"] or "",
                  r["raw_path"][:70])
    console.print(t)
    console.print(f"[yellow]{len(rows)}[/] unmatched entries")


# ---------------------------------------------------------------- devices

@cli.group()
def device():
    """Work with devices."""


@device.command("detect")
@click.option("--all", "show_all", is_flag=True, help="Include fixed disks.")
def device_detect(show_all):
    """List mounted volumes that could be a player."""
    vols = dev_mod.list_volumes(removable_only=not show_all)
    if not vols:
        console.print("[yellow]no removable volumes mounted.[/] "
                      "Use --all to see fixed disks.")
        return
    t = Table("mount", "label", "fs", "capacity", "free", "paired", box=None)
    for v in vols:
        t.add_row(v["mountpoint"], v["label"] or "[dim]unlabelled[/]",
                  v["fstype"], gb(v["total"]), gb(v["free"]),
                  "[green]yes[/]" if v["device_uid"] else "no")
    console.print(t)


@device.command("add")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--name", default=None, help="What to call it.")
@click.option("--profile", default="ums",
              type=click.Choice(sorted(dev_mod.PROFILES)), show_default=True)
@click.pass_context
def device_add(ctx, root, name, profile):
    """Pair a device by its mount point."""
    con = _con(ctx)
    row = dev_mod.register(con, root, name=name, profile=profile)
    console.print(
        f"[green]paired[/] {row['name']}  label={row['label'] or '-'}  "
        f"profile={row['profile']}  id={row['device_uid'][:8]}"
    )
    if not row["label"]:
        console.print("[yellow]note:[/] this volume has no label. Give it one "
                      "so it is easy to recognise; the marker file will "
                      "identify it either way.")


@device.command("list")
@click.pass_context
def device_list(ctx):
    """List paired devices."""
    con = _con(ctx)
    rows = con.execute("SELECT * FROM device ORDER BY name").fetchall()
    if not rows:
        console.print("[yellow]no devices paired yet.[/] "
                      "Try: lemon-zest device detect")
        return
    t = Table("name", "label", "profile", "set", "on card", "mounted", "last sync",
              box=None)
    for d in rows:
        where = dev_mod.locate(con, d)
        n_set = con.execute(
            "SELECT COUNT(*) FROM device_set WHERE device_id=?", (d["id"],)
        ).fetchone()[0]
        n_files = con.execute(
            "SELECT COUNT(*) FROM device_manifest WHERE device_id=?", (d["id"],)
        ).fetchone()[0]
        last = (time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_sync"]))
                if d["last_sync"] else "never")
        t.add_row(d["name"], d["label"] or "-", d["profile"], f"{n_set} rules",
                  f"{n_files:,}",
                  f"[green]{where}[/]" if where else "[dim]not mounted[/]", last)
    console.print(t)


def _require_device(con, ref):
    d = dev_mod.find(con, ref)
    if d is None:
        raise click.ClickException(f"no device matching {ref!r}")
    return d


@device.command("set")
@click.argument("device_ref")
@click.option("--playlist", "playlists_", multiple=True, help="Add a playlist.")
@click.option("--artist", "artists", multiple=True, help="Add an artist.")
@click.option("--album", "albums", multiple=True, help="Add an album.")
@click.option("--remove", is_flag=True, help="Remove the given rules instead.")
@click.option("--clear", is_flag=True, help="Drop every rule first.")
@click.pass_context
def device_set(ctx, device_ref, playlists_, artists, albums, remove, clear):
    """Edit what a device should hold."""
    con = _con(ctx)
    d = _require_device(con, device_ref)
    if clear:
        con.execute("DELETE FROM device_set WHERE device_id=?", (d["id"],))
    pairs = ([("playlist", p) for p in playlists_]
             + [("artist", a) for a in artists]
             + [("album", a) for a in albums])
    for kind, ref in pairs:
        if remove:
            con.execute(
                "DELETE FROM device_set WHERE device_id=? AND kind=? AND ref=?",
                (d["id"], kind, ref))
        else:
            con.execute(
                "INSERT OR IGNORE INTO device_set(device_id, kind, ref, added_at) "
                "VALUES (?,?,?,?)", (d["id"], kind, ref, time.time()))
    con.commit()
    rules = con.execute(
        "SELECT kind, ref FROM device_set WHERE device_id=? ORDER BY kind, ref",
        (d["id"],)).fetchall()
    tracks, _ = planner.desired_tracks(con, d["id"])
    size = sum(t["size"] for t in tracks)
    console.print(f"[green]{d['name']}[/]: {len(rules)} rules -> "
                  f"{len(tracks):,} tracks, {human(size)}")
    for r in rules:
        console.print(f"   [dim]{r['kind']:9}[/] {r['ref']}")


def _detect_playlist_template(con, d):
    """Read a mounted card and report the naming its own playlists use.

    Returns the template, or None when the card holds no playlist file
    carrying a name this catalog knows - in which case the stored template
    is left alone rather than replaced with a guess.
    """
    root = _resolve_root(con, d, None)
    pl_dir = (d["playlist_dir"] or "").strip("/")
    pl_root = os.path.join(root, pl_dir) if pl_dir else root
    files = playlists.list_playlist_files(pl_root)
    names = [r["name"] for r in con.execute("SELECT name FROM playlist")]
    template, matched, total = playlists.infer_template(files, names)
    if not template:
        console.print(f"[yellow]none of the {total} playlist files in "
                      f"{pl_root} carry a name this catalog knows[/]")
        return None
    console.print(f"[green]{matched} of {total}[/] playlist files on the card "
                  f"are named [bold]{template}[/]")
    return template


@device.command("config")
@click.argument("device_ref")
@click.option("--template", default=None,
              help="Destination path template, or {rel_path} to mirror the "
                   "library's own folder layout.")
@click.option("--music-dir", default=None, help="Folder on the card for audio.")
@click.option("--playlist-dir", default=None, help="Folder for playlists.")
@click.option("--playlist-template", default=None,
              help="How this player names its playlist files, with {name} "
                   "standing for the playlist itself.")
@click.option("--detect-playlists", is_flag=True,
              help="Read the mounted card and adopt its own playlist naming.")
@click.option("--name", default=None)
@click.option("--label", default=None, help="Record the volume label.")
@click.pass_context
def device_config(ctx, device_ref, template, music_dir, playlist_dir,
                  playlist_template, detect_playlists, name, label):
    """Show or change how a device is laid out."""
    con = _con(ctx)
    d = _require_device(con, device_ref)
    if detect_playlists:
        playlist_template = _detect_playlist_template(con, d) or playlist_template
    changes = {"path_template": template, "music_dir": music_dir,
               "playlist_dir": playlist_dir, "name": name, "label": label,
               "playlist_template": playlist_template}
    changes = {k: v for k, v in changes.items() if v is not None}
    for key, val in changes.items():
        con.execute(f"UPDATE device SET {key}=? WHERE id=?", (val, d["id"]))
    if changes:
        con.commit()
        d = _require_device(con, device_ref)
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("")
    for key in ("name", "label", "profile", "music_dir", "playlist_dir",
                "path_template", "playlist_template", "root", "device_uid"):
        t.add_row(key, str(d[key]))
    console.print(t)


@device.command("adopt")
@click.argument("device_ref")
@click.option("--root", default=None, help="Where the device is mounted.")
@click.option("--verify", type=click.Choice(["size", "content"]),
              default="size", show_default=True,
              help="How closely to check that a file on the card matches.")
@click.pass_context
def device_adopt(ctx, device_ref, root, verify):
    """Recognise files already on a card instead of recopying them.

    Reads the card and writes only to the catalog. Use it once, after
    pairing a device that some other tool already filled.
    """
    con = _con(ctx)
    d = _require_device(con, device_ref)
    root = _resolve_root(con, d, root)
    with console.status(f"checking {d['name']} against the plan..."):
        res = planner.adopt(con, d, root, verify=verify)
    console.print(
        f"[green]{res['adopted']:,}[/] of {res['considered']:,} files were "
        f"already in place and have been recorded"
        + (f", [yellow]{res['mismatched']:,}[/] differ" if res["mismatched"] else "")
        + (f", {res['absent']:,} not on the card" if res["absent"] else "")
    )
    if res["adopted"]:
        console.print("[dim]those will not be copied again. "
                      "Run 'lemon-zest plan' to see what is left.[/]")


# ------------------------------------------------------------- plan/sync

def _resolve_root(con, d, override):
    root = override or dev_mod.locate(con, d)
    if not root:
        raise click.ClickException(
            f"{d['name']} is not mounted. Plug it in, or pass --root.")
    if not os.path.isdir(root):
        raise click.ClickException(f"not a directory: {root}")
    return root


def _print_plan(p, space):
    d = p["device"]
    console.rule(f"[bold]{d['name']}[/] - {p['root']}")
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("", justify="right")
    t.add_row("tracks in set", f"{p['tracks_desired']:,}")
    t.add_row("already correct", f"{len(p['unchanged']):,}")
    t.add_row("[green]to copy[/]", f"{len(p['copies']):,}  ({human(p['bytes_in'])})")
    t.add_row("[red]to remove[/]", f"{len(p['deletes']):,}  ({human(p['bytes_out'])})")
    t.add_row("playlists to write", f"{len(p['playlists']):,}")
    if p.get("playlist_deletes"):
        t.add_row("[red]playlists to remove[/]", f"{len(p['playlist_deletes']):,}")
    if p["missing_source"]:
        t.add_row("[yellow]skipped sources[/]", f"{len(p['missing_source']):,}")
    t.add_row("free on card", human(space["free"]))
    t.add_row("needed", human(space["need"]))
    console.print(t)

    if p["copies"]:
        console.print("\n[bold]copies[/] (first 10)")
        for c in p["copies"][:10]:
            console.print(f"  [green]+[/] {c['rel']}  [dim]{c['reason']}, "
                          f"{mb(c['size'])}[/]")
        if len(p["copies"]) > 10:
            console.print(f"  [dim]... {len(p['copies']) - 10:,} more[/]")
    if p["deletes"]:
        console.print("\n[bold]removals[/] (first 10)")
        for c in p["deletes"][:10]:
            console.print(f"  [red]-[/] {c['rel']}  [dim]{c['reason']}[/]")
        if len(p["deletes"]) > 10:
            console.print(f"  [dim]... {len(p['deletes']) - 10:,} more[/]")
    if p["playlists"]:
        console.print(f"\n[bold]playlists[/] [dim]as "
                      f"{p.get('playlist_template', '{name}.m3u8')}[/]")
        for pl in p["playlists"]:
            skip = f"  [yellow]{pl['skipped']} not in set[/]" if pl["skipped"] else ""
            verb = "[green]replaces[/]" if pl.get("replaces") else "[dim]new[/]"
            console.print(f"  {pl['filename']}  [dim]{len(pl['entries'])} "
                          f"entries[/]  {verb}{skip}")
    if p.get("playlist_strays"):
        shadowing = [x for x in p["playlist_strays"] if x["shadows"]]
        console.print("\n[bold yellow]playlist files this sync would leave "
                      "behind[/]")
        for x in p["playlist_strays"][:10]:
            why = (f"[yellow]the player would list this beside "
                   f"{x['shadows']}[/]" if x["shadows"]
                   else "[dim]not in the set[/]")
            console.print(f"  [yellow]![/] {x['filename']}  {why}")
        if len(p["playlist_strays"]) > 10:
            console.print(f"  [dim]... {len(p['playlist_strays']) - 10:,} more[/]")
        if shadowing:
            console.print(
                "  [dim]this device spells its playlists differently, so the "
                "sync would add rather than replace. Fix it with:[/]\n"
                f"  [dim]  lemon-zest device config \"{d['name']}\" "
                f"--detect-playlists[/]")
    if p["missing_source"]:
        console.print("\n[yellow]skipped - not usable as a source[/]")
        for m in p["missing_source"][:10]:
            console.print(f"  [dim]{m['why']:12}[/] {m['track']['path']}")

    if not space["fits"]:
        console.print(f"\n[bold red]will not fit[/] - short by "
                      f"{human(space['shortfall'])}")
    elif not (p["copies"] or p["deletes"]):
        console.print("\n[green]nothing to do - the card is already correct.[/]")


@cli.command("plan")
@click.argument("device_ref")
@click.option("--root", default=None, help="Where the device is mounted.")
@click.option("--prune", is_flag=True,
              help="Also remove files under the music folder that Lemon Zest did "
                   "not put there.")
@click.pass_context
def plan_cmd(ctx, device_ref, root, prune):
    """Dry run: show exactly what a sync would do."""
    con = _con(ctx)
    d = _require_device(con, device_ref)
    root = _resolve_root(con, d, root)
    p = planner.plan(con, d, root, prune=prune)
    space = planner.check_space(p, dev_mod.free_space(root)["free"])
    _print_plan(p, space)


@cli.command("sync")
@click.argument("device_ref")
@click.option("--root", default=None, help="Where the device is mounted.")
@click.option("--prune", is_flag=True,
              help="Also remove files under the music folder that Lemon Zest did "
                   "not put there.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.pass_context
def sync_cmd(ctx, device_ref, root, prune, yes):
    """Copy, remove and write playlists to bring a device up to date."""
    con = _con(ctx)
    d = _require_device(con, device_ref)
    root = _resolve_root(con, d, root)
    p = planner.plan(con, d, root, prune=prune)
    space = planner.check_space(p, dev_mod.free_space(root)["free"])
    _print_plan(p, space)

    if not space["fits"]:
        raise click.ClickException(
            "refusing to start: the set does not fit on the card.")
    if not (p["copies"] or p["deletes"] or p["playlists"]
            or p.get("playlist_deletes")):
        return
    if not yes:
        console.print()
        click.confirm(f"Apply this to {d['name']} at {root}?", abort=True)

    dev_mod.touch(con, d["id"], root)
    total = len(p["copies"])
    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  TimeRemainingColumn(), console=console) as prog:
        task = prog.add_task("copying", total=max(total, 1))

        def on_event(kind, detail, done, tot):
            if kind in ("copy", "error"):
                prog.update(task, completed=done, description=detail[-48:])
            elif kind == "playlist":
                prog.update(task, description=f"playlist {detail[:40]}")

        try:
            summary = executor.execute(con, p, prune=prune, on_event=on_event)
        except executor.Aborted as exc:
            raise click.ClickException(
                f"{exc}. Nothing was left half-written; run sync again to "
                "resume where it stopped.")

    console.print(
        f"\n[green]done[/] - {summary['copied']:,} copied "
        f"({human(summary['bytes'])}), {summary['deleted']:,} removed, "
        f"{summary['playlists']} playlists written, "
        f"{summary['skipped_unchanged']:,} already correct"
    )
    if summary["failed"]:
        console.print(f"[red]{summary['failed']} failed[/]")
        for e in summary["errors"][:10]:
            console.print(f"  [red]![/] {e}")
    if summary["free_after"] is not None:
        console.print(f"[dim]{human(summary['free_after'])} free on card[/]")


@cli.command("log")
@click.argument("device_ref", required=False)
@click.option("-n", "limit", default=25, show_default=True)
@click.pass_context
def log_cmd(ctx, device_ref, limit):
    """Show recent sync activity."""
    con = _con(ctx)
    if device_ref:
        d = _require_device(con, device_ref)
        rows = con.execute(
            "SELECT * FROM sync_log WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (d["id"], limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    colour = {"copy": "green", "delete": "red", "error": "bold red",
              "playlist": "cyan", "start": "dim", "done": "bold"}
    for r in reversed(rows):
        ts = time.strftime("%H:%M:%S", time.localtime(r["at"]))
        c = colour.get(r["kind"], "")
        size = f"  [dim]{mb(r['size'])}[/]" if r["size"] else ""
        console.print(f"[dim]{ts}[/] [{c}]{r['kind']:9}[/] {r['detail']}{size}")


@cli.command("doctor")
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def doctor_cmd(ctx, directory):
    """Check playlists in a folder for duplicate entries and dead paths."""
    import glob
    import urllib.parse

    files = sorted(glob.glob(os.path.join(directory, "*.m3u8"))
                   + glob.glob(os.path.join(directory, "*.m3u")))
    if not files:
        console.print("[yellow]no playlists found there.[/]")
        return
    t = Table("playlist", "entries", "unique", "repeats", "dead paths", box=None)
    tot = uniq_tot = dead_tot = 0
    for f in files:
        pl = playlists.read(f)
        paths = [urllib.parse.unquote(e["raw_path"]) for e in pl["entries"]]
        uniq = len(dict.fromkeys(paths))
        dead = sum(1 for e in pl["entries"] if e["abs_path"] is None)
        rep = len(paths) - uniq
        tot += len(paths)
        uniq_tot += uniq
        dead_tot += dead
        t.add_row(pl["name"], f"{len(paths):,}", f"{uniq:,}",
                  f"[red]{rep:,}[/]" if rep else "0",
                  f"[yellow]{dead:,}[/]" if dead else "0")
    console.print(t)
    console.print(f"\n{len(files)} playlists - {tot:,} entries, {uniq_tot:,} unique, "
                  f"{tot - uniq_tot:,} repeats, {dead_tot:,} dead paths")
    if tot > uniq_tot:
        factor = tot / max(uniq_tot, 1)
        console.print(
            f"[red]these playlists have grown {factor:.1f}x[/] - each sync "
            "appended instead of replacing. Lemon Zest rewrites playlists in "
            "place, so syncing with it once will collapse them back."
        )


# ------------------------------------------------------------- enrichment

@cli.group()
def enrich():
    """Identify tracks against MusicBrainz and correct their metadata."""


def _track_ref(con, ref):
    """Find one track by id, content key, or a fragment of its path."""
    row = None
    if str(ref).isdigit():
        row = con.execute("SELECT * FROM track WHERE id = ?", (int(ref),)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM track WHERE content_key = ?",
                          (ref,)).fetchone()
    if row is None:
        rows = con.execute("SELECT * FROM track WHERE rel_path LIKE ? LIMIT 5",
                           (f"%{ref}%",)).fetchall()
        if len(rows) > 1:
            raise click.ClickException(
                f"{ref!r} matches {len(rows)} tracks; use the id instead:\n  "
                + "\n  ".join(f"{r['id']}  {r['rel_path']}" for r in rows))
        row = rows[0] if rows else None
    if row is None:
        raise click.ClickException(f"no track matching {ref!r}")
    return row


@enrich.command("status")
@click.pass_context
def enrich_status(ctx, ):
    """How much of the library is identified."""
    from . import enrich as en
    con = _con(ctx)
    s = en.summary(con)
    total = max(s["tracks"], 1)
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("", justify="right")
    t.add_column("", justify="right", style="dim")
    for label, key in (("tracks", "tracks"), ("carrying an ISRC", "with_isrc"),
                       ("enriched", "applied"), ("awaiting review", "candidates"),
                       ("rejected", "rejected"), ("no match found", "unmatched"),
                       ("hand-corrected", "overrides")):
        pct = f"{100 * s[key] / total:.1f}%" if key != "tracks" else ""
        t.add_row(label, f"{s[key]:,}", pct)
    console.print(t)
    if s["candidates"]:
        console.print(f"\n[yellow]{s['candidates']} candidates need a decision[/] - "
                      "lemon-zest enrich review")


@enrich.command("config")
@click.option("--acoustid-key", default=None,
              help="Free application key from acoustid.org, for fingerprinting.")
@click.option("--contact", default=None,
              help="Contact URL or address for the MusicBrainz User-Agent.")
@click.pass_context
def enrich_config(ctx, acoustid_key, contact):
    """Show or change the enrichment settings."""
    from . import enrich as en
    con = _con(ctx)
    en.set_config(con, acoustid_key=acoustid_key, contact=contact)
    cfg = en.get_config(con)
    fp = en.fingerprint_status(con)

    t = Table(box=None, pad_edge=False)
    t.add_column("", style="dim")
    t.add_column("")
    t.add_row("contact", cfg["contact"] or "[dim](the project URL)[/]")
    # The key is an application identifier rather than a secret, but there is
    # no reason to put it on someone's screen in full.
    key = cfg["acoustid_key"]
    t.add_row("AcoustID key",
              (key[:4] + "…" + key[-2:]) if len(key) > 6 else
              ("set" if key else "[dim]not set[/]"))
    t.add_row("fpcalc", fp["fpcalc"] or "[dim]not found[/]")
    t.add_row("fingerprinting",
              "[green]ready[/]" if fp["ready"]
              else "[yellow]needs " + " and ".join(fp["missing"]) + "[/]")
    console.print(t)
    if not fp["fpcalc"]:
        console.print("\n[dim]fpcalc comes with Chromaprint:[/]\n"
                      "  winget install AcoustID.Chromaprint")
    if not fp["has_key"]:
        console.print("\n[dim]a key is free at "
                      "https://acoustid.org/new-application[/]\n"
                      "  lemon-zest enrich config --acoustid-key <key>")


@enrich.command("backfill")
@click.option("--dry-run", is_flag=True, help="Report matches without storing them.")
@click.pass_context
def enrich_backfill(ctx, dry_run):
    """Copy ISRCs onto untagged twins already in the library. Offline."""
    from . import enrich as en
    con = _con(ctx)
    counts = en.backfill_isrc(con, dry_run=dry_run)
    console.print(
        f"{counts['scanned']:,} tracks without an ISRC - "
        f"[green]{counts['matched']:,}[/] matched a twin that has one, "
        f"{counts['ambiguous']:,} ambiguous"
        + (" [dim](dry run, nothing stored)[/]" if dry_run else ""))
    if counts["matched"] and not dry_run:
        console.print("[dim]stored as candidates; apply them with "
                      "'enrich review' or the next 'enrich run'.[/]")


@enrich.command("run")
@click.option("--root", type=click.Path(), default=None,
              help="Only tracks under this library root.")
@click.option("--limit", type=int, default=None,
              help="Stop after this many lookups.")
@click.option("--redo", is_flag=True, help="Re-look-up tracks already decided.")
@click.option("--write-tags", is_flag=True,
              help="Write applied values into the audio files themselves.")
@click.option("--artwork", is_flag=True,
              help="With --write-tags, replace the cover with the release's own.")
@click.option("--contact", default=None,
              help="Contact URL or address for the MusicBrainz User-Agent.")
@click.option("--fingerprint", is_flag=True,
              help="Identify by audio when the tags are not enough "
                   "(needs fpcalc and an AcoustID key).")
@click.option("--yes", is_flag=True, help="Do not ask before writing to files.")
@click.pass_context
def enrich_run(ctx, root, limit, redo, write_tags, artwork, contact,
               fingerprint, yes):
    """Look tracks up against MusicBrainz and store what it says.

    Rate-limited to one request per second, as MusicBrainz asks. Stopping it
    early is safe: every decision is committed as it is made, and the next
    run continues where this one left off.
    """
    from . import enrich as en
    con = _con(ctx)

    if fingerprint:
        fp = en.fingerprint_status(con)
        if not fp["ready"]:
            raise click.ClickException(
                "fingerprinting needs " + " and ".join(fp["missing"])
                + " - see 'lemon-zest enrich config'")

    if write_tags and not yes:
        click.confirm(
            "--write-tags rewrites the tags inside your audio files. "
            "Each file is rewritten to a copy and swapped in, so an "
            "interruption leaves the original. Continue?", abort=True)

    pend = en.pending(con, limit=limit, root=root, redo=redo)
    if not pend:
        console.print("[green]nothing to look up.[/] "
                      "Use --redo to revisit tracks already decided.")
        return
    console.print(f"{len(pend):,} tracks to look up "
                  f"[dim](~{len(pend) * 1.05 / 60:.0f} min at 1 req/s)[/]")

    with Progress(SpinnerColumn(), TextColumn("[cyan]identifying"), BarColumn(),
                  TextColumn("{task.completed}/{task.total}"),
                  TimeRemainingColumn(), console=console) as prog:
        task = prog.add_task("enrich", total=len(pend))

        def cb(done, total, status):
            prog.update(task, completed=done, total=total)

        counts = en.run(con, root=root, limit=limit, redo=redo,
                        write_tags=write_tags, artwork=artwork,
                        contact=contact, use_fingerprint=fingerprint,
                        progress=cb)

    console.print(
        f"[green]{counts['applied']}[/] identified, "
        f"[yellow]{counts['candidates']}[/] need review, "
        f"{counts['unmatched']} no match"
        + (f", {counts['backfilled']} backfilled" if counts["backfilled"] else "")
        + (f", [red]{counts['failed']} lookups failed[/]" if counts["failed"] else "")
        + (f", [green]{counts['written']}[/] files rewritten" if write_tags else "")
        + (f", [red]{counts['write_failed']} writes failed[/]"
           if counts["write_failed"] else ""))
    if counts["stopped"]:
        console.print(f"[red]stopped early:[/] {counts['stopped']}")
    if counts["candidates"]:
        console.print("[dim]lemon-zest enrich review[/]")


@enrich.command("review")
@click.option("--limit", default=20, show_default=True)
@click.pass_context
def enrich_review(ctx, limit):
    """List candidates that need a decision, least confident first."""
    from . import enrich as en
    con = _con(ctx)
    rows = en.review_queue(con, limit=limit)
    if not rows:
        console.print("[green]nothing waiting.[/]")
        return
    for r in rows:
        p = r["proposed"]
        console.print(f"\n[bold]{r['track_id']}[/]  [dim]{r['rel_path']}[/]  "
                      f"[{'yellow' if r['confidence'] < 0.8 else 'green'}]"
                      f"{r['confidence']:.2f}[/] [dim]via {r['source']}[/]")
        t = Table(box=None, pad_edge=False, show_header=True)
        t.add_column("", style="dim")
        t.add_column("in the file")
        t.add_column("proposed", style="cyan")
        for field in ("title", "artist", "album", "album_artist", "year", "isrc"):
            old = r.get(field) or ""
            new = p.get(field)
            if new is None or str(new) == str(old):
                continue
            t.add_row(field, str(old), str(new))
        console.print(t)
    console.print(f"\n[dim]lemon-zest enrich accept <id>   |   "
                  f"enrich reject <id>[/]")


@enrich.command("accept")
@click.argument("ref")
@click.option("--write-tags", is_flag=True, help="Also write it into the file.")
@click.option("--artwork", is_flag=True, help="With --write-tags, fetch the cover.")
@click.pass_context
def enrich_accept(ctx, ref, write_tags, artwork):
    """Apply a stored candidate."""
    from . import enrich as en
    con = _con(ctx)
    row = _track_ref(con, ref)
    if not en.accept(con, row["content_key"]):
        raise click.ClickException("no candidate stored for that track.")
    console.print(f"[green]applied[/] to {row['rel_path']}")
    if write_tags:
        ok = en.write_back(con, row["content_key"], artwork=artwork)
        console.print("[green]file rewritten[/]" if ok
                      else "[red]could not write the file[/]")


@enrich.command("reject")
@click.argument("ref")
@click.pass_context
def enrich_reject(ctx, ref):
    """Mark a candidate wrong. It will not be offered or re-fetched."""
    from . import enrich as en
    con = _con(ctx)
    row = _track_ref(con, ref)
    en.reject(con, row["content_key"])
    console.print(f"[yellow]rejected[/] for {row['rel_path']}")


@enrich.command("set")
@click.argument("ref")
@click.option("--title")
@click.option("--artist")
@click.option("--album")
@click.option("--album-artist")
@click.option("--year")
@click.option("--isrc")
@click.option("--write-tags", is_flag=True, help="Also write it into the file.")
@click.pass_context
def enrich_set(ctx, ref, write_tags, **fields):
    """Correct a track by hand. Outranks every source, now and later."""
    from . import enrich as en
    con = _con(ctx)
    given = {k: v for k, v in fields.items() if v is not None}
    if not given:
        raise click.ClickException("give at least one field to set.")
    row = _track_ref(con, ref)
    en.override(con, row["content_key"], **given)
    console.print(f"[green]set[/] {', '.join(given)} on {row['rel_path']}")
    if write_tags:
        ok = en.write_back(con, row["content_key"])
        console.print("[green]file rewritten[/]" if ok else
                      "[yellow]nothing applied yet - accept a candidate first, "
                      "or the file has no enrichment to write.[/]")


# --------------------------------------------------------------- organise

@cli.group()
def organise():
    """Move library files so the folders match the metadata."""


def _journal_dir(ctx):
    return os.path.dirname(os.path.abspath(ctx.obj["db_path"]))


@organise.command("plan")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--template", default=None,
              help=f"Destination layout. Default: {org_mod.DEFAULT_TEMPLATE}")
@click.option("--limit", type=int, default=40, show_default=True,
              help="How many moves to list; 0 lists all of them.")
@click.pass_context
def organise_plan(ctx, root, template, limit):
    """Show what would move. Touches nothing."""
    con = _con(ctx)
    moves, skipped = org_mod.plan(con, root, template=template)
    _print_organise(con, moves, skipped, limit)


def _print_organise(con, moves, skipped, limit):
    if not moves:
        console.print("[green]every file is already where the metadata says.[/]")
    for m in (moves if not limit else moves[:limit]):
        console.print(f"[dim]{m['src_rel']}[/]\n  [green]->[/] {m['dest_rel']}")
    if limit and len(moves) > limit:
        console.print(f"[dim]… {len(moves) - limit:,} more[/]")
    console.print(f"\n{len(moves):,} to move, {skipped and len(skipped) or 0:,} "
                  "skipped for having no artist to file them under")

    affected = org_mod.affected_devices(con)
    if moves and affected:
        names = ", ".join(d["name"] for d in affected)
        console.print(
            f"[yellow]note:[/] {names} mirror(s) the library layout "
            "([dim]{rel_path}[/]), so the next sync will move these on the "
            "card too - the files are already there, but it is a lot of "
            "writing to a memory card.")


@organise.command("apply")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--template", default=None, help="Destination layout.")
@click.option("--yes", is_flag=True, help="Do not ask.")
@click.pass_context
def organise_apply(ctx, root, template, yes):
    """Move the files. Writes a journal so it can be undone."""
    con = _con(ctx)
    moves, skipped = org_mod.plan(con, root, template=template)
    if not moves:
        console.print("[green]every file is already where the metadata says.[/]")
        return
    _print_organise(con, moves, skipped, limit=20)

    if not yes:
        click.confirm(f"\nMove {len(moves):,} files?", abort=True)

    with Progress(SpinnerColumn(), TextColumn("[cyan]moving"), BarColumn(),
                  TextColumn("{task.completed}/{task.total}"),
                  console=console) as prog:
        task = prog.add_task("move", total=len(moves))
        counts, journal = org_mod.apply(
            con, moves, journal_dir=_journal_dir(ctx),
            progress=lambda d, t: prog.update(task, completed=d, total=t))

    console.print(f"[green]{counts['moved']:,}[/] moved"
                  + (f", [red]{counts['failed']} failed[/]"
                     if counts["failed"] else ""))
    if journal:
        console.print(f"[dim]undo with: lemon-zest organise undo "
                      f"{os.path.basename(journal)}[/]")


@organise.command("undo")
@click.argument("journal", required=False)
@click.pass_context
def organise_undo(ctx, journal):
    """Put the files from a previous run back where they were."""
    con = _con(ctx)
    directory = _journal_dir(ctx)
    past = org_mod.journals(directory)
    if not journal:
        if not past:
            console.print("[dim]no organise runs to undo.[/]")
            return
        t = Table("journal", "moves", "when", box=None)
        for j in past:
            t.add_row(os.path.basename(j["path"]), f"{j['moves']:,}",
                      time.strftime("%Y-%m-%d %H:%M", time.localtime(j["at"])))
        console.print(t)
        console.print("\n[dim]lemon-zest organise undo <journal>[/]")
        return

    path = journal if os.path.isabs(journal) else os.path.join(directory, journal)
    if not os.path.exists(path):
        raise click.ClickException(f"no such journal: {path}")
    counts = org_mod.undo(con, path)
    console.print(f"[green]{counts['restored']:,}[/] put back"
                  + (f", [red]{counts['failed']} failed[/]"
                     if counts["failed"] else ""))


@cli.command("gui")
@click.option("--port", default=7777, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--no-browser", is_flag=True, help="Do not open a browser window.")
@click.pass_context
def gui_cmd(ctx, port, host, no_browser):
    """Open the desktop interface in a browser."""
    try:
        from .server import serve
    except ImportError:
        raise click.ClickException(
            "the interface needs Flask: pip install flask")
    console.print(f"[green]Lemon Zest[/] running at http://{host}:{port}/  "
                  f"[dim](ctrl-c to stop)[/]")
    serve(db_path=ctx.obj["db_path"], host=host, port=port,
          open_browser=not no_browser)


def main():
    try:
        cli(obj={})
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted.[/] Nothing was left half-written.")
        sys.exit(130)


if __name__ == "__main__":
    main()
