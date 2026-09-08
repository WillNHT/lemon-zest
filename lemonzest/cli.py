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
from . import executor, planner, playlists, scan
from .db import connect, default_db_path

console = Console()


def gb(n):
    return f"{n / 2**30:.2f} GB"


def mb(n):
    return f"{n / 2**20:.1f} MB"


def human(n):
    return gb(n) if n >= 2**30 else mb(n)


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
    t.add_row("playlists", f"{q('SELECT COUNT(*) FROM playlist'):,}")
    t.add_row("playlist entries", f"{q('SELECT COUNT(*) FROM playlist_entry'):,}")
    t.add_row("unmatched entries",
              f"{q('SELECT COUNT(*) FROM playlist_entry WHERE track_id IS NULL'):,}")
    t.add_row("devices", f"{q('SELECT COUNT(*) FROM device'):,}")
    console.print(t)


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


@device.command("config")
@click.argument("device_ref")
@click.option("--template", default=None,
              help="Destination path template, or {rel_path} to mirror the "
                   "library's own folder layout.")
@click.option("--music-dir", default=None, help="Folder on the card for audio.")
@click.option("--playlist-dir", default=None, help="Folder for playlists.")
@click.option("--name", default=None)
@click.option("--label", default=None, help="Record the volume label.")
@click.pass_context
def device_config(ctx, device_ref, template, music_dir, playlist_dir, name, label):
    """Show or change how a device is laid out."""
    con = _con(ctx)
    d = _require_device(con, device_ref)
    changes = {"path_template": template, "music_dir": music_dir,
               "playlist_dir": playlist_dir, "name": name, "label": label}
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
                "path_template", "root", "device_uid"):
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
        console.print("\n[bold]playlists[/]")
        for pl in p["playlists"]:
            skip = f"  [yellow]{pl['skipped']} not in set[/]" if pl["skipped"] else ""
            console.print(f"  {pl['name']}  [dim]{len(pl['entries'])} entries[/]{skip}")
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
