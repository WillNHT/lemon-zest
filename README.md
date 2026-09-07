# Hoard

Sync a local music library to portable players — DAPs, Rockboxed players,
plain USB drives — where each device keeps its own independent set.

This is the MVP: the core engine, a CLI, and a local interface. It does the
thing the whole project is for, which is to put the right files on a card
**correctly, repeatably, and without breaking what is already there.**

## Why it exists

The playlists on the card this was built for had been growing for eighteen
syncs. `chill` was 101 tracks stored as 1,817 lines; across 23 playlists,
1,089 real entries had become 19,449. Every sync had merged the list in
again instead of replacing it.

```
hoard doctor "C:/Users/ASUS/Music/hiby/Music"
```

```
23 playlists - 19,449 entries, 1,089 unique, 18,360 repeats, 0 dead paths
these playlists have grown 17.9x - each sync appended instead of replacing.
```

Hoard rewrites playlists in place, so syncing once collapses them back.

## Install

Needs Python 3.10+. `ffmpeg` is optional for now (the test suite uses it to
generate audio; the MVP does no transcoding).

```bash
pip install -e .
```

Or run it without installing:

```bash
python hoard-cli.py --help
```

## Use

```bash
# 1. Index the library. Read-only; it never touches your music files.
hoard scan "C:/Users/ASUS/Music/hiby/Music"

# 2. Import playlists. Run this for each folder that has them - the second
#    import enriches the first rather than overwriting it, so the copies
#    that resolve and the copies that carry Apple Music URIs both count.
hoard playlist import "C:/Users/ASUS/Music/hiby/playlist_data"
hoard playlist import "C:/Users/ASUS/Music/hiby/playlist_data/Playlists_hiby"

# 3. Pair the card.
hoard device detect
hoard device add E:/ --name "HiBy R1" --profile hiby

# 4. Say what goes on it.
hoard device set "HiBy R1" --playlist chill --playlist angsty --artist Radiohead

# 5. Look before you leap, then sync.
hoard plan "HiBy R1"
hoard sync "HiBy R1"
```

Other commands: `hoard stats`, `hoard device list`, `hoard playlist list`,
`hoard playlist unmatched`, `hoard log`, `hoard doctor <folder>`.

## The interface

```bash
hoard gui
```

Opens `http://127.0.0.1:7777` in your browser. Everything above is there:
browse and filter the library through a three-pane genre/artist/album
browser, see at a glance which tracks are already on the selected device,
tick playlists onto a device, dry-run, and sync with live progress.

It is served by the same process that owns the catalog, with no build step,
no `npm install`, and no CDN - so it works with the network unplugged. Long
jobs run on a worker thread and are reported through a job registry, so a
sync keeps going if you reload the page, and the catalog stays readable
while it writes (SQLite in WAL mode).

**Needs attention** collects everything worth a second look in one place:
empty files (zero bytes on disk - failed downloads, which Hoard refuses to
copy rather than putting dead entries on the card), untagged files, and
playlist entries that resolve to nothing.

## What it guarantees

Each of these is covered by a test in `tests/test_sync.py`; `tests/test_server.py`
covers the API the interface runs on. 25 tests, no network, no real card:

| | |
|---|---|
| Replug is free | A card that is already correct produces an empty plan and copies nothing. |
| Playlists are rewritten | Syncing five times leaves a 3-track playlist at 3 tracks. |
| Nothing is left half-written | Copies land on a temp name and are renamed; an interrupted sync leaves no partial file and resumes where it stopped. |
| Removals are exact | Unticking removes those files and that playlist file, and nothing else. |
| It refuses rather than fills | A set larger than the free space is rejected at plan time, before a byte moves. |
| Writes stay on the device | Any destination that escapes the device root is refused. |
| Provenance survives | `#Collection URI` and per-track `#Apple Music URI` comments are read, stored, and written back out. |

```bash
python -m unittest discover -s tests -v
```

## How it works

A sync is a diff over three collections. Keeping them apart is what makes a
replug a no-op instead of a re-copy.

- **desired** — what you ticked (`device_set`). Intent only, no paths.
- **planned** — desired run through the device profile: destination paths,
  playlists, bytes required.
- **observed** — what is physically on the card (`device_manifest`,
  confirmed against a directory listing).

`planned - observed` is the work. `observed - planned` is the deletions.
The manifest stores the **source** content key, so an unchanged library and
an intact card produce no work at all.

| Module | Role |
|---|---|
| `db.py` | SQLite schema; WAL; single writer |
| `meta.py` | Tag reading via mutagen, content keys |
| `paths.py` | Unicode normalisation, FAT-safe names, destination templating |
| `scan.py` | Library walk and incremental upsert |
| `playlists.py` | m3u8 read/write, both dialects |
| `devices.py` | Volume detection, profiles, pairing |
| `planner.py` | The three-set diff. Pure; touches nothing |
| `executor.py` | Carries out a plan, safely and resumably |
| `cli.py` | Commands |
| `server.py` | JSON API and job registry for the interface |
| `web/` | The interface: one HTML file, one stylesheet, one script |

The planner is deliberately side-effect free: it is both the dry run and the
easiest part to test, which is why it was built first.

### Device identity

A device is matched by **volume label**, with a `.hoard-id` marker file at
the card root as the tiebreaker. That is enough for a personal fleet and
needs no native USB APIs. Give each card a distinct label; the marker keeps
two cards apart even when the labels collide.

### Path handling

Two hazards, both hit in real use:

- **Unicode normalisation.** Vietnamese filenames round-trip as NFC on one
  filesystem and NFD on another, so a literal compare reports a missing file
  that is plainly there. Everything stored or compared is normalised, and
  lookups try both forms. Playlist *names* too — otherwise one playlist
  becomes two rows that look identical in every listing.
- **FAT32/exFAT naming.** Illegal characters, trailing dots and spaces,
  reserved names, and case-insensitive collisions are all handled by
  `paths.py` before anything is written.

## Not in the MVP

Deliberately, per the build plan: transcoding (this library is uniformly
AAC, so there is nothing to convert yet), loudness analysis and ReplayGain,
streaming imports, iTunesDB for stock-firmware iPods, and playback.

The interface covers the library, playlists, devices and syncing. It does
not yet have the loudness or import screens from the prototype, because
there is nothing behind them to show. Nothing in the core knows the
interface exists — the planner and executor communicate through plain dicts
and an event callback — so a Tauri or Electron shell could replace the
browser later without touching them.
