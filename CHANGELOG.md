# CHANGELOG


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

- **tests**: Stop the stub downloader writing into the repository
  ([`2208bd0`](https://github.com/WillNHT/lemon-zest/commit/2208bd07f339bb61ba4d87e63b3ec1b4ccc8ddbe))

An intermediate version of the stub derived its destination from the -o template alone. Once that
  template became relative, the derivation produced an empty root and three test files were written
  to the working directory - and committed. They are removed here, and the stub now refuses to write
  to anything but an absolute path, so a wrong guess fails the test instead of landing files in the
  checkout.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

### Features

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
