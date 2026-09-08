# Lemon Zest

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
lemon-zest doctor "C:/Users/ASUS/Music/hiby/Music"
```

```
23 playlists - 19,449 entries, 1,089 unique, 18,360 repeats, 0 dead paths
these playlists have grown 17.9x - each sync appended instead of replacing.
```

Lemon Zest rewrites playlists in place, so syncing once collapses them back.

## Install

Needs Python 3.10+. `ffmpeg` is optional for now (the test suite uses it to
generate audio; the MVP does no transcoding).

```bash
pip install -e .
```

Or run it without installing:

```bash
python lemon-zest.py --help
```

## Use

```bash
# 1. Index the library. Read-only; it never touches your music files.
lemon-zest scan "C:/Users/ASUS/Music/hiby/Music"

# 2. Import playlists. Run this for each folder that has them - the second
#    import enriches the first rather than overwriting it, so the copies
#    that resolve and the copies that carry Apple Music URIs both count.
lemon-zest playlist import "C:/Users/ASUS/Music/hiby/playlist_data"
lemon-zest playlist import "C:/Users/ASUS/Music/hiby/playlist_data/Playlists_hiby"

# 3. Pair the card.
lemon-zest device detect
lemon-zest device add E:/ --name "HiBy R1" --profile hiby

# 4. Learn how this player names its own playlist files, so a sync replaces
#    them instead of adding a second copy beside each one.
lemon-zest device config "HiBy R1" --detect-playlists

# 5. Say what goes on it.
lemon-zest device set "HiBy R1" --playlist chill --playlist angsty --artist Radiohead

# 6. Look before you leap, then sync.
lemon-zest plan "HiBy R1"
lemon-zest sync "HiBy R1"
```

Other commands: `lemon-zest stats`, `lemon-zest device list`, `lemon-zest playlist list`,
`lemon-zest playlist unmatched`, `lemon-zest log`, `lemon-zest doctor <folder>`.

## The interface

```bash
lemon-zest gui
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
empty files (zero bytes on disk - failed downloads, which Lemon Zest refuses to
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

A device is matched by **volume label**, with a `.lemon-zest-id` marker file at
the card root as the tiebreaker. Cards paired before the project was renamed
carry a `.hoard-id`; that file is still read and the id inside it is kept, so
a rename never re-pairs a card or re-copies its contents. That is enough for a personal fleet and
needs no native USB APIs. Give each card a distinct label; the marker keeps
two cards apart even when the labels collide.

### Playlist filenames

A player names its playlists its own way. HiBy writes
`chill-Tiến Nguyễn Hữu.m3u8`; something writing plain `chill.m3u8`
onto that card does not replace the file the player reads, it adds a second
one, and the player then lists both. So each device stores a
`playlist_template` in which `{name}` is the playlist and everything around
it is the device's spelling.

`device config --detect-playlists` reads the card and works the template out
from the files already there, matching the longest playlist name first so
that `chill` cannot claim the file belonging to `chill-archive`. When
nothing on the card carries a name the catalog knows, it declines to guess
and leaves the stored template alone.

The dry run reports this either way: every playlist line names the file it
will land on and whether that replaces something, and any playlist file the
sync would leave behind is listed separately. A dry run that reports only
what it writes cannot tell you what survives next to it.

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
