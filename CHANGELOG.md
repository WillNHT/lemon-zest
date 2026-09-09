# CHANGELOG


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
