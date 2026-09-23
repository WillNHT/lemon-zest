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

### The Windows executable

Download `lemon-zest-<version>-windows-x64.zip` from the
[latest release](https://github.com/WillNHT/lemon-zest/releases/latest),
extract it anywhere, and run `lemon-zest.exe` from the extracted folder. No
Python, no install step, **and nothing else to install**. Double-click it to
open the interface, or run it from a terminal to use the CLI:

```
lemon-zest.exe doctor "D:/Music"
```

A folder rather than a single .exe on purpose: everything below travels
with it, and a onefile build would unpack a quarter of a gigabyte into a
temporary directory on every launch. Keep the folder together — the .exe
on its own is not the program.

Everything downloading and identifying needs travels inside it:

| Bundled | What it is for |
| --- | --- |
| `yt-dlp` | fetching audio. The binary re-runs itself as yt-dlp, so there is no `pip install` step |
| `ffmpeg`, `ffprobe` | extracting the audio yt-dlp downloads |
| `deno` | YouTube signs its media URLs with a JavaScript challenge that has to be run; without a runtime every video fails |
| `fpcalc` | Chromaprint, for identifying a file by sound. Still needs a free AcoustID key |

That is what makes the download large — roughly 100 MB zipped, 350 MB
extracted. It starts instantly, because nothing is unpacked at launch.
`lemon-zest.exe tools` prints what it found and where each one came from.

A copy of any of these already on PATH is still preferred for yt-dlp, so
one you keep updated wins over the bundled release. `LEMONZEST_TOOLS_DIR`
points a build at binaries you supply instead of downloading them.

The binary is unsigned, so Windows SmartScreen will warn on first run;
*More info -> Run anyway* is the way past it.

### From source

Needs Python 3.10+. `ffmpeg` is optional for now (the test suite uses it to
generate audio; the MVP does no transcoding), but downloading needs it,
because that is what extracts the audio.

```bash
pip install -e .
```

Downloading from YouTube needs `yt-dlp`. A `yt-dlp` already on PATH is used
in preference to anything bundled, so it can be upgraded on its own schedule:

```bash
pip install -e ".[youtube]"     # or: pip install yt-dlp
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

### Rockbox

Pair with `--profile rockbox`. Music goes to `/Music`, playlists to their own
`/Playlists` folder, and playlist entries are written from the card's root
(`/Music/blink-182/…/Stay Together For The Kids.m4a`) rather than relative.

Rockbox also shows a full date tag as the year and draws letterboxed or
non-4:2:0 JPEG covers badly (padded, or in greyscale). New downloads are
written with the year only and a square, baseline 4:2:0 cover.

The full date is not thrown away: it goes in a tag of its own, the one
MusicBrainz Picard uses for a release date - `TDRL` in ID3, `RELEASEDATE` in
Vorbis comments, and a `----:com.apple.iTunes:RELEASEDATE` atom in MP4 - so
the year tag stays a year and a player that wants "this day, years ago" can
still find the day. A download takes it from the video's release or upload
date; an identified track from its MusicBrainz release, filling a blank only.
The catalog keeps it as `date` (ISO, `2018-02-01`), editable beside the year.

For files downloaded earlier, fix them once and sync again - `fix-tags` moves
a full date out of the year tag into the release-date tag rather than
discarding it:

```
lemon-zest fix-tags --dry-run
lemon-zest fix-tags
```

## Downloading from YouTube

```bash
# What is at this URL? Nothing is fetched.
lemon-zest download "https://www.youtube.com/watch?v=..." --info

# Fetch it into a library folder, and put it in a playlist while you are there.
lemon-zest download "https://www.youtube.com/watch?v=..." --playlist chill

# A playlist or channel URL fetches the lot.
lemon-zest download "https://www.youtube.com/playlist?list=..." --to "C:/Users/ASUS/Music/hiby/Music"
```

The file lands **inside a library folder** and is indexed on the spot, so it
is an ordinary library track immediately: tick it onto a card and sync,
with no rescan in between. `--embed-metadata` writes the source URL into the
`purl` tag, which the scanner already reads, so where a track came from
survives in the catalog.

A playlist URL becomes a playlist, written to a `Playlists` folder **beside**
the library folder - `…/Music` and `…/Playlists`, the way a Rockbox card lays
them out - with every entry named from that shared parent
(`/Music/blink-182/…/Stay Together For The Kids.m4a`). Copy both folders to
the root of a card and the playlists play as they are. A playlist an older
version wrote inside the library (`Music/playlists`, relative paths) is moved
out on the next scan.

The playlist's own picture comes with it: `Playlists/<name>.jpg`, squared and
saved as a baseline JPEG like every other cover, beside the playlist file
where the player looks for it. A sync copies it next to the playlist on the
card, and takes it off again with the playlist.

An album or EP is not a playlist. YouTube Music serves one as a playlist URL
(`list=OLAK5uy_…`, titled *Album - Dookie*); its tracks are downloaded and
filed under the album, and no playlist is made for it. Album playlists made
by earlier versions are dropped from the catalog and from beside the library.

### Where every file came from

A download is traceable from the URL somebody typed to the file it became,
however much it changed on the way:

- **the source URLs** - every URL that asked for the video, so one song in
  two playlists has both;
- **the file as it arrived** - where yt-dlp first put it and what its tags
  said, recorded once, before anything identifies or renames it;
- **the file now** - the catalog row, after enrichment, hand edits and
  `organise`.

They are tied together by the video id in the source URL yt-dlp writes into
the file (`purl`), which no tag write touches - so a video that became
*Title X by Artist C* in a different folder is still known to be the one
downloaded, and is not fetched again. The inspector shows it under **Where
it came from**, and **Identify again** can search from what the file arrived
as instead of what it says now. Files downloaded before this was recorded
get a record from what the catalog held.

### Big libraries: several at once, pause and resume

A playlist is split into its videos and fetched **three at a time** (one to
four, under *Where files land*), each yt-dlp on its own with its own
progress bar. One process at a time spent most of each item waiting - on
the page, the signature challenge, ffmpeg - with the network idle.
Identification was already off the download's path (its own queue, one
MusicBrainz request a second), so it never held a download back; it simply
finishes later.

Before anything is fetched, every video the library already has is taken
off the list - by the download archive and by the catalog, so a file
renamed or re-tagged since is still recognised as that video.

**Pause** lets the videos in hand finish and starts no more; **Stop now**
kills them. Either way the run is kept under **Paused downloads** and
**Resume** runs it again, taking only what it had not reached - and a run
cut off by closing the program is kept the same way, as *interrupted*.

Two things stop a second run re-fetching what you already have. yt-dlp keeps
a download archive at `.lemon-zest-downloads.txt` in the folder (pass
`--no-archive` to ignore it), and adding a track to a playlist it is already
in does nothing — the same rule the sync obeys, for the same reason.

### Cookies

YouTube refuses a growing share of requests from a signed-out client: age
gates, the "sign in to confirm" bot check, and members-only material. yt-dlp
can read the cookies straight out of a local Firefox profile, which needs
nothing exported and nothing kept in sync:

```bash
lemon-zest download-config                      # what it would use, and why
lemon-zest download-config --cookies firefox
lemon-zest download-config --firefox-profile "7p00fljl.default-release"
```

Profiles are found by looking for a `cookies.sqlite` on disk rather than by
reading `profiles.ini`, most recently written first — that is the profile
you are signed in to. When there is no Firefox to read, a `cookies.txt`
exported by a browser extension works instead:

```bash
lemon-zest download-config --cookies "C:/Users/ASUS/cookies.txt"
lemon-zest download-config --cookies none
```

The default, `auto`, tries Firefox, falls back to the file, and then
proceeds without cookies rather than refusing to start: plenty of videos
need none. When YouTube does refuse, the error says which of these to fix
rather than repeating yt-dlp's own wording.

### When it goes wrong

Every line yt-dlp writes is kept, along with the command that produced it -
including which cookie source was chosen - and what Lemon Zest did
afterwards: what it catalogued, and what went into a playlist. The interface
shows it live as the download runs and keeps it afterwards, failure
included; it is the one part of the app you can select and copy, because a
log exists to be pasted into a bug report. The CLI prints the tail of it
when a download fails, so a failure need not be reproduced to find out what
it said.

## Metadata quality

A file's tags are only as good as whatever wrote them, and a downloaded one
is often wrong in a specific way: with no `artist` tag to read, yt-dlp falls
back to the channel name, so a KIRINJI track lands in a folder called
"Nemu". `enrich` reconciles what a file claims with what MusicBrainz knows.

**It runs on its own.** A scan and a download each finish by identifying
whatever they brought in and writing the tags into those files — no flag, no
button, no setting. Identifying a file is part of taking it into the library,
not a chore to remember afterwards. The commands below are for the parts that
still want a person: seeing what it decided, correcting it, and telling it to
leave something alone.

It runs as its own job rather than as a tail on the scan, because MusicBrainz
allows one request a second: a scan that finishes in seconds should say so
rather than appearing to grind for an hour. Three things bound what it will do
unattended — a **skipped** file is never looked at, only a **certain** match
(exact ISRC, or a text match at or above 0.90, or a fingerprint the tags
agree with) is written to disk, and the fill-only rule and hand-typed
overrides still win. Doubtful matches wait in the review queue and no file is
rewritten for them.

```bash
# Free and offline: copy ISRCs onto untagged twins already in the library.
lemon-zest enrich backfill

# Look the rest up. One request per second, as MusicBrainz asks.
lemon-zest enrich run

# What it was not sure about, least confident first.
lemon-zest enrich review
lemon-zest enrich accept 41
lemon-zest enrich reject 42

# Say it yourself. Outranks every source, now and on every later run.
lemon-zest enrich set 43 --album "Kirinji" --artist "KIRINJI"

# Leave a track out of it entirely. Never looked up again until you unskip.
lemon-zest enrich skip 44 45
lemon-zest enrich unskip 44

# How much of the library is identified.
lemon-zest enrich status
lemon-zest enrich states
```

### The four states

Every file is in exactly one of them, and the Music page shows which in a
column of its own.

| State | Means | What a run does with it |
| --- | --- | --- |
| **raw** | never looked up, or a lookup that came back empty | picks it up |
| **awaiting review** | a match is stored and wants your decision | leaves it alone |
| **enriched** | identified - matched automatically, accepted, or marked enriched by hand | leaves it alone unless you ask again |
| **skipped** | deliberately excluded | **never** looks at it, even with `--redo`, even when you select it by hand |

`skipped` is the one with teeth. A live bootleg MusicBrainz will never have,
a podcast episode, a file whose tags are right and whose match keeps coming
back wrong — mark it and it stops costing a request forever. Rejecting a
proposal lands in the same place, because "not this one" has always meant
"stop asking". `enrich unskip`, or **Mark raw** in the interface, is the way
back, and it forgets the stored answer as well as the state.

Four rungs, cheapest first. **Backfill** matches a file with no ISRC against
one that has an ISRC on folded artist, folded title and a duration inside two
seconds — no network at all, and it promotes files onto the rung above.
**ISRC lookup** is an exact identifier, so a hit is certain. **Text search**
is fuzzy, scored on title, artist and a duration window; above 0.90 it is
applied, between 0.62 and 0.90 it waits for you, below that it is discarded.

**Fingerprinting** is the last rung, and the only source that ignores what a
file claims and listens to it instead — which is the whole of the untagged
download case. It is opt-in, because it needs two things this repo does not
ship:

```bash
winget install AcoustID.Chromaprint          # fpcalc, found on PATH
lemon-zest enrich config --acoustid-key <key>   # free: acoustid.org/new-application
lemon-zest enrich run --fingerprint
```

The packaged executable already carries `fpcalc`, so on Windows only the
key is missing there. From source it is located on PATH, and dropping
`fpcalc.exe` beside `lemon-zest.exe` works too. `enrich config` says what is
missing, and `lemon-zest tools` says what was found.

How much of a fingerprint match is applied still depends on the tags, even
though getting there did not. Audio and tags agreeing is the strongest
evidence available, and is applied. Audio alone goes to the review queue —
"trust the sound over the tag" is a judgement about your library, not a fact.

Four rules make it safe to run over a library you care about:

- **Nothing is overwritten in place.** The proposal lives in its own table
  with its source, its confidence and its date, so a wrong answer is
  reversible and auditable.
- **A release-derived field only fills a blank.** Identifying a *recording*
  and choosing which of its forty *releases* this copy came from are
  different questions with very different certainties, and one confidence
  score describes only the first. Without this rule a correctly tagged
  "Cigarettes After Sex" track, matched with total confidence to its own
  recording, gets relabelled with the HBO soundtrack that recording also
  appears on. Title, artist and ISRC come from the recording and are taken;
  album, year and track number are filled in only where the file was silent.
- **A hand-typed value wins.** `enrich set` outranks every source, survives a
  re-run, and is re-applied after any later match. Typing a value does not
  make a file *enriched*: correcting a spelling is not identifying a
  recording. Only a lookup, an accepted match, or **Mark enriched** does.
- **Audio files are not touched** unless you pass `--write-tags`, which asks
  first. When you do, each file is rewritten to a copy and swapped in, so an
  interruption leaves the original — and the content key is recomputed in the
  same transaction, or the next scan would see every corrected file as new
  and the card would recopy the lot.

`--write-tags --artwork` also replaces the embedded cover with the release's
own front cover from the Cover Art Archive. That is worth knowing about: the
artwork a download embeds is whatever yt-dlp scraped, which for one of
YouTube's auto-generated art tracks is the real square cover and for an
ordinary upload is a 16:9 video frame.

Everything is keyed by content key rather than by track id, so moving or
rescanning a file keeps its enrichment and costs no further lookups.

One more rule, learned the hard way. **A stored path is resolved to the
spelling the filesystem actually has** before any file is opened. The catalog
stores NFC, because a path needs one spelling to be comparable; NTFS keeps
whatever bytes wrote the file, and a yt-dlp download of a Japanese title
arrives as NFD — `か` plus a combining dakuten where the catalog holds the
single character `が`. The two read identically and open differently, so
without this a write fails with *no such file* over a file that is plainly
sitting there. `paths.resolve_existing` tries the whole path in both
normalisations and then walks it component by component, which is what a
hand-made folder holding a downloaded file needs.

### From the interface

The same four states, the same rules, on the **Music** page — where picking
which tracks to identify is much easier than typing their ids.

Rows select the way a file manager's do: click one, shift-click for a run,
ctrl-click (cmd on a Mac) to add one, `ctrl+A` for the page, and **Select all
N matching** for everything behind the current filter rather than everything
on screen. Arrow keys walk the list, shift extends. With something selected:

| Key | Does |
| --- | --- |
| `E` | look the selection up |
| `A` / `R` | accept or reject the stored match |
| `S` | skip |
| `U` | mark raw |
| `N` | mark enriched - "this file is right as it is" |
| `Enter` | edit the metadata by hand |
| `W` | write the tags into the files |
| `Esc` | clear the selection |

Enrichment is automatic, so most of the time there is nothing to press: the
selection is for asking again, correcting by hand, or skipping. The
**Metadata** chips above the table filter to one state, so "show me the 23
awaiting review" is one click.

**Enrich** on a single track shows the terms the lookup will send — artist,
title, album — and lets them be typed over. That box is the answer to the
match that found nothing: MusicBrainz has *Song*, the file is called *Song
(Single Version) [Official Video]*, and no amount of scoring recovers that.
Edited terms are searched exactly as typed, once, and the results are judged
against them rather than against the file's own tags; the automatic
rewrites, and the ISRC shortcut, are both skipped, because they exist to
guess and you have just said what the answer is. Over a selection the box
is not offered: one query cannot describe forty tracks.

Genre comes from the release when the file has none — MusicBrainz records it
per release and per release group, and the votes decide - and from the
artist when the release has no votes, which is most singles. It is only
asked for when the tag is empty, so a file that already says *City Pop*
keeps it and costs no extra request. A YouTube *category* - "Music",
"People & Blogs", which yt-dlp used to write into the genre tag - is not a
genre and counts as empty; downloads no longer carry one. **Utilities > Fill
in what is missing > Genres** (or `lemon-zest enrich genres`) fills the
tracks identified before this.

Lyrics come from [LRCLIB](https://lrclib.net), a free lyrics database that
needs no key, and go **into the file** - `©lyr` in MP4, `USLT` in ID3,
`LYRICS` in Vorbis comments - time-synced (LRC) when LRCLIB has them, plain
when it does not. They are fetched when an identified track's tags are
written, so every download that is identified gets them without being asked;
**Utilities > Fill in what is missing > Lyrics** (or `lemon-zest lyrics`)
fills the rest of the library. A file that already has lyrics is left alone.

**Edit metadata** opens on one track with three tiers side by side — what the
catalog says now, what the source proposed (click a proposal to drop it into
the box), and anything already typed by hand. On a selection of many it edits
one field across all of them: filling in only *Album artist* fixes a folder
full of tracks without flattening their titles to one value. An empty box
means "leave this alone" over a selection, and "forget what I typed" on a
single track.

**Write tags to files** is the only button on that page that touches your
audio. Everything else edits the catalog and can be undone by clicking the
other button; that one cannot, so it asks separately — and it shows the exact
diff first: every field, what the file holds now, what it would become. If
nothing would change, or the files are not where the catalog thinks, or the
cover box has no release to fetch from, the dialog says so instead of running
and reporting a zero.

Every run leaves a result on the page until it is dismissed — what was
enriched, what is waiting, what failed and *why*. A lookup that comes back
empty says so; if the reason is that fingerprinting is not set up, it says
that too, because a channel-name artist gives a text search nothing to match.

### Making the folders agree

`enrich` fixes what the catalog believes. `organise` fixes what is on disk —
the folder still named after seven credited writers, because that was the
only thing yt-dlp had to build a path from.

```bash
lemon-zest organise plan  "C:/Users/nhti/Music/nhaccuatui/music"
lemon-zest organise apply "C:/Users/nhti/Music/nhaccuatui/music"
lemon-zest organise undo          # lists past runs
lemon-zest organise undo organise-1789012345.json
```

```
Joji, Kurtis McKenzie, Linden Jay, Chelsea Lena, George Miller, …/Nectar/Like You Do.m4a
  -> Joji/Nectar/Like You Do.m4a

3 to move, 2 skipped for having no artist to file them under
```

It plans by default and asks before it moves anything, because this is the
most destructive thing the program can be asked to do. Every run writes a
journal that `organise undo` replays backwards.

The default template changes **folders only** and leaves every filename
exactly as it is — the two halves of this library spell filenames
differently, one with track numbers and one without, so renaming them too is
a much larger diff than the problem calls for. `--template` if you want it.

Two refusals worth knowing about. A track with no artist is left where it is,
and so is any move whose destination would contain `Unknown Album` or
`Unknown Artist`: a folder reading *someday you'll wake up, and you'll be 26*
carries more than one reading *Unknown Album*, and tidiness that destroys
information is not tidiness.

Moving a file does not change its content key — the bytes did not change — so
the device manifest still matches and a replug stays a no-op. A device whose
template mirrors the library layout (`{rel_path}`) is named before anything
moves, because its next sync will move the same files on the card.

## Moving the library to another computer

**Copy the library folder.** Each one carries a copy of the catalog in a
hidden `.lemon-zest` folder, refreshed after every scan and download - the
tracks, the playlists, what was identified and typed, where every download
came from, and the downloads still paused - so the folder is all there is to
take. Copy the `Playlists` folder beside it too. Part-downloaded files
(`.lz-incomplete`) and the download archive are inside it already.

On the other computer, **Utilities > Move to another computer**, point at
where the folder landed, and **Open it**. The catalog comes in and every
path in it is moved from where the folder was to where it is now - a library
at `C:\Users\Bob\Music\lemon-zest` on one machine and
`C:\Users\Remote\Music\library\master` on the other is the case it is for.

```bash
lemon-zest pack                       # refresh the copy now
lemon-zest unpack "C:/Users/Remote/Music/library/master"
lemon-zest relocate "D:/old/Music" "E:/Music"   # moved on this machine
```

What stays behind on purpose: cookies, the Firefox profile, the AcoustID key
and the MusicBrainz contact are never written into the copy, and opening one
keeps this machine's own. The program itself is not in the folder either -
any release will open it. A folder that has moved on the same machine shows
as **not found** under *Add music*, with a box to say where it is now.

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

**Download** fetches audio from YouTube into a library folder, says up front
which cookies it will use, reports what it added, and streams the whole run
into a log you can select and copy. A batch is shown as a batch: the URLs
are listed before anything is fetched, so the bar has a denominator from
the first second — *12 of 47*, how many were downloaded, how many the
archive already had, how many failed, how long it has taken and roughly how
long is left, with the track being fetched right now and its own bytes
underneath. The status strip carries the same count on every page, so a
long playlist can be watched from the library. **Needs attention**
collects everything worth a second look in one place:
empty files (zero bytes on disk - failed downloads, which Lemon Zest refuses to
copy rather than putting dead entries on the card), untagged files, and
playlist entries that resolve to nothing.

**Inbox** is what a library page cannot be: everything the catalog has
indexed since you last emptied it, newest first, with when each file
arrived. Downloads and scanned files land there, automatic identification
runs on them as they arrive, and the inbox is where you see what it did -
six tracks that came in this morning are otherwise six rows in the middle
of an alphabet. *Mark all as seen* moves a watermark; it deletes nothing
and no row leaves the library.

**Not in a playlist** lists every track no playlist holds - the ones nothing
carries to a player, and so the ones that get lost. A playlist that holds
everything (a *DAP-master*) would answer the question for every track at
once, so any playlist can be set aside there and stop counting; the choice
is remembered.

Clicking a row opens an inspector beside the table: the artwork the file
itself carries (a downloaded video frame included, which is the reason to
look), every field including the ones the table has no room for, the full
path, when it arrived and when the file was last modified, and where its
values came from. The **Updated** column says when the catalog's view of a
track last changed - a tag, the file's bytes, or where it lives - which a
database trigger keeps, so every path that edits a track counts.

**Scan & import** lists the library folders. Each one can be rescanned,
**hidden** - still indexed, but out of the library, the facets and the
inbox - or **removed**, which forgets its rows. Neither deletes a file, and
a removed folder comes back by scanning it again.

## What it guarantees

Each of these is covered by a test in `tests/test_sync.py`; `tests/test_server.py`
covers the API the interface runs on, and `tests/test_download.py` the
download path against a stub yt-dlp, `tests/test_enrich_states.py` the four
states and the endpoints the Music page drives them with (no ffmpeg needed),
`tests/test_paths_unicode.py` the NFC/NFD path resolution,
and `tests/test_enrich.py` the metadata
ladder against a stub MusicBrainz and `tests/test_organise.py` the file moves.
137 tests, no network, no real card:

| | |
|---|---|
| Replug is free | A card that is already correct produces an empty plan and copies nothing. |
| Playlists are rewritten | Syncing five times leaves a 3-track playlist at 3 tracks. |
| Nothing is left half-written | Copies land on a temp name and are renamed; an interrupted sync leaves no partial file and resumes where it stopped. |
| Removals are exact | Unticking removes those files and that playlist file, and nothing else. |
| It refuses rather than fills | A set larger than the free space is rejected at plan time, before a byte moves. |
| Writes stay on the device | Any destination that escapes the device root is refused. |
| A download is a library file | What yt-dlp writes is indexed on the spot, into the folder the scanner watches, and adding it to a playlist twice adds one entry. |
| A dead video is not a dead run | yt-dlp exiting non-zero after fetching some of a playlist keeps what arrived; a run that fetched nothing raises, naming the cookie fix when that is the cause. |
| Provenance survives | `#Collection URI` and per-track `#Apple Music URI` comments are read, stored, and written back out. |
| A right album is not overwritten by a guess | A recording matched with confidence 1.00 whose best release is a soundtrack leaves a correctly tagged album alone, and still takes the recording's own ISRC. |
| A correction outranks the source | `enrich set` survives a later match that disagrees with it. |
| A skip is honoured everywhere | A skipped file is not looked up by `enrich run`, not by `--redo`, not by selecting it in the interface and pressing Enrich, and not by the automatic pass after a scan. |
| Automation writes only what is certain | The pass that follows a scan writes tags for `applied` matches only; a candidate waits for a person and no file is touched for it. |
| A scan survives a dead service | MusicBrainz being down is reported by the enrichment job, and never raised into the scan that started it. |
| A failure says why | A run that could not reach MusicBrainz, or a file that could not be written, reports the reason, not just a count. |
| A stored path finds its file | An NFC path resolves an NFD file on disk and the reverse, including a folder and filename in different normalisations. |
| A tag write is atomic | A failed write leaves the original file byte-for-byte; a successful one moves the content key, so a rescan reports no change and the card recopies nothing. |
| One bad lookup is not a bad run | An isolated request failure is skipped and counted; three in a row stop the run. |
| A placeholder is not a destination | `organise` refuses to move a file into `Unknown Album`, and refuses `Various Artists` as an album artist. |
| A move is reversible | Every `organise` run writes a journal; undoing it puts every file back and leaves the catalog matching the disk. |
| A move does not recopy the card | The content key survives a move, so the device manifest still matches and a replug stays a no-op. |

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
| `download.py` | yt-dlp as a subprocess: cookies, progress, indexing |
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
importing a streaming service's own library, iTunesDB for stock-firmware
iPods, and playback. Downloading is in: it lands files in the library and
then gets out of the way.

The interface covers the library, playlists, devices, syncing and
downloading. It does not yet have the loudness screen from the prototype,
because there is nothing behind it to show. Nothing in the core knows the
interface exists — the planner and executor communicate through plain dicts
and an event callback — so a Tauri or Electron shell could replace the
browser later without touching them.

## Releasing

The version number is not typed anywhere by hand. Every commit subject
follows [Conventional Commits](https://www.conventionalcommits.org):

```
feat(playlists): write playlists under the device's own filename
fix(sync): resolve a destination's on-disk spelling before writing it
docs: how a device's playlist naming is detected and honoured
```

CI rejects a pull request whose commits do not parse. On a push to `master`,
[python-semantic-release](https://python-semantic-release.readthedocs.io)
reads the subjects since the last tag and derives the next
[semantic version](https://semver.org): a `fix` bumps the patch, a `feat`
the minor, a `!` or a `BREAKING CHANGE:` footer the major, and a history of
nothing but `docs`/`chore`/`ci` releases nothing at all. It then bumps both
copies of the number, writes the changelog, tags, and opens the GitHub
release. A second job builds from that tag, smoke-tests the result, zips it
and attaches the archive.

To build it yourself:

```bash
pip install -e . -r packaging/requirements-build.txt
pyinstaller --clean --noconfirm packaging/lemon-zest.spec
python packaging/smoke_test.py dist/lemon-zest/lemon-zest.exe
python packaging/make_zip.py
```

The build downloads ffmpeg, Deno and fpcalc into `build/tools` and folds
them in; they are cached under `build/tools-cache`, so only the first build
pays for them. `LEMONZEST_TOOLS_DIR=/somewhere` uses binaries you supply
instead, for a build that cannot reach the network. The smoke test runs the
result with PATH stripped to the system directories, so a machine that
happens to have ffmpeg installed cannot make a broken bundle look fine.
