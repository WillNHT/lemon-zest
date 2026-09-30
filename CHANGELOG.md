# CHANGELOG


## v0.11.1 (2026-09-30)

### Bug Fixes

- **download**: Bundle yt-dlp's challenge solver scripts in the executable
  ([`b2860e9`](https://github.com/WillNHT/lemon-zest/commit/b2860e9524663c6732caca2f35784bdf1c75d853))

The release build installed plain yt-dlp, which leaves out yt-dlp-ejs: the solver scripts the
  JavaScript runtime runs. The packaged Deno had nothing to execute, every YouTube download failed,
  and the hint blamed a missing runtime, so installing Deno or Node changed nothing.

Install yt-dlp[default], refuse to build without yt-dlp-ejs, check for the scripts in the smoke
  test, and explain the failure accurately.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>


## v0.11.0 (2026-09-25)

### Continuous Integration

- Build and smoke-test the executable without uploading it
  ([`4f31974`](https://github.com/WillNHT/lemon-zest/commit/4f31974ce242ea28ddf25aaa9f254f5548d9dbda))

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **loudness**: Normalize volume with ReplayGain tags on download and on request
  ([`fe57598`](https://github.com/WillNHT/lemon-zest/commit/fe57598f9b38d9d5545a9ca5fb0d7d6790b135f1))

Measure each track with ffmpeg's EBU R128 meter and write replaygain_track_gain/peak (MP4 freeform,
  ID3 TXXX, Vorbis comment), which the Rockbox fork applies by default. Audio is never re-encoded.
  New downloads are tagged on arrival; a Volume maintenance button tags the rest.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>


## v0.10.0 (2026-09-25)

### Bug Fixes

- **dedupe**: Count songs, not files, and follow versions into playlists
  ([`5e1e49c`](https://github.com/WillNHT/lemon-zest/commit/5e1e49cd29885400eac289c241f7b4c6963745e1))

The sidebar counts skip versions set aside behind a master, "not in a playlist" treats a master as
  listed when one of its versions is, and the inspector's "as seen in" lists the playlists that
  reach a track through its versions. The suggested master is the earliest release, so the album cut
  wins over the holiday album and the best-of; the duplicates table shows every value in full.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **dedupe**: Group versions of a song under a master file
  ([`97a3b4e`](https://github.com/WillNHT/lemon-zest/commit/97a3b4e2f28835d8627f3cdb0ac1784217850afb))

Adds work/work_member/dup_dismissed tables and a track_canon view that resolves every track to its
  work's master. Membership is keyed by content_key and follows tag rewrites through tags.rekey.
  dedupe.py offers merge, set_master, unmerge and canon_ids; nothing touches files.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

- **dedupe**: Playlists, device sync and library resolve to the master
  ([`078b98e`](https://github.com/WillNHT/lemon-zest/commit/078b98e2927e879fe2c9ab6abbe653acd5054002))

Every reader goes through track_canon: device rules and playlists put a merged song on the card
  once, as its master; the PC-side playlist file names the master's file; append_tracks treats
  another version of a song already present as present. Reading a written playlist back keeps each
  entry on the version it was made from, so unmerge restores it.

The library list hides non-master versions unless ?versions=1, and shows a ×N badge on songs held as
  several files.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

- **dedupe**: Suggest duplicates, merge from the UI and CLI, pick values per version
  ([`f754977`](https://github.com/WillNHT/lemon-zest/commit/f754977723e5181c44d37ae63f575b41b59983af))

Suggestions come from same source video, ISRC, matched recording, or the same title and length (with
  or without the same artist, which is how a pseudonym re-release shows up). Turned-down pairs are
  remembered. Merged songs gain a Versions panel in the inspector; the Duplicates page lets you
  choose the master and, per field, which version each value (or the cover) comes from. Picks are
  recorded with the version they came from.

Downloads never fetch a video already in media, even after its file is gone, resolve playlist order
  to masters, and report arrivals that look like a song already held without merging them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>


## v0.9.0 (2026-09-24)

### Bug Fixes

- **download**: Youtube Music albums and EPs are not playlists
  ([`8b117fc`](https://github.com/WillNHT/lemon-zest/commit/8b117fcc1ab254b15695728056b23b21e6c0a247))

- **enrich**: Real genres instead of YouTube categories
  ([`1f69a46`](https://github.com/WillNHT/lemon-zest/commit/1f69a4692ebc5e321856243475ae335efdb22134))

- **enrich**: Typing metadata no longer marks a file enriched
  ([`c49a95c`](https://github.com/WillNHT/lemon-zest/commit/c49a95c1618581ec7dafa4e1e31dcf855099f14b))

- **playlists**: Write library playlists beside the library, from the card root
  ([`c02dc17`](https://github.com/WillNHT/lemon-zest/commit/c02dc17ee04c029ab87b92dba1f86cdac8a371c0))

### Continuous Integration

- Keep build artifacts for a day, and only the newest three
  ([`b5362a9`](https://github.com/WillNHT/lemon-zest/commit/b5362a9429df7084e64941ae91d58c0a366f950e))

Each CI build uploads a ~190 MB zip, and a week of pushes filled the repository's artifact storage
  so every later upload failed. Uploads now expire after one day, and a step after the upload
  deletes all but the three newest lemon-zest-* artifacts. Releases carry their own zip and are
  unaffected.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### Features

- **download**: Fetch several at once, with pause, stop and resume
  ([`26011da`](https://github.com/WillNHT/lemon-zest/commit/26011da8433ed2de98319b65de961980490d5a3f))

- **download**: Trace every download from its source URL to the file it became
  ([`82138cc`](https://github.com/WillNHT/lemon-zest/commit/82138ccf72da465b3542d5b1ebe19639d712a2a3))

A media record per video keeps what yt-dlp wrote before anything changed it: the video URL, the
  folder and path it first landed at, and the tags it arrived with. media_source keeps every URL
  that asked for it, so one video in two playlists has both. The track links to it through
  source_id, the video id read from the purl tag yt-dlp embeds - which no tag write touches, so the
  link survives enrichment, hand edits and organise, and a file whose name, tags and folder all
  changed is still recognised and not downloaded again.

The inspector shows where a track came from and what it was on arrival, and Identify again can
  search from the arrival tags. Downloads made before this get a record from what the catalog held.

Closes #24

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

- **library**: Move a library to another computer by copying its folder
  ([`7c27f04`](https://github.com/WillNHT/lemon-zest/commit/7c27f04d08d236682be020382d8694ed1f08aa41))

Each library folder now carries a copy of the catalog in .lemon-zest/, refreshed after every scan
  and download (and after a reset), so the folder is the thing to copy: music, playlists,
  identifications, typed values, download history and paused downloads travel with it. On the other
  computer, Utilities > Move to another computer (or lemon-zest unpack) takes the copy in and
  relocates every stored path from where the folder was to where it is. Credentials - cookies, the
  Firefox profile, the AcoustID key, the MusicBrainz contact - are never written into the copy, and
  the receiving machine keeps its own.

relocate (lemon-zest relocate, or "not found > Point here" under Add music) rewrites paths for a
  folder moved on the same machine; rows a scan already made at the new place give way to the older
  ones, which carry the playlists and rules.

Closes #25

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

- **lyrics**: Embed lyrics from LRCLIB
  ([`d08068c`](https://github.com/WillNHT/lemon-zest/commit/d08068c90f36f912e96b6b8121aecbd6f5225031))

- **playlists**: Save and sync playlist covers
  ([`0a3362a`](https://github.com/WillNHT/lemon-zest/commit/0a3362a3975684ba725bd090f387b15821b7f145))

- **tags**: Keep the full release date beside a year-only year
  ([`62b22f0`](https://github.com/WillNHT/lemon-zest/commit/62b22f07e41b4c98873bf5b3e2326344fddff858))

- **ui**: List tracks that are in no playlist
  ([`0b674a8`](https://github.com/WillNHT/lemon-zest/commit/0b674a82da3db57dc330cc52bdaf54b88c69f0ae))

- **ui**: Updated column showing when a track last changed
  ([`e2812d4`](https://github.com/WillNHT/lemon-zest/commit/e2812d4b0bbdff54fe536a282634957a90e431d7))

### Testing

- **download**: Stop two download tests racing the worker pool
  ([`c63e0b6`](https://github.com/WillNHT/lemon-zest/commit/c63e0b6b56433eaf84afd9c8e484b6ed10e4541b))

With several workers either item can arrive first, so a stop can catch either one unwritten: the
  stop test now checks that every item is either downloaded or owed to a resume, not which. The
  output-timing test measured the "batch:" line the engine now writes before any child starts, and
  an absolute one-second bound that a loaded machine misses; it now waits for the child's own line
  and checks it arrived well before the child exited.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>


## v0.8.0 (2026-09-17)

### Features

- **ui**: Utilities page with library reset
  ([`c5c1265`](https://github.com/WillNHT/lemon-zest/commit/c5c1265005a0c66138197500878d5ad0520a2bd4))

Adds a Tools > Utilities page whose "Reset library" action forgets every track, playlist,
  identification and the sync list, and removes the yt-dlp download archive so the same videos can
  be fetched again. Deleting the audio files from disk is an opt-in checkbox. Library folders,
  devices, download settings and the saved URL list are kept.

Guarded by a typed RESET confirmation in the page and in the API, and refused while a job is
  running.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.7.1 (2026-09-17)

### Bug Fixes

- **sync**: Write Rockbox playlist entries from the card root
  ([`75146f7`](https://github.com/WillNHT/lemon-zest/commit/75146f7a17715bc2baa11c7f4116f22766f589cc))

Rockbox playlists now list tracks as /Music/<artist>/<album>/<file> instead of paths relative to the
  playlist folder, and keep living in their own /Playlists folder.

Closes #11

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **tags**: Year-only dates and square colour covers for Rockbox
  ([`e08d62f`](https://github.com/WillNHT/lemon-zest/commit/e08d62f032685300a5e1d6062a51a518edeb9329))

Rockbox showed the upload date (20180201) as the year, and drew YouTube thumbnails letterboxed and
  in greyscale: its JPEG decoder only renders colour for baseline 4:2:0/4:2:2 files.

- downloads keep only the year and convert the thumbnail to a centre square, baseline 4:2:0 JPEG -
  every tag write trims the year and normalises the cover the same way - new fix-tags command
  repairs files downloaded before this change

Closes #10

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.7.0 (2026-09-10)

### Features

- **app**: One identification queue, and a library you can arrange
  ([`a31c345`](https://github.com/WillNHT/lemon-zest/commit/a31c345fa0bc6e3a4f2b64373ae3b09993acb147))

Identification is the only part of Lemon Zest that talks to somebody else's service, and MusicBrainz
  asks for one request a second from one client. That rule was kept by each caller separately - a
  download built a client and paced itself, a scan built another and paced itself - so two of them
  running at once politely went twice as fast as allowed. A 257-item download came back with a run
  of 503s and a handful of files left unidentified for nobody to notice.

There is now one queue, one worker and one client, per catalog. Not a policy the callers agree to
  follow: nothing else holds a client, so there is never more than one request in flight. Retries
  live there too - a 503 is almost always the service being busy for a minute, so the item goes back
  with a longer fuse rather than being dropped on the floor. Manual work jumps the queue, because a
  person clicking Identify is watching and a sweep over two thousand files is not. The client reads
  Retry-After instead of guessing, widens its own interval when refused and eases back after a run
  of answers, and caches a release's genres so an album asks once rather than once per track. The
  backlog is rebuilt from the catalog at startup: a file that has never been looked at is already
  `raw`, so it needs no second copy of the truth.

Downloads no longer wait for the batch. Each file is catalogued and queued the moment yt-dlp
  finishes writing it, so track one is finished while track two is still coming down the wire, and
  the batch says what is happening to each file rather than only how many bytes are moving. A
  playlist URL becomes a playlist under the source's own name, written to <library>/playlists as
  m3u8 with the source URL in it, so a rescan reads it back as the thing it is rather than demoting
  it to local. The URLs asked for are remembered - the last five offered back, and playlist ones
  kept and re-fetched on demand, taking only what is new.

The library, the inbox and a playlist are now one table over three questions: same rows, same
  selection, same inspector, same shortcuts. A playlist used to be its own seven columns with no way
  to play anything or fix a tag from where you noticed it was wrong. Columns can be dragged to
  reorder, dragged at the edge to resize, and are remembered per page; headings sort; paging says
  where you are and lets you jump. Every cell is one line, because a row that grows because a track
  carries an extra tag makes the whole table jump.

The inbox empties itself. A file leaves when it has settled - identified, skipped, rejected, or
  looked up and not found - or when it is a day old, so a library with no network still drains. What
  stays is what is waiting on you, which is what an inbox is for.

Also: play any track from the row or the inspector, to hear whether a download is what it claims to
  be; a sync list that can be built with nothing plugged in and applied to a card later; a decade
  facet; filters that belong to the page rather than following you between them; marks on playlists
  that have gained something since you last looked, cleared by looking; and work that survives a
  reload - the page asks what is running rather than remembering what it started, so a second tab
  shows the download the first one began.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Testing

- **download**: Stop asserting a state the queue is still changing
  ([`d087161`](https://github.com/WillNHT/lemon-zest/commit/d0871617d9b7acbdd7009c97b093556050ddcfba))

The batch goes on changing after a download returns: the files it queued are still being identified,
  on the queue's own thread, and the summary carries the live batch rather than a copy of it.
  Asserting that an item reads "done" was therefore a coin toss - it passed on the machine it was
  written on and failed on CI, and asserting "queued" instead only moves which machine loses.

The end state is the one worth having, so the test waits for the queue and then asks. The gate
  test's bound goes from five seconds to nine for the same reason - room for a slow machine, still
  nowhere near the thirty a batched run would take to time three gates out - and each test in the
  class now starts with a fresh queue rather than inheriting the last one's backlog.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.6.1 (2026-09-10)

### Bug Fixes

- **download**: Stop the Download page hanging on the packaged build
  ([`1aecc43`](https://github.com/WillNHT/lemon-zest/commit/1aecc43667b936f60361970d73928ac5403dc560))

The page sat on "Checking what is installed..." forever in the 0.6.0 executable. Its render threw
  `ReferenceError: norm is not defined` - `norm` is a Python helper, called from the JavaScript that
  tags a JavaScript runtime as ours. Nothing catches a throw inside render, so the loading
  placeholder was the last thing painted, and the server, which had answered /api/download/config in
  77ms, looked like the culprit.

The tag needed two paths compared, and the browser is the wrong place to do it. `shutil.which`
  returns the extension in the case PATHEXT carries it - `deno.EXE` - while the bundle spells it
  `deno.exe`, so even a correct string comparison would have failed on the machine this feature
  exists for. The server now decides: each found runtime carries a `bundled` flag, settled by
  `os.path.samefile` with a normcase fallback, and the page just reads it.

`bundled.status()` reports normalised paths to match every other path shown; `tool()` keeps the
  native spelling that actually gets executed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.6.0 (2026-09-09)

### Features

- **download**: Show the batch, not just the current file
  ([`ca5cc6b`](https://github.com/WillNHT/lemon-zest/commit/ca5cc6bde33254170b1ba2c9686f6e0fc19d524b))

A forty-track download reported the bytes of whichever file yt-dlp happened to be fetching. That bar
  resets to zero at every track and never describes the run, so the only way to know how far along a
  batch was is to count "wrote ..." lines in the log.

The run now counts items. The URLs are listed once before anything is fetched - one flat listing
  each, reused afterwards for the playlist ordering that already needed it - so the bar has a
  denominator from the first second rather than the last. Every item ends exactly once: written,
  already in the archive, or failed, which is what makes "12 of 47" add up.

The progress template carries the playlist position, the speed and the ETA alongside the bytes, and
  the short three-field line an older yt-dlp emits is still read rather than dropped. Progress
  events now carry item counts; the bytes moved to where they describe something - the current item,
  handed to a new on_batch callback and kept whole on the job.

The Download page draws it: overall bar, done/total, downloaded vs already had vs failed, elapsed
  and an estimate of what is left, and beneath it the track being fetched with its own bar, size,
  speed and ETA. It stays on screen when the run finishes. The status strip shows the same count on
  every page, so a long playlist can be watched from the library.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.5.0 (2026-09-09)

### Features

- **app**: Inbox, folder control, track inspector, standalone build
  ([`1e1caa3`](https://github.com/WillNHT/lemon-zest/commit/1e1caa39797aa81fbeb18f0a67e8ebb6e0a10a5f))

The interface gains the things a library page cannot answer, and the packaged executable stops
  asking the user to install anything.

Inbox. Every file the catalog has indexed since the inbox was last emptied, newest first, with when
  it arrived. track.added_at is written on insert and never updated, so a rescan of an edited file
  does not make it new again; "mark all as seen" moves a watermark in meta and deletes nothing. The
  library and the inbox are one table over two questions, so the selection model, the shortcuts and
  the actions are shared.

Library folders. Each one can be hidden - still indexed, but out of the library, the facets, the
  inbox and the counts - or removed, which forgets its rows. Neither touches a file, and the remove
  dialog says so before the button rather than after it.

Track inspector. Clicking a row opens what the table has no room for: the artwork the file itself
  carries (served from the audio, not from the Cover Art Archive - a download's video frame is the
  reason to look), every field, the full path, added and modified, and where the values came from.

Search terms. Enrich on a single track shows the terms the lookup will send and lets them be typed
  over: MusicBrainz has "Song", the file is "Song (Single Version) [Official Video]", and no scoring
  recovers that. Typed terms are searched once, verbatim, judged against themselves rather than the
  file's tags, and skip the ISRC shortcut - they exist because the exact answer was wrong. Refused
  over a selection: one query cannot describe forty tracks.

Genre. It was never in ENRICHABLE, so nothing ever proposed one. Now taken from the release and its
  release group, by vote, and only when the file's own tag is empty.

Standalone executable. yt-dlp, ffmpeg, ffprobe, Deno and fpcalc ship inside the binary;
  lemonzest.bundled puts them at the front of PATH at startup, so yt-dlp finds ffmpeg and a JS
  runtime without anything being installed. A frozen build has no `-m`, so the exe re-runs itself
  with --yt-dlp and becomes it. `lemon-zest tools` reports what was found and whether it came from
  the bundle; the smoke test checks all five with PATH stripped, so a machine that happens to have
  ffmpeg cannot pass by accident.

Also: the download page keeps its URL list across reloads, text is selectable everywhere except the
  controls, and every gradient is now a solid colour.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Performance Improvements

- **app**: Ship a folder, and stop the download page waiting on yt-dlp
  ([`4b9a616`](https://github.com/WillNHT/lemon-zest/commit/4b9a61620d9acae4f9d1c7d8aa79665dab1922ad))

Two delays, one of them four seconds long.

Opening the Download page asked yt-dlp its version on every visit. That is a process start, an
  import of yt-dlp and - on a packaged build - a second copy of this executable: 4.1s measured,
  spent inside the click that opened the page. The answer is now cached for the life of the process,
  warmed by a thread at startup, and taken under a lock so a page opened during the warm-up waits
  for that one subprocess instead of starting another. The view also paints before it loads, so
  switching to Download is immediate whatever the config costs, and the page keeps its last answer
  rather than blinking back to a spinner.

The artifact is now a folder in a zip rather than a onefile .exe. Onefile unpacked all 320 MB of
  bundled ffmpeg, Deno and yt-dlp into a temporary directory on every single launch; extracted once
  by whoever unzips it, the same build starts in 0.85s instead of five seconds.
  `packaging/make_zip.py` writes the archive - one top-level folder, so extracting it never scatters
  three hundred files - and CI and the release workflow now build, smoke-test the executable inside
  the folder, zip it, and attach that. The tools download is cached in CI on the URL set, so a rerun
  does not refetch a quarter of a gigabyte.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.4.0 (2026-09-09)

### Bug Fixes

- **tags**: Open the file the filesystem has, not the one the catalog stored
  ([`6ec4e1b`](https://github.com/WillNHT/lemon-zest/commit/6ec4e1bf5c013a68ea19b958a041061f8675b890))

Writing tags to a downloaded track failed with "no such file" over a file that was plainly sitting
  there. The catalog stores paths NFC-normalised, because a path needs one spelling to be
  comparable; NTFS keeps whatever bytes wrote the file, and a yt-dlp download of a Japanese title
  arrives as NFD - U+304B plus a combining dakuten where the catalog holds U+304C. Identical to a
  reader, different to open().

`paths.resolve_existing` already existed for exactly this, and `executor` and `organise` both used
  it. `tags.write` did not, so it was the one path that opened the stored spelling directly. It now
  resolves first.

resolve_existing also gains a component-wise walk. Trying the whole path as NFC and then as NFD
  covers the ordinary case and misses the one that actually occurs here: a folder made by hand in
  one normalisation holding a file written by yt-dlp in the other, where normalising the whole path
  either way fixes one component and breaks the other.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- **enrich**: Identify and tag new files automatically, and make the rest reviewable
  ([`2ce9688`](https://github.com/WillNHT/lemon-zest/commit/2ce9688912c38bc928bac623942bcefc2c38b87d))

Enrichment used to be something you had to remember to run, and the only way to act on it was to
  type track ids at a terminal. It now runs itself, and what still needs a person has a place in the
  interface.

Automatic --------- A scan and a download each finish by identifying whatever they brought in and
  writing the tags into those files - `enrich.auto_after_ingest`, in both the CLI and the server. It
  runs as its own job rather than as a tail on the caller's: MusicBrainz allows one request a
  second, and a scan that finished in two seconds should not appear to grind for an hour. A
  download's pass is scoped to the files that run fetched, so downloading into a large library does
  not start a pass over all of it.

Three bounds automation does not get to widen: a skipped file is never looked at, only an `applied`
  match reaches disk (a candidate waits for a person and no file is rewritten for it), and the
  fill-only rule and hand-typed overrides still win.

Four states ----------- `enrichment.status` records what a lookup did; the interface needs the
  shorter question of what to do next. Four states answer it - raw, awaiting, enriched, skipped -
  and `enrich.STATE_SQL` maps every status onto one so the library list can filter, page and count
  on it in the same statement as the rest. `rejected` folds into `skipped` because that is what it
  always did.

A skipped file is excluded from `pending` even under `--redo`, and from an explicit hand-picked
  selection, which is the promise the state makes.

In the library -------------- A Metadata column and state chips, selection the way a file manager
  does it (click, shift-click a run, ctrl-click, ctrl+A, arrows, and "select all N matching" for the
  whole filter rather than the page), and single-key actions over the selection: E enrich, A/R
  accept or reject, S skip, U mark raw, Enter edit, W write tags.

Editing by hand opens on one track with three tiers side by side - what the catalog says, what the
  source proposed, what has been typed - and over a selection it writes only the fields filled in,
  so fixing one album artist does not flatten everybody's title to one value.

Saying what happened -------------------- A run that failed used to report `failed: 1` and drop the
  reason on the floor; `write_back` returned a bare bool, and returned True when it had written
  nothing at all. Failures now carry their message (folded with counts, so an outage is one
  sentence), `write_back_result` reports why, and a finished job leaves a result on the page until
  it is dismissed.

The write dialog previews the exact diff before running - every field, what the file holds, what it
  would become - and says up front when nothing would change, when files are not where the catalog
  thinks, or when the cover box has no release to fetch from.

ISRC lookups were returning almost nothing ------------------------------------------
  `/ws/2/isrc/<isrc>` answers with the recording and its artist credit and zero releases, whatever
  `inc` asks for. So the free, exact-identifier rung - the one that covers 98.7% of the tagged
  library - was delivering a title and an artist and then stopping: no album, no year, and no
  release id, which meant cover art could never be fetched for any ISRC match. One extra request by
  mbid, spent only when the first reply comes back short, restores it.

Tests set LEMONZEST_AUTO_ENRICH=0. That is a suite hatch so CI does not make rate-limited calls to
  somebody else's service, not a user-facing setting: nothing in the interface can turn automatic
  enrichment off.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.3.0 (2026-09-09)


## v0.2.1 (2026-09-09)

### Bug Fixes

- **scan**: An empty folder is a library folder
  ([`ce48738`](https://github.com/WillNHT/lemon-zest/commit/ce48738b208f1d50658fe4f5ac575508ea042767))

A library folder was known only through the tracks in it: the roots list was SELECT DISTINCT root
  FROM track. Scanning a brand new empty folder therefore registered nothing, so it never appeared
  in the Download view's "Into" picker, and a download with nothing else in the catalog failed with
  "no library folder to download into" - the one case where downloading is exactly how the folder
  would get filled.

A library_root table now records the folders themselves. scan() registers the root before it walks,
  and download() registers whatever root it lands in. An older catalog adopts its track roots once,
  on open.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.2.0 (2026-09-09)

### Bug Fixes

- **download**: --print implies --quiet, so undo it
  ([`8919b9b`](https://github.com/WillNHT/lemon-zest/commit/8919b9bc616ea01157a143450b5983a4ffb62a4a))

The progress bar never moved on a real download and the log held nothing but warnings, errors and
  the one line naming each finished file. The cause was our own command: --print implies --quiet as
  well as --simulate, and while a WHEN prefix suppresses the --simulate half it leaves the quiet
  half in place. yt-dlp was doing exactly as asked, and saying almost nothing.

--no-quiet after the --print restores it. On one real video that is 14 progress lines where there
  were none, parsing as (1024, 4185442, "Adam's Song"), plus twenty lines of extraction detail worth
  having in a log.

No stub could have caught this: a stub prints whatever it is written to print regardless of the
  flags it is handed, which is exactly why the two things it cannot judge - what yt-dlp does with an
  option, and when it flushes - both went wrong here. The test therefore asserts the flag is present
  and ordered after --print, and the reasoning lives in a comment beside it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **download**: A resumed download finishes the job it started
  ([`aa387dd`](https://github.com/WillNHT/lemon-zest/commit/aa387dd11e2d7c3b37f949f50bb77535bbff2e3c))

Indexing happens after yt-dlp exits, so a run that is interrupted - and a sixty-track playlist gives
  ample opportunity - leaves audio on disk and a line in the download archive, but no catalog row.
  Running it again skips those files by design and reports them without naming them, so nothing ever
  indexed them: they sat in the library folder, invisible to the catalog, the device set and every
  playlist. Testing against a real playlist produced exactly that, fifteen tracks deep.

When a run skips anything, it now rescans the library folder afterwards. That is stat-only for the
  thousands of files that have not changed, and it is the one thing that cannot miss a file whose
  name we were never told.

The playlist gets the same treatment. Rather than adding what this run happened to fetch, it
  resolves what the request asked for: the source is listed, each item's video id is matched against
  the purl tag that --embed-metadata wrote and the scanner read, and the playlist is filled in the
  source's own order. A resumed download therefore produces the whole playlist rather than the tail
  of it. If the source cannot be listed, it falls back to this run's files, since a playlist missing
  its older half still beats no playlist at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **download**: Ask yt-dlp not to buffer, so the log is live
  ([`ecba8cd`](https://github.com/WillNHT/lemon-zest/commit/ecba8cd1f725f56d7f183152c9221735ac4797b0))

On the first real playlist download the interface reported nothing for minutes at a time - no
  progress, no files - while the audio was plainly landing on disk and the download archive was
  growing. Python block-buffers stdout when it is a pipe rather than a terminal, so yt-dlp's output
  reached Lemon Zest in 8 KB instalments: the progress bar sat still and the log looked empty until
  enough text had accumulated to flush.

PYTHONUNBUFFERED in the child's environment fixes it, next to the PYTHONIOENCODING that is already
  there for the same class of reason.

The existing tests could not have caught this, and neither could any test whose stub exits promptly,
  because exiting flushes. The new one holds the pipe open after its first line and deliberately
  does not flush it, then asserts the line arrived while the child was still running; it fails at
  2.3s against the unfixed code.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **download**: Enable a JavaScript runtime, and keep --paths working
  ([`e3a4352`](https://github.com/WillNHT/lemon-zest/commit/e3a4352012cebfb70e92dac1e18724c039c4daa5))

Every video from a real playlist failed with "The page needs to be reloaded", preceded by "Signature
  solving failed" and "n challenge solving failed". YouTube signs its media URLs with a challenge
  that has to be executed, so yt-dlp needs a JavaScript runtime to get a playable format at all -
  and it enables only Deno by default, treating node, bun and quickjs as opt-in. This machine has
  Node and no Deno, so nothing could be fetched.

Lemon Zest passes --ignore-config, so the user cannot fix that in their own yt-dlp.conf: enabling
  the other runtimes has to happen here. It is a config key, so it can be changed or emptied, and
  Deno keeps its priority when it is installed, so a machine that already worked behaves
  identically. The option is only passed to a yt-dlp that advertises it in --help, cached per
  interpreter, because an unknown option is not a degraded download but an immediate usage error,
  and this project would rather work with whatever yt-dlp is installed than pin a version.

Which runtime a download will use is now reported next to the cookie source, in the interface and in
  both commands, for the same reason cookies are: when it is missing, every video fails, and the
  failure says nothing about why. The error mapping now names it too.

Also fixes a bug the fix uncovered: yt-dlp ignores every --paths when the output template is itself
  absolute ("--paths is ignored since an absolute path is given in output template"), so the part
  files were landing beside the finished audio rather than in the dot-folder the scanner skips -
  exactly the case that would let a scan index a half-written download. The template is now
  relative, with the library folder passed as -P home:.

Verified against the real yt-dlp on the video that failed: it now resolves format 251 and a
  destination inside the library, with no warnings.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **enrich**: Search on the primary credit when the artist is a writer list
  ([`2157508`](https://github.com/WillNHT/lemon-zest/commit/21575080d83ed367793b0987abec919e4403f7d8))

The seven-name folder was still unmatched. `credits()` split the string correctly and had done since
  the first commit, but it was only ever used for scoring and for the offline backfill - the search
  query still received all seven names verbatim:

artist:"Joji, Kurtis McKenzie, Linden Jay, Chelsea Lena, George Miller, Joshua Bliss Taffel, Kacy
  Anne Hill"

No index has an artist by that name, so the lookup returned nothing and the track was reported as
  "no match" rather than as a question we had failed to ask properly.

The attempt ladder now falls back to the primary credit. The whole string is still tried first,
  because splitting is not always right: "Simon & Garfunkel" is one artist the credit splitter
  happily halves, and the full string is what matches it. Attempts are capped at four so a stubborn
  track cannot cost half a minute of a rate-limited run, and the title rewrites are paired with the
  narrow artist, since by the time they are reached the wide string has already failed.

On the real catalog this takes the run from 17 identified to 20. Both Joji tracks now resolve to
  artist "Joji" with their own ISRCs, and album_artist - which was empty, and which the device path
  template reads first - is filled in, so the seven-name folder stops being generated on the card.

The library file itself keeps its folder: nothing here renames anything on disk.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **paths**: Substitute illegal characters rather than erase them
  ([`1e90a2e`](https://github.com/WillNHT/lemon-zest/commit/1e90a2e3221a5d3cf379f9da9d8eddfedd500265))

Two defects in one small function, both found by pointing the new organise command at the real
  library and reading what it proposed.

An illegal character became an underscore, which loses what the title said. "WHEN WE ALL FALL
  ASLEEP, WHERE DO WE GO?" was about to be filed under a folder ending "GO_", next to the folder
  yt-dlp had already created ending "GO？" - the same album, spelled two ways, because the downloader
  substitutes the full-width twin and this did not. They are ordinary letters to every filesystem
  involved: FAT32 and exFAT store names as UTF-16, so there was never a reason to reach for an
  underscore. Now the two agree.

The second is worse and was hiding behind an escape. The character class read [<>:"/\|?*], where the
  backslash was escaping the pipe rather than standing for itself - so a backslash in a tag passed
  through untouched, and on Windows that separates path components. An artist called "AC\DC" split
  one folder into two: exactly the defect already fixed for the forward slash, in the same function,
  undetected because the test asserted one example rather than the property.

The test now asserts the property - that no member of the illegal set survives - which is what found
  the backslash.

Consequence worth stating: a track whose tags contain one of these characters gets a different
  destination on a device than it did before, so the first sync after this moves those files on the
  card. The count is small and the sync handles it as an ordinary rename.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **tests**: Stop the stub downloader writing into the repository
  ([`2208bd0`](https://github.com/WillNHT/lemon-zest/commit/2208bd07f339bb61ba4d87e63b3ec1b4ccc8ddbe))

An intermediate version of the stub derived its destination from the -o template alone. Once that
  template became relative, the derivation produced an empty root and three test files were written
  to the working directory - and committed. They are removed here, and the stub now refuses to write
  to anything but an absolute path, so a wrong guess fails the test instead of landing files in the
  checkout.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Chores

- **packaging**: Name the modules a --help run never imports
  ([`05910a9`](https://github.com/WillNHT/lemon-zest/commit/05910a92c86a569aff6d95a7bfb86e56209c590a))

enrich, organise and tags are imported inside the command functions, so a --help run does not touch
  them - which means the frozen binary could be missing all three and still print its help
  perfectly, and the smoke test would pass. Both halves of that are now closed: the spec names them
  (with mutagen.id3 and mutagen.oggopus, which the tag writer newly needs), and the smoke test runs
  two read-only commands that actually reach them.

Also tidies two expressions written badly the first time: the fpcalc error path, which reached into
  a list twice to avoid a variable, and the organise summary, which claimed every skipped file
  lacked an artist when there are now two reasons a file is left alone. It reports the counts per
  reason.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Documentation

- **readme**: Document fingerprinting and organise
  ([`cfda60b`](https://github.com/WillNHT/lemon-zest/commit/cfda60b471154d5646412eb20ed1fef10ce9f9dc))

Covers what the last two commits added: the fourth rung of the ladder and what it needs installed,
  and the command that makes the folders on disk agree with the catalog - including the two things
  it refuses to do, since a tool that moves your music should say where it stops.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- Identify tracks by their audio, and file them where the metadata says
  ([`de8880e`](https://github.com/WillNHT/lemon-zest/commit/de8880ec58d427f9fed49ed212c19a5a41c4bab2))

Two rungs the ladder was missing, and the disk finally agreeing with the catalog.

AcoustID fingerprinting is rung four: the only source that ignores what a file claims and listens to
  it instead, which is the whole of the untagged download case. A file whose artist is a channel
  name and whose title is a video title gives a text search nothing to work with and gives
  Chromaprint everything.

It is not bundled. The earlier note that this needed a packaging change was wrong: fpcalc is located
  the way yt-dlp and ffmpeg already are - PATH first, then beside the frozen executable - which
  keeps an LGPL binary out of the distribution, lets it be upgraded on its own schedule, and means
  the spec and the CI build are untouched. Opt-in behind --fingerprint, and unavailable rather than
  broken when fpcalc or the free AcoustID key is missing.

How much of a fingerprint match is applied depends on the tags, even though getting there did not.
  Audio and tags agreeing is the strongest evidence available and is applied. Audio alone - which is
  what the files this rung exists for will produce, because their tags are what is wrong - is stored
  for review, since "trust the sound over the tag" is a judgement about somebody's library rather
  than a fact.

`organise` then makes the folders match. It plans by default, asks before it moves anything, and
  writes a journal that `organise undo` replays backwards, because moving two thousand files is the
  most destructive thing this program can be asked to do. The default template changes folders only
  and leaves every filename exactly as it is: the two halves of this library spell filenames
  differently, so renaming them as well is a much larger diff than the problem calls for, and is
  available with --template rather than by accident.

The first dry run against the real library paid for the whole design, and found three things:

* Release credits of "Various Artists" were being written into album_artist, which would have filed
  Alphaville and Looking Glass under V. Placeholder credits are now refused and the recording's own
  artist used instead. * A file with no album was to be moved out of a folder reading "someday
  you'll wake up, and you'll be 26" and into one reading "Unknown Album". Any destination containing
  a placeholder component is now skipped: tidiness that destroys information is not tidiness. * The
  character-substitution defects fixed in the previous commit.

After all three, the plan against the real library is exactly the three moves that should happen -
  both Joji writer-list folders and one Rex Orange County - and nothing else. Identification over
  the same 28 tracks went from 17 to 21.

Moving a file leaves its content key alone, because the bytes did not change, so the device manifest
  still matches and a replug stays a no-op. A device whose template mirrors the library layout is
  named before anything moves, since its next sync will move the same files on the card.

47 new tests, no network. 137 in total.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **download**: Fetch audio from YouTube into the library
  ([`d336022`](https://github.com/WillNHT/lemon-zest/commit/d336022e855bbaa34f33399ed3cd7d0b635fcdc9))

Downloads land inside a library root and are indexed on the spot, so what arrives is an ordinary
  library track: tick it onto a card and sync, with no rescan in between. yt-dlp runs as a
  subprocess rather than as an import - that is the interface it promises to keep stable, it can be
  upgraded on its own schedule, and a download that wedges cannot take the catalog's process with
  it.

Two output templates are parsed rather than yt-dlp's human-facing progress: --progress-template for
  bytes, and --print after_move: for the path that was really written, after the extraction and the
  rename. --ignore-config keeps a user's own yt-dlp.conf from redirecting files out of the library,
  part files go to a dotfile folder the scanner skips, and the download archive lives beside them so
  a second run does not re-fetch what is already there.

Cookies are the difference between a download and a refusal: YouTube turns away signed-out clients
  for age gates, the bot check and members-only material. Firefox profiles are found by looking for
  a cookies.sqlite on disk, most recently written first, which is the profile the user is signed in
  to; a cookies.txt is accepted when there is no Firefox to read. The default tries Firefox, falls
  back to the file, and then proceeds without cookies rather than refusing to start, because plenty
  of videos need none. When YouTube does refuse, the error names which of those to fix instead of
  repeating yt-dlp's wording.

Adding a downloaded track to a playlist it is already in does nothing - the same rule the sync
  obeys, for the reason this project exists.

Covered by tests/test_download.py against a stub yt-dlp: no network, and nothing that depends on a
  video still being up.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **download**: Keep the whole run, and let it be copied
  ([`0c3a5bd`](https://github.com/WillNHT/lemon-zest/commit/0c3a5bd775d34effe879f9d223962d69d06d965e))

A download that goes wrong is debugged from what yt-dlp said, and until now almost none of that
  survived: progress moved a bar, one line of detail was kept, and the rest was read and dropped.
  Every line is now recorded and streamed to the interface as it arrives, headed by the command that
  produced it - which is the first question anyone asks of a failed download, and whose answer
  includes which cookie source was chosen - and followed by what happened after yt-dlp exited: what
  was catalogued, and what went into a playlist.

A failure carries its log on the exception rather than only having streamed past, so the CLI can
  print the tail of it instead of leaving the user to reproduce the failure to find out what it
  said, and the server can hand it to a page that loads after the job is over.

The app sets user-select: none, being a tool rather than a document. A log is the exception: it
  exists to be pasted into a bug report. So the log block opts back in, wraps rather than scrolling
  sideways - a truncated path is exactly what nobody can debug - and has a Copy button that falls
  back to selecting the text when the clipboard is not available.

Progress lines are deliberately not recorded: one per chunk would push the run's actual output out
  of the ring within seconds. The ring is 500 lines and the job endpoint now returns all of it,
  rather than the last 40.

Also: a long library path in the destination menu no longer widens the pane past the window, which
  it did because a select will not shrink below its widest option without min-width: 0.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **enrich**: Reconcile a file's tags with what MusicBrainz knows
  ([`340c020`](https://github.com/WillNHT/lemon-zest/commit/340c020a373a3b968d73146927babe36e7b2d9b9))

A file's tags are only as good as whatever wrote them, and the downloaded half of this library is
  wrong in a specific way: with no `artist` tag to read, yt-dlp falls back to the channel name, so a
  KIRINJI track sits in a folder called "Nemu". Nothing in the catalog could tell that from a
  correct tag, because nothing ever asked a second source.

Adds a three-rung ladder, cheapest first. Backfill copies an ISRC onto an untagged twin already in
  the library on folded artist, folded title and a two-second duration window - no network, and it
  promotes files onto the rung above. ISRC lookup is an exact identifier, so a hit is certain. Text
  search is fuzzy, scored on title, artist and duration; above 0.90 it is applied, between 0.62 and
  0.90 it waits for a person, below that it is discarded.

Four rules make it safe to point at a library you care about.

Nothing is overwritten in place: a proposal lives in its own table with its source, confidence and
  date, keyed by content key rather than track id, so a wrong answer is reversible and a moved file
  keeps its enrichment.

A release-derived field only fills a blank. Identifying a recording and choosing which of its forty
  releases a copy came from are different questions with very different certainties, and one
  confidence score describes only the first. The live run proved it: a correctly tagged "Cigarettes
  After Sex" track matched its own recording with confidence 1.00 and proposed relabelling the album
  with the HBO soundtrack that recording also appears on. Title, artist and ISRC come from the
  recording and are taken; album, year and track number fill in only where the file was silent.
  Release choice also prefers the artist's own record over a Various Artists anthology, and ties
  between equally-scoring recordings are broken the same way - without which "How to Save a Life"
  arrives as track 19 of "Hot Party Summer 2007".

A hand-typed value outranks every source, now and on every later run.

Audio files are untouched unless --write-tags, which asks first. Each file is rewritten to a copy
  and swapped in, so an interruption leaves the original; the content key is recomputed in the same
  transaction, or the next scan would see every corrected file as new and the card would recopy the
  lot. --artwork additionally replaces the embedded cover with the release's own front cover, which
  matters because a download embeds whatever yt-dlp scraped - the real square cover for an art
  track, a 16:9 video frame for an ordinary upload.

An isolated lookup failure is skipped and counted rather than ending the run; three in a row stop
  it, because at that point the service is down rather than slow.

Also on the download side, both cheap: the output template prefers album_artist to artist, since an
  art track puts every credited writer in the latter and the one searchable name in the former; and
  thumbnails are converted to JPEG, because YouTube serves WebP and several players show nothing at
  all for a WebP cover.

AcoustID is the obvious fourth rung and is deliberately absent: it needs the fpcalc binary bundled
  into the executable, which is a packaging change.

41 tests against a stub MusicBrainz, no network.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>


## v0.1.0 (2026-09-09)

### Bug Fixes

- A slash in a tag no longer becomes a folder
  ([`8794971`](https://github.com/WillNHT/lemon-zest/commit/87949716673a4bf64ee80ae4df6b7582bb220ae9))

Found by checking the path template against the real card before syncing to it. The template
  substituted tag values and only then split the result on "/", so an artist called "AC/DC" turned
  one path component into two and the tracks would have landed in AC/DC/... instead of AC_DC/... -
  scattering files into a folder tree nobody asked for, and making 94 destinations disagree with
  what was already on the card. Values are now sanitised individually, before they are joined; the
  template's own slashes are what separate components. That brought the disagreement down to 39.

Also adds, both needed to sync a card that already has content:

- "{rel_path}" as a template, mirroring the library's folder layout onto the device. For a card
  populated from that library it matches exactly, where the tag-derived template does not.

- "hoard device adopt": record files already in place instead of recopying them. A card filled by an
  earlier tool has an empty manifest, so every file reads as untracked and a first sync would recopy
  the lot; adoption verifies each planned destination (by size, or by content key) and writes the
  manifest rows. On the real card this recognised 533 of 533 files and turned a 20 GB recopy into an
  empty plan.

- "hoard device config" to inspect or change a device's layout.

And a correctness fix in the planner: a track skipped as unusable is now left out of the playlists
  too. Six of the tracks referenced by these playlists are zero-byte files, and writing entries that
  point at them just moves the dead reference from the copy step to the player.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **sync**: Resolve a destination's on-disk spelling before writing it
  ([`10c960f`](https://github.com/WillNHT/lemon-zest/commit/10c960f1499e8812a6683af1a826dc72bef6f115))

Caught by the first real sync, which turned 23 playlists into 46. The card's filenames are NFD; the
  catalog and the plan speak NFC. Everything that compares normalises, so the plan correctly
  reported all 23 as replacements - and then the writer opened the other byte sequence, which is a
  different file to the filesystem, and left every bloated original sitting beside a clean copy.

Every destination now goes through resolve_existing first: playlists, copies and deletions alike.
  The regression test seeds a card with an NFD-named playlist and asserts one file survives a sync;
  without the fix it finds two.

The card was returned to its exact pre-sync state and re-synced: 23 files, 39,971 entries down to
  1,073, every entry resolving, original filenames untouched.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Build System

- Package the program as a standalone executable
  ([`56bbe61`](https://github.com/WillNHT/lemon-zest/commit/56bbe61087289ad06306b00a7eec4ed23d70b53a))

The interface is the part of this people actually want, and it currently costs them a Python install
  to reach. PyInstaller folds the interpreter, the dependencies and the staged frontend into one
  file instead.

The spec derives everything it needs itself - the version out of the package, the frontend staged
  and stamped with it, the Windows version resource written from it - so the same one command works
  on a laptop and on a CI runner with no arguments to keep in sync.

The entry point decides which half of the program to run from how it was launched: arguments mean
  the CLI, a bare double-click from Explorer means the interface. The smoke test then starts the
  built binary and asks it for a page and an API response, because a build can succeed and still
  ship something that dies on its first import.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Chores

- Keep the scratch test database out of the repo
  ([`72226ce`](https://github.com/WillNHT/lemon-zest/commit/72226ce71d98e9470b5627e77d0343bfa6c260ba))

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- Rename the project to Lemon Zest
  ([`e998190`](https://github.com/WillNHT/lemon-zest/commit/e9981903598447260f09413b0baa7c5c999c4f0c))

Package hoard -> lemonzest, command hoard -> lemon-zest (with lz), marker file .hoard-id ->
  .lemon-zest-id, temp suffix .hoard-tmp -> .lz-tmp, and the device table's hoard_id column ->
  device_uid.

Nothing already paired or already synced is disturbed. read_marker falls back to .hoard-id and keeps
  the id it finds, so the card stays the same device; write_marker retires the old file only after
  the new one is in place and only when the two agree. db.migrate renames the column and adds the
  new playlist_template in place, guarded by what the database actually holds rather than a version
  number, so a half-applied upgrade finishes on the next open. HOARD_DB and ~/.hoard/hoard.db are
  still honoured.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Continuous Integration

- Test every push and derive releases from the commit history
  ([`0a90972`](https://github.com/WillNHT/lemon-zest/commit/0a9097221651cfefcef3fc940ed04e6e21146a55))

Three workflows' worth of questions, kept as separate jobs so a failure names itself: do the commit
  messages parse, do the tests pass on the platforms people run this on, and does the executable
  still build and start.

Releases stop being a number anyone types. python-semantic-release reads the Conventional Commit
  subjects since the last tag and derives the next one - fix bumps the patch, feat the minor, a
  breaking marker the major, and a run of docs and chore commits releases nothing at all. The
  executable is built from the tag rather than from the triggering commit, so the version baked into
  the binary is the version on the release it hangs off.

The pull request check is what keeps that honest: an unparseable subject would not fail anything, it
  would silently fail to ship.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Documentation

- How a device's playlist naming is detected and honoured
  ([`9a1e234`](https://github.com/WillNHT/lemon-zest/commit/9a1e234bc6d8e09dfddc50906bfc6abd4fdf7b7e))

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- How to download the executable and how a release happens
  ([`01c80ea`](https://github.com/WillNHT/lemon-zest/commit/01c80ea3b9b197d206817dab4348b564fbdc70f4))

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

- Hoard MVP - catalog, planner and sync engine
  ([`57f58d5`](https://github.com/WillNHT/lemon-zest/commit/57f58d5fb115277ea3ec28c31fd202ede59f9bf5))

Implements the MVP scoped in the build plan: index a local library, choose what goes on a device,
  and sync it correctly and repeatably.

The core is a three-set diff - desired (what was ticked), planned (desired run through the device
  profile) and observed (what is on the card). The manifest stores the source content key, so a card
  that is already correct produces an empty plan and copies nothing.

Notable behaviours, each covered by a test:

- Playlists are rewritten, never appended. The card this was built for had grown 1,089 real playlist
  entries into 19,449 over eighteen syncs. - Copies land on a temp name and are renamed, so an
  interrupted sync leaves no partial file and resumes where it stopped. - A set larger than the free
  space is refused at plan time. - Destinations that escape the device root are refused. - Playlist
  provenance (#Collection URI, per-track #Apple Music URI) is read, stored and written back.

Devices are identified by volume label with a .hoard-id marker file as the tiebreaker, rather than
  by USB serial - enough for a personal fleet and no native APIs needed.

Path handling covers the two hazards seen in the real library: Unicode normalisation (NFC/NFD, in
  filenames and in playlist names) and FAT32/exFAT naming rules.

No transcoding or loudness analysis yet - the library is uniformly AAC, so there is nothing to
  convert. The planner and executor talk in plain dicts and an event callback, leaving room for a
  GUI later.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- Local interface for the sync engine
  ([`ef8e6d0`](https://github.com/WillNHT/lemon-zest/commit/ef8e6d0ec03cbd913a668f4765985d247b79ecca))

Adds `hoard gui`: a Flask server over the existing core plus a single-page UI, served from the
  process that owns the catalog. No build step, no npm, no CDN - it works with the network
  unplugged.

Four views against the real core: the library with a three-pane genre/artist/album browser and a
  per-device tick column, playlist detail showing match state and provider URIs, device detail with
  dry run, live sync progress and activity log, and pairing from a list of mounted volumes.

The palette and proportions come from the 0.9 prototype - warm paper ground, oxide accent, hairline
  rules, dense rows - written out as plain CSS.

Scans and syncs run on a worker thread and report through a job registry, so the page stays
  responsive, a sync survives a reload, and the catalog stays readable while it writes. Each worker
  opens its own SQLite connection.

Working against the real library turned up 28 zero-byte files - failed downloads, including a whole
  album. Copying those would put dead entries on the card, so the planner now skips them with a
  reason, and a "Needs attention" view collects them alongside untagged files and unmatched playlist
  entries. Untagged tracks also sort last in the library rather than first, where SQLite's NULL
  ordering had put them.

tests/test_server.py covers the API through Flask's test client: no port is opened and no browser is
  involved. 25 tests pass.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **playlists**: Write playlists under the device's own filename
  ([`119520a`](https://github.com/WillNHT/lemon-zest/commit/119520a8ba629948a70b83e084e826301ed91ffe))

A player names its playlist files its own way - the HiBy card spells them "<name>-<owner>.m3u8" -
  and a writer that ignores that does not fix the bloated file it was meant to replace, it adds a
  clean one beside it. The card would have ended up listing 46 playlists, the broken 23 among them.

Devices now carry a playlist_template, of which {name} is the playlist and everything around it is
  the device's spelling; "device config --detect-playlists" reads a mounted card and adopts what is
  already there (23 of 23 on the real card, longest name first so "chill" cannot claim the file
  belonging to "chill-archive").

The dry run could not see this before, because it reported only what it would write. It now names
  the file each playlist lands on, says whether that replaces something, and lists the playlist
  files it would leave behind - which is the whole signal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

- **ui**: Surface the playlist naming mismatch in the interface
  ([`929aab8`](https://github.com/WillNHT/lemon-zest/commit/929aab8f888f53e2dffcc1973771c06a8912ec69))

The dry run in the browser had the same blind spot the CLI did: it listed what it would write and
  said nothing about the 23 files that would survive beside it. The plan panel now names the
  template it is writing under and warns when files would be left in place, with a button that
  adopts the card's own naming and re-plans.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
