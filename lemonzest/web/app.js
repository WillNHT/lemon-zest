/* Lemon Zest UI.
   Plain ES modules-free JavaScript, no build step, no CDN: the app is served
   from the same local process that owns the catalog, so it should work with
   the network unplugged. State lives in one object; every mutation calls
   render(). At this size that is simpler and easier to follow than a
   framework, and the tables are the only thing big enough to need care. */

'use strict';

// ------------------------------------------------------------------ util

const $ = (sel, el) => (el || document).querySelector(sel);
const h = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

function bytes(n) {
  if (n === null || n === undefined) return '-';
  if (n >= 2 ** 30) return (n / 2 ** 30).toFixed(2) + ' GB';
  if (n >= 2 ** 20) return (n / 2 ** 20).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}
function dur(sec) {
  if (!sec) return '';
  const s = Math.round(sec);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
function num(n) { return (n || 0).toLocaleString(); }
function rate(bps) { return bps ? bytes(bps) + '/s' : ''; }
// A duration as a person says it. Used for elapsed and for what is left,
// which are the two numbers a long download is actually watched for.
function span(secs) {
  if (secs === null || secs === undefined || !isFinite(secs)) return '';
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60), sec = secs % 60;
  if (m < 60) return m + 'm ' + String(sec).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
function when(ts) {
  if (!ts) return 'never';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([],
    { hour: '2-digit', minute: '2-digit' });
}
function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function icon(id, size) {
  return `<svg class="ico" width="${size || 12}" height="${size || 12}" aria-hidden="true"><use href="#${id}"/></svg>`;
}

async function api(path, opts) {
  const res = await fetch('/api' + path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, opts || {}));
  let body = null;
  try { body = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((body && body.error) || res.statusText);
    err.payload = body;
    err.status = res.status;
    throw err;
  }
  return body;
}

// ----------------------------------------------------------------- state


// One question's worth of filtering. A function rather than a shared
// object: three views hold one each, and they must not be the same one.
function BLANK_FILTER() {
  return { q: '', decade: '', genre: '', artist: '', album: '', state: '' };
}

const S = {
  view: 'library',
  // The download page's URL box, kept across navigation and reloads: a list
  // of twenty links is typed once, and losing it to a stray click on the
  // sidebar is the kind of loss that makes people paste into Notepad first.
  dlUrls: (() => { try { return localStorage.getItem('lz.dl.urls') || ''; }
                   catch (e) { return ''; } })(),
  stats: null,
  devices: [],
  playlists: [],
  syncList: null,        // /sync-list: what is prepared to go out
  deviceId: null,        // device being viewed
  scopeId: null,         // device the library's tick column edits
  playlistId: null,
  facets: { decade: [], genre: [], artist: [], album: [] },
  // One set of filters per question, not one for the whole app. A search
  // typed in the library used to follow you into the inbox, which then
  // showed nothing while the badge beside it said six - the badge counting
  // what arrived, the page answering a question you had asked somewhere
  // else and forgotten.
  filters: {
    library: BLANK_FILTER(), inbox: BLANK_FILTER(), playlist: BLANK_FILTER(),
    unlisted: BLANK_FILTER(),
  },
  // Playlists that do not count on the "Not in a playlist" page: the one
  // that holds everything would otherwise make every track look placed.
  unlistedIgnore: [],
  paneFilter: { decade: '', genre: '', artist: '', album: '' },
  navFilter: { device: '', playlist: '' },
  tracks: [], tracksTotal: 0, offset: 0, limit: 200,
  // Which column the table is ordered by, and which way. Null means the
  // view's own default: newest first in the inbox, the source's order in a
  // playlist, the album shelf in the library.
  sort: { col: null, dir: 'asc' },
  // The library selection, kept as content keys rather than track ids:
  // enrichment is keyed by content key, and two copies of one recording are
  // one thing to identify, so ticking either has to mean the same row.
  sel: new Set(),
  anchor: null,          // row index a shift-click measures its range from
  detail: null,          // /enrich/track/<key>: the proposal being judged
  // The track the inspector is showing, and what it knows about it. One
  // selected row means one thing worth reading in full; a selection of
  // forty is an action, not a subject.
  inspectKey: null,
  inspect: null,
  dialogKeys: [],        // what the open dialog acts on, fixed when it opened
  writePreview: null,    // what a write would put in each file, before it runs
  // The last finished enrich or write, kept on screen until it is dismissed.
  // A job that takes 200ms is otherwise invisible: the status strip has moved
  // on by the time anyone looks at it, and "nothing happened" is what the
  // user is left with whether it worked or not.
  outcome: null,
  playlistDetail: null,
  problems: null,
  plan: null, planning: false, planError: null,
  dl: null,              // /download/config: settings, cookie status, yt-dlp
  dlResult: null,        // summary of the last finished download
  dlProbe: null,         // what is at the URL, when asked before downloading
  play: null,            // the track in the listening bar: {id, title, artist}
  job: null,
  log: [],
  busy: false,
  error: null,
};

let pollTimer = null;

// ------------------------------------------------------------------ load

async function loadCore() {
  const [stats, devices, playlists] = await Promise.all([
    api('/stats'), api('/devices'), api('/playlists'),
  ]);
  S.stats = stats;
  S.devices = devices;
  S.playlists = playlists;
  if (!S.scopeId && devices.length) S.scopeId = devices[0].id;
}

/* `S.filter` is whichever set belongs to the view being looked at.

   A property rather than a lookup at each site: every reader and writer of
   a filter already says `S.filter`, and what was wrong was never how they
   said it - it was that there was only one of them. */
Object.defineProperty(S, 'filter', {
  get() { return S.filters[S.view] || S.filters.library; },
});

function anyFilter(f) {
  return !!(f.q || f.decade || f.genre || f.artist || f.album || f.state);
}

function libraryParams() {
  const p = new URLSearchParams();
  for (const k of ['q', 'decade', 'genre', 'artist', 'album', 'state']) {
    if (S.filter[k]) p.set(k, S.filter[k]);
  }
  // A playlist narrows the same table rather than replacing it.
  if (S.view === 'playlist' && S.playlistId) p.set('playlist', S.playlistId);
  if (S.view === 'unlisted') p.set('unlisted', '1');
  if (S.showVersions) p.set('versions', '1');
  if (S.sort.col) { p.set('sort', S.sort.col); p.set('dir', S.sort.dir); }
  return p;
}

/* The library, the inbox and a playlist are one table over three questions.
   They share the row model, the selection, the shortcuts and the inspector,
   because a track is the same track whichever way you arrived at it. */
function isTrackView() {
  return S.view === 'library' || S.view === 'inbox' || S.view === 'playlist'
    || S.view === 'unlisted';
}

async function loadLibrary() {
  const p = libraryParams();
  p.set('limit', S.limit); p.set('offset', S.offset);
  if (S.view === 'inbox') { p.set('new', '1'); p.set('order', 'added'); }
  if (S.scopeId) p.set('device', S.scopeId);
  const [lib, facets] = await Promise.all([
    api('/library?' + p), api('/facets?' + p),
  ]);
  S.tracks = lib.tracks; S.tracksTotal = lib.total;
  S.facets = facets;
}

/* Open a playlist: its own details, and the library narrowed to it.

   One function for both ways in - the sidebar and a link from the
   inspector - because arriving from either has to leave the page in the
   same state. */
function openPlaylist(id) {
  S.view = 'playlist';
  S.offset = 0; S.sel.clear(); S.anchor = null;
  S.sort = { col: null, dir: 'asc' };
  return guard(async () => {
    await loadPlaylist(id);
    // The dot in the sidebar is drawn from the listing, which nothing else
    // is about to reload. Opening the playlist is what clears it, so it
    // clears now rather than at whatever moment the next poll lands.
    const row = S.playlists.find(x => x.id === id);
    if (row) row.fresh = 0;
    await loadLibrary();
  });
}

async function loadPlaylist(id) {
  S.playlistId = id;
  S.playlistDetail = id ? await api('/playlists/' + id) : null;
}

async function loadPlan(deviceId) {
  S.planning = true; S.plan = null; S.planError = null;
  render();
  try {
    S.plan = await api('/devices/' + deviceId + '/plan');
  } catch (e) {
    S.planError = e.message;
    if (e.payload && e.payload.not_mounted) S.planError = e.payload.error;
  } finally {
    S.planning = false;
  }
}

async function loadDownload() {
  S.dl = await api('/download/config');
}

async function loadLog(deviceId) {
  S.log = await api('/devices/' + deviceId + '/log?limit=60');
}

// ------------------------------------------------------------------ jobs

/* Work belongs to the program, not to the tab that started it.

   A download runs on a worker inside Lemon Zest and goes on running whether
   or not anybody is looking at it. The interface used to know that only if
   this tab was the one that pressed the button: reload the page, or open a
   second tab, and a forty-track download in full flight was invisible -
   which reads as "it stopped", and the reasonable response to that is to
   press Download again.

   So the page asks what is running rather than remembering what it started.
   Whatever it finds, it watches, with the same handler the button would
   have attached. */
async function adoptRunningJob() {
  let jobs = [];
  try { jobs = await api('/jobs'); } catch (e) { return; }
  const running = jobs.find(j => j.state === 'running');
  if (running) {
    if (S.job && S.job.id === running.id && pollTimer) return;
    S.job = running;
    if (running.kind === 'download') S.dlResult = null;
    watchJob(running.id, (job) => jobFinished(job));
    return;
  }
  // Nothing running. A download that finished while this tab was closed
  // still has a report worth showing on the page it belongs to.
  if (!S.dlResult) {
    const done = jobs.find(j => j.kind === 'download' && j.state === 'done');
    if (done && done.result) S.dlResult = done.result;
  }
}

/* What to do when a job ends, decided by what it was rather than by who
   started it - so a job this tab adopted finishes the same way as one it
   began itself. */
async function jobFinished(job) {
  if (job.kind === 'download') {
    S.dlResult = job.result || null;
    await loadCore();
    await loadDownload();
    if (S.view === 'playlist' && S.playlistId) {
      await loadPlaylist(S.playlistId, { peek: true });
    }
    if (isTrackView()) await loadLibrary();
    followAutoEnrich(job, 'the new files');
    return;
  }
  if (job.kind === 'enrich' || job.kind === 'write-tags') {
    S.outcome = job;
    await loadCore();
    if (isTrackView()) await loadLibrary();
    return;
  }
  await loadCore();
  if (isTrackView()) await loadLibrary();
}

/* Look in on the program every few seconds when nothing is being watched.

   This is what makes a second tab notice a download the first one started,
   and what lets a tab opened mid-run pick it up. Cheap: the job list is a
   dictionary in memory, and the timer stops itself while a job is being
   polled properly. */
const ADOPT_EVERY = 4000;
setInterval(() => {
  if (pollTimer) return;               // already watching one, closely
  if (document.hidden) return;         // a background tab needs nothing
  adoptRunningJob().then(() => {
    if (S.job && S.job.state === 'running') render();
  }, () => { /* the server may be restarting; the next tick will find it */ });
}, ADOPT_EVERY);

function watchJob(id, onDone) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await api('/jobs/' + id);
      S.job = job;
      if (job.state !== 'running') {
        clearInterval(pollTimer);
        pollTimer = null;
        if (onDone) await onDone(job);
      }
      render();
    } catch (e) {
      clearInterval(pollTimer); pollTimer = null;
    }
  }, 400);
}

// --------------------------------------------------------------- sidebar

function navRow(opts) {
  return `<button class="row ${opts.on ? 'on' : ''} ${opts.add ? 'add' : ''}"
      data-act="${h(opts.act)}" ${opts.arg !== undefined ? `data-arg="${h(opts.arg)}"` : ''}
      title="${h(opts.title || opts.label)}">
    ${icon(opts.icon)}
    <span class="txt">${h(opts.label)}</span>
    ${opts.dot ? '<span class="dot" aria-label="new"></span>' : ''}
    ${opts.pill ? `<span class="pill">${h(opts.pill)}</span>` : ''}
    ${opts.n !== undefined ? `<span class="n">${h(opts.n)}</span>` : ''}
  </button>`;
}

// Where a playlist came from, said in full. A downloaded playlist that
// reads LOCAL is the sidebar contradicting the page, and an abbreviation
// nobody can expand is barely better.
const PL_ORIGIN = {
  apple_music: 'apple music', spotify: 'spotify', youtube: 'youtube',
  download: 'download', local: 'local',
};

function originLabel(pl) {
  if (!pl) return 'local';
  // YouTube Music and YouTube are different services with one domain
  // between them, and which one a playlist came from is worth keeping.
  if (pl.origin === 'youtube') {
    return /music\.youtube\./i.test(pl.source_uri || '')
      ? 'youtube music' : 'youtube';
  }
  return PL_ORIGIN[pl.origin] || 'local';
}

function renderSidebar() {
  const st = S.stats || {};
  const devFilter = S.navFilter.device.toLowerCase();
  const devs = S.devices.filter(d => !devFilter ||
    (d.name + ' ' + (d.label || '')).toLowerCase().includes(devFilter));
  const plFilter = S.navFilter.playlist.toLowerCase();
  const pls = S.playlists.filter(p => !plFilter ||
    p.name.toLowerCase().includes(plFilter));
  const connected = S.devices.filter(d => d.mounted_at).length;

  const sect = (title, count, body, filterKey, ph, foot, scroll) => `
    <div class="sect">
      <h4>${h(title)}${count !== undefined ? `<span class="n">${h(count)}</span>` : ''}</h4>
      ${filterKey ? `<div class="filter"><input data-filter="${filterKey}"
          placeholder="${h(ph)}" value="${h(S.navFilter[filterKey])}"></div>` : ''}
      <div class="items ${scroll ? 'scroll' : ''}">${body}</div>
      ${foot ? `<div class="foot">${h(foot)}</div>` : ''}
    </div>`;

  $('#sidebar').innerHTML =
    sect('Library', undefined,
      navRow({ act: 'view', arg: 'library', label: 'Music', icon: 'i-disc',
               n: num(st.tracks), on: S.view === 'library' }) +
      navRow({ act: 'view', arg: 'inbox', label: 'Inbox', icon: 'i-inbox',
               n: num(st.inbox), on: S.view === 'inbox',
               title: 'Everything indexed since you last emptied the inbox' }) +
      navRow({ act: 'view', arg: 'unlisted', label: 'Not in a playlist',
               icon: 'i-list', n: num(st.unlisted), on: S.view === 'unlisted',
               title: 'Tracks no playlist carries to a player' }) +
      navRow({ act: 'view', arg: 'dupes', label: 'Duplicates', icon: 'i-disc',
               on: S.view === 'dupes',
               title: 'Songs held as more than one file' }) +
      navRow({ act: 'view', arg: 'problems', label: 'Needs attention',
               icon: 'i-warn', n: num(st.attention), on: S.view === 'problems' })) +

    sect('Devices', String(S.devices.length),
      devs.map(d => navRow({
        act: 'device', arg: d.id, label: d.name, icon: 'i-device',
        pill: d.label || '', n: num(d.files),
        on: S.view === 'device' && S.deviceId === d.id,
        title: d.mounted_at ? d.name + ' - ' + d.mounted_at : d.name + ' - not mounted',
      })).join('') +
      navRow({ act: 'view', arg: 'syncList', label: 'Sync list',
               icon: 'i-list', n: num((S.stats || {}).sync_list || 0),
               on: S.view === 'syncList',
               title: 'Prepare what goes on a card before the card is plugged in' }) +
      navRow({ act: 'view', arg: 'addDevice', label: 'Add device...',
               icon: 'i-plus', add: true, on: S.view === 'addDevice' }),
      S.devices.length > 6 ? 'device' : null,
      'Filter ' + S.devices.length + ' devices',
      S.devices.length + ' paired \u00b7 ' + connected + ' connected',
      S.devices.length > 5) +

    sect('Playlists', String(S.playlists.length),
      pls.map(p => navRow({
        act: 'playlist', arg: p.id, label: p.name, icon: 'i-list',
        n: num(p.n), pill: originLabel(p),
        // A dot, not a number: what matters is that something arrived, and
        // how much is in the playlist itself, one click away.
        dot: p.fresh > 0,
        title: p.fresh
          ? `${p.name} - ${num(p.fresh)} added since you last looked`
          : p.name,
        on: S.view === 'playlist' && S.playlistId === p.id,
      })).join('') || '<div class="foot">nothing imported yet</div>',
      S.playlists.length > 6 ? 'playlist' : null,
      'Filter ' + S.playlists.length + ' playlists',
      plFilter ? pls.length + ' of ' + S.playlists.length + ' shown' : null,
      S.playlists.length > 5) +

    sect('Tools', undefined,
      navRow({ act: 'view', arg: 'download', label: 'Add music',
               icon: 'i-dl', on: S.view === 'download',
               title: 'Scan a folder, import playlists, or fetch from YouTube' }) +
      navRow({ act: 'view', arg: 'normalize', label: 'Normalize volume',
               icon: 'i-level', on: S.view === 'normalize',
               title: 'Even out loudness across the library - not built yet' }) +
      navRow({ act: 'view', arg: 'utilities', label: 'Utilities',
               icon: 'i-warn', on: S.view === 'utilities',
               title: 'Maintenance: start the library over' }));
}

// --------------------------------------------------------------- library

function renderBrowser() {
  const pane = (key, label) => {
    const all = S.facets[key] || [];
    const f = S.paneFilter[key].toLowerCase();
    const shown = f ? all.filter(x => x.value.toLowerCase().includes(f)) : all;
    return `<div class="col ${key}">
      <h5>${h(label)}<span class="n">${num(all.length)}</span></h5>
      <div class="filter"><input data-pane="${key}" placeholder="Filter ${h(label.toLowerCase())}"
        value="${h(S.paneFilter[key])}"></div>
      <div class="list">
        <button class="cell ${S.filter[key] ? '' : 'on'}" data-facet="${key}" data-value="">
          <span class="txt">All (${num(all.length)})</span></button>
        ${shown.slice(0, 600).map(x => `
          <button class="cell ${S.filter[key] === x.value ? 'on' : ''}"
                  data-facet="${key}" data-value="${h(x.value)}" title="${h(x.value)}">
            <span class="txt">${h(x.value)}</span><span class="n">${num(x.count)}</span>
          </button>`).join('')}
      </div>
    </div>`;
  };
  return `<div class="browser">${pane('decade', 'Decade')}${pane('genre', 'Genre')}${
    pane('artist', 'Artist')}${pane('album', 'Album')}</div>`;
}

// The four enrichment states, and how each one reads in the table. The
// wording is the promise: `skipped` says "never" because that is literally
// what the backend does with it, and a softer word would be a lie the next
// run tells.
const STATE_TAG = {
  raw:      { cls: '',     label: 'raw',      hint: 'never looked up. A run will pick this one up.' },
  awaiting: { cls: 'warn', label: 'review',   hint: 'a match is waiting for your decision.' },
  enriched: { cls: 'ok',   label: 'enriched', hint: 'identified by a lookup, or marked enriched by hand.' },
  skipped:  { cls: 'bad',  label: 'skipped',  hint: 'excluded. Never looked up again until you change it.' },
};

function stateTag(t) {
  const st = STATE_TAG[t.state] || STATE_TAG.raw;
  const conf = (t.state === 'awaiting' || t.state === 'enriched') && t.confidence
    ? ` ${t.confidence >= 1 ? '1.00' : t.confidence.toFixed(2)}` : '';
  const via = t.enrich_source ? ` via ${t.enrich_source}` : '';
  return `<span class="tag ${st.cls}" title="${h(st.label + ': ' + st.hint + via)}"
    >${st.label}${conf}</span>`
    + (t.overrides ? '<span class="tag" title="carries hand-typed values">edited</span>' : '')
    + (t.versions > 1 ? `<span class="tag" title="held as ${t.versions} files; this one stands for the song">×${t.versions}</span>` : '');
}

// How long ago, in the words a person uses about a download that just
// finished. Exact timestamps are what the title attribute is for.
function ago(ts) {
  if (!ts) return '';
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 90) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + ' min ago';
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
  const days = Math.round(hrs / 24);
  if (days < 30) return days + (days === 1 ? ' day ago' : ' days ago');
  return new Date(ts * 1000).toLocaleDateString();
}

/* The track table, shared by the library, the inbox and a playlist.

   One table over three questions, so one row model, one selection, one set
   of shortcuts and one inspector. What differs between them is which
   columns are worth showing and in what order, and that is not something to
   decide on somebody's behalf: the layout is theirs, per page, kept in the
   browser, and resettable.

   Every cell is one line. A row that grows to two because a track happens
   to carry an extra tag makes the whole table jump, and a table you cannot
   scan is worse than one that hides four characters of an album name. */
const COLUMNS = {
  on: {
    label: 'ON', w: 34, cls: 'tick', sort: null,
    title: 'On the device selected above',
    cell: (t) => {
      const scope = S.devices.find(d => d.id === S.scopeId);
      if (!scope) return '';
      return `<span title="${t.on_device ? 'on ' + h(scope.name)
        : 'not on ' + h(scope.name)}"
        style="color:${t.on_device ? 'var(--accent)' : 'var(--line-2)'}"
        >${t.on_device ? '&#9679;' : '&#9675;'}</span>`;
    },
  },
  no: {
    label: '#', w: 40, cls: 'num',
    sort: () => (S.view === 'playlist' ? 'pos' : 'track_no'),
    title: () => (S.view === 'playlist' ? 'Position in the playlist'
                                        : 'Track number'),
    cell: (t) => (S.view === 'playlist'
      ? (t.playlist_pos === null || t.playlist_pos === undefined
         ? '' : num(t.playlist_pos + 1))
      : (t.track_no || '')),
  },
  title: {
    label: 'Name', w: 260, cls: 'clip', sort: 'title',
    cell: (t) => `<button class="btn sm play" data-play="${t.id}"
        title="Listen to it">${icon('i-play')}</button> ${
        h(t.title || (t.rel_path || '').split('/').pop())}${
        isFreshHere(t) ? ' <span class="tag new">new</span>' : ''}${
        t.empty ? ' <span class="tag bad">empty</span>' : ''}${
        !t.title && !t.empty ? ' <span class="tag warn">untagged</span>' : ''}`,
    cellTitle: (t) => t.path,
  },
  duration: {
    label: 'Time', w: 56, cls: 'num', sort: 'duration',
    cell: (t) => dur(t.duration),
  },
  artist: {
    label: 'Artist', w: 180, cls: 'clip', sort: 'artist',
    cell: (t) => h(t.artist || ''), cellTitle: (t) => t.artist || '',
  },
  album: {
    label: 'Album', w: 180, cls: 'clip', sort: 'album',
    cell: (t) => h(t.album || ''), cellTitle: (t) => t.album || '',
  },
  state: {
    label: 'Metadata', w: 128, cls: 'clip', sort: 'state',
    title: 'Enrichment state', cell: (t) => stateTag(t),
  },
  format: {
    label: 'Format', w: 86, cls: 'mono', sort: 'format',
    cell: (t) => `${h((t.ext || '').replace('.', '').toUpperCase())} ${
      t.bitrate ? Math.round(t.bitrate / 1000) : ''}`,
  },
  added: {
    label: 'Added', w: 104, cls: 'mono', sort: 'added',
    title: 'When the catalog first saw this file',
    cell: (t) => h(ago(t.added_at)),
    cellTitle: (t) => `added ${when(t.added_at)} · file modified ${when(t.mtime)}`,
  },
  updated: {
    label: 'Updated', w: 104, cls: 'mono', sort: 'updated',
    title: 'When its tags, its file or its place last changed',
    cell: (t) => h(ago(t.updated_at)),
    cellTitle: (t) => `updated ${when(t.updated_at)} · added ${when(t.added_at)}`,
  },
  isrc: {
    label: 'ISRC', w: 110, cls: 'mono', sort: 'isrc',
    cell: (t) => h(t.isrc || ''),
  },
};

// What each page starts out showing. The inbox leads with when a file
// landed, because that is the question it is asking.
const DEFAULT_COLS = {
  library: ['on', 'no', 'title', 'duration', 'artist', 'album', 'state',
            'format', 'added', 'updated', 'isrc'],
  inbox: ['added', 'no', 'title', 'duration', 'artist', 'album', 'state',
          'format', 'updated', 'isrc'],
  playlist: ['on', 'no', 'title', 'duration', 'artist', 'album', 'state',
             'format', 'added', 'updated', 'isrc'],
  unlisted: ['on', 'no', 'title', 'duration', 'artist', 'album', 'state',
             'format', 'added', 'updated', 'isrc'],
};

const COLS_KEY = (view) => 'lz.cols.' + view;

/* The layout in force for a page: the columns, in order, with their widths.

   Read from what was saved, then reconciled with the defaults - a column
   added to a later version of this program appears rather than being
   invisible to everybody who ever dragged a heading, and one that is gone
   is dropped rather than throwing. */
function columnLayout(view) {
  const defaults = DEFAULT_COLS[view] || DEFAULT_COLS.library;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(COLS_KEY(view)) || 'null'); }
  catch (e) { saved = null; }
  const out = [];
  const seen = new Set();
  for (const col of (Array.isArray(saved) ? saved : [])) {
    const k = col && col.k;
    if (!COLUMNS[k] || !defaults.includes(k) || seen.has(k)) continue;
    seen.add(k);
    out.push({ k, w: Math.max(28, +col.w || COLUMNS[k].w) });
  }
  for (const k of defaults) {
    if (!seen.has(k)) out.push({ k, w: COLUMNS[k].w });
  }
  return out;
}

function saveColumnLayout(view, layout) {
  try { localStorage.setItem(COLS_KEY(view), JSON.stringify(layout)); }
  catch (e) { /* private mode: the layout lasts as long as the page does */ }
}

function resetColumnLayout(view) {
  try { localStorage.removeItem(COLS_KEY(view)); } catch (e) { /* as above */ }
}

/* Did this row join the playlist since the last time it was opened?

   Measured against the watermark the view was opened with, not the one now
   stored - opening the playlist is what marks it seen, and the rows have to
   go on looking new for as long as you are looking at them. Come back later
   and they are ordinary rows, which is what short-lived means here: seen
   once, done. */
function isFreshHere(t) {
  if (S.view !== 'playlist' || !S.playlistDetail) return false;
  const since = S.playlistDetail.since || 0;
  return !!(t.playlist_added_at && t.playlist_added_at > since);
}

// A column's sort key, heading and tooltip may depend on the page.
const colValue = (v, t) => (typeof v === 'function' ? v(t) : v);

function trackTable(opts) {
  const inbox = !!(opts && opts.inbox);
  const layout = columnLayout(S.view);
  const rows = S.tracks.map((t, i) => `
    <tr data-track="${t.id}" data-row="${i}" data-key="${h(t.content_key)}"
        class="${S.sel.has(t.content_key) ? 'sel' : ''}">
      <td class="tick"><input type="checkbox" data-pick="${i}" tabindex="-1"
        ${S.sel.has(t.content_key) ? 'checked' : ''}></td>
      ${layout.map(({ k }) => {
        const col = COLUMNS[k];
        const tip = col.cellTitle ? colValue(col.cellTitle, t) : '';
        return `<td class="${col.cls || ''}"${tip ? ` title="${h(tip)}"` : ''}
          >${col.cell(t)}</td>`;
      }).join('')}
    </tr>`).join('');

  const allShown = S.tracks.length > 0
    && S.tracks.every(t => S.sel.has(t.content_key));
  const filtered = anyFilter(S.filter);
  const empty = filtered
    ? 'Nothing here matches the filters on this page.'
    : inbox
      ? 'Nothing still arriving. Everything indexed has settled.'
      : S.view === 'playlist'
        ? 'This playlist holds nothing the catalog has.'
        : 'No tracks match.';

  return renderOutcome() + renderSelectionBar(allShown) + `
    <div class="withside">
    <div class="withside-main">
    <table class="tbl cols" id="lib-table">
      <colgroup><col style="width:26px">${
        layout.map(c => `<col style="width:${c.w}px">`).join('')}</colgroup>
      <thead><tr>
        <th class="tick"><input type="checkbox" id="pick-all"
          title="Select everything on this page" ${allShown ? 'checked' : ''}></th>
        ${layout.map(({ k }) => {
          const col = COLUMNS[k];
          const sortKey = colValue(col.sort, null);
          const on = sortKey && S.sort.col === sortKey;
          const arrow = on ? (S.sort.dir === 'desc' ? ' ▾' : ' ▴') : '';
          const tip = colValue(col.title, null)
            || (sortKey ? 'Sort by ' + col.label.toLowerCase() : col.label);
          return `<th draggable="true" data-col="${k}"
            ${sortKey ? `data-sort="${h(sortKey)}"` : ''}
            class="${on ? 'sorted' : ''} ${sortKey ? 'sortable' : ''}"
            title="${h(tip)} · drag to move, drag the edge to resize"
            >${h(col.label)}<span class="arrow">${arrow}</span
            ><span class="grip" data-grip="${k}"></span></th>`;
        }).join('')}
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="${layout.length + 1}" class="empty">
        ${h(empty)}${filtered
          ? ' <button class="btn sm" data-clear-filters="1">Clear them</button>'
          : ''}</td></tr>`}</tbody>
    </table>
    ${renderPager()}
    </div>
    ${renderInspector()}
    </div>`;
}

/* Paging that says where you are and lets you go elsewhere.

   Previous and Next alone answer "is there more" and nothing else: on two
   thousand tracks, page 1 to page 9 was eight clicks and no way to know it
   was eight. Five numbered pages around the current one, the ends always
   reachable, and a box to pick from when the answer is page 47. */
function renderPager() {
  const per = S.limit;
  const total = S.tracksTotal;
  const pages = Math.max(1, Math.ceil(total / per));
  const page = Math.floor(S.offset / per) + 1;
  const from = total ? S.offset + 1 : 0;
  const to = Math.min(S.offset + per, total);

  // Five, centred on where you are, sliding rather than jumping so the
  // window does not change shape as you walk through it.
  let first = Math.max(1, page - 2);
  const last = Math.min(pages, first + 4);
  first = Math.max(1, last - 4);
  const numbers = [];
  for (let n = first; n <= last; n += 1) {
    numbers.push(`<button class="btn sm ${n === page ? 'primary' : ''}"
      data-goto="${n}" ${n === page ? 'disabled' : ''}>${num(n)}</button>`);
  }

  return `<div class="footnote pinned hstack">
    <span>${total ? `SHOWING ${num(from)}–${num(to)} OF ${num(total)}`
                  : 'NOTHING TO SHOW'}</span>
    <button class="btn sm" data-cols-reset="1"
      title="Put this page's columns back to their default order and width"
      >RESET COLUMNS</button>
    <span class="grow" style="flex:1"></span>
    <span class="keys">click select · shift+click range ·
      ctrl+click add · ctrl+A page · E enrich · A accept ·
      R reject · S skip · U raw · Enter edit · Esc clear</span>
    <span class="grow" style="flex:1"></span>
    ${pages > 1 ? `<span class="pager">
      <button class="btn sm" data-goto="1" ${page === 1 ? 'disabled' : ''}
        title="First page">«</button>
      <button class="btn sm" data-page="-1" ${page === 1 ? 'disabled' : ''}
        >Previous</button>
      ${numbers.join('')}
      <button class="btn sm" data-page="1" ${page >= pages ? 'disabled' : ''}
        >Next</button>
      <button class="btn sm" data-goto="${pages}" ${page >= pages ? 'disabled' : ''}
        title="Last page">»</button>
      ${pages > 5 ? `<select id="page-jump" title="Go to a page">
        ${Array.from({ length: pages }, (unused, i) => `<option value="${i + 1}"
          ${i + 1 === page ? 'selected' : ''}>${num(i + 1)} of ${num(pages)}</option>`
        ).join('')}
      </select>` : ''}
    </span>` : ''}
  </div>`;
}

/* Moving and resizing columns.

   Kept out of the render loop on purpose: a resize redraws nothing until
   the mouse comes up, because re-rendering a two hundred row table on every
   mousemove is how a drag turns into a slideshow. The width goes straight
   onto the <col> element, and only the finished number is written down. */
const DRAG = { key: null, grip: null, startX: 0, startW: 0, moved: false };

function tableColEl(index) {
  // The first <col> is the tick column, which is not one of ours.
  const cols = $('#lib-table') && $('#lib-table').querySelectorAll('colgroup col');
  return cols ? cols[index + 1] : null;
}

document.addEventListener('mousedown', (ev) => {
  const grip = ev.target.closest('[data-grip]');
  if (!grip) return;
  const layout = columnLayout(S.view);
  const index = layout.findIndex(c => c.k === grip.dataset.grip);
  if (index < 0) return;
  // The heading is draggable, and a drag beginning on the grip would move
  // the column instead of widening it.
  ev.preventDefault();
  ev.stopPropagation();
  DRAG.key = grip.dataset.grip;
  DRAG.grip = index;
  DRAG.startX = ev.clientX;
  DRAG.startW = layout[index].w;
  DRAG.moved = false;
  document.body.classList.add('resizing');
});

document.addEventListener('mousemove', (ev) => {
  if (DRAG.grip === null) return;
  const width = Math.max(28, DRAG.startW + (ev.clientX - DRAG.startX));
  DRAG.width = width;
  DRAG.moved = true;
  const col = tableColEl(DRAG.grip);
  if (col) col.style.width = width + 'px';
});

document.addEventListener('mouseup', () => {
  if (DRAG.grip === null) return;
  const index = DRAG.grip;
  const moved = DRAG.moved;
  const width = DRAG.width;
  DRAG.grip = null; DRAG.key = null; DRAG.width = null;
  document.body.classList.remove('resizing');
  if (!moved) return;
  const layout = columnLayout(S.view);
  layout[index].w = width;
  saveColumnLayout(S.view, layout);
  // Suppress the click that follows this mouseup, or the heading sorts
  // itself the moment you finish widening it.
  DRAG.justResized = true;
  setTimeout(() => { DRAG.justResized = false; }, 0);
});

document.addEventListener('dragstart', (ev) => {
  const th = ev.target.closest && ev.target.closest('th[data-col]');
  if (!th) return;
  ev.dataTransfer.effectAllowed = 'move';
  // Firefox refuses to start a drag with nothing in the payload.
  ev.dataTransfer.setData('text/plain', th.dataset.col);
  DRAG.key = th.dataset.col;
  th.classList.add('dragging');
});

document.addEventListener('dragover', (ev) => {
  const th = ev.target.closest && ev.target.closest('th[data-col]');
  if (!th || !DRAG.key || th.dataset.col === DRAG.key) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  for (const el of document.querySelectorAll('th.dropinto')) {
    el.classList.remove('dropinto');
  }
  th.classList.add('dropinto');
});

document.addEventListener('drop', (ev) => {
  const th = ev.target.closest && ev.target.closest('th[data-col]');
  if (!th || !DRAG.key) return;
  ev.preventDefault();
  const from = DRAG.key;
  const onto = th.dataset.col;
  DRAG.key = null;
  if (from === onto) return render();
  const layout = columnLayout(S.view);
  const moving = layout.find(c => c.k === from);
  const rest = layout.filter(c => c.k !== from);
  const at = rest.findIndex(c => c.k === onto);
  rest.splice(at, 0, moving);
  saveColumnLayout(S.view, rest);
  render();
});

document.addEventListener('dragend', () => {
  DRAG.key = null;
  for (const el of document.querySelectorAll('th.dragging, th.dropinto')) {
    el.classList.remove('dragging', 'dropinto');
  }
});

/* What one track is, beside the table. Opened by clicking a row, because
   that is the gesture that already means "this one" - and a library row is
   nine columns of the fields that fit, which is never the artwork, the
   path, or where the values came from. */
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

/* A release date, as it would be said out loud.

   The tag holds whatever the file or the lookup gave it, and the spellings
   differ by where it came from: MusicBrainz writes 2018-06-13, and a file
   tagged from a YouTube upload date carries 20241010 with no separators at
   all. Both are a date; only one of them looked like one to the old reader,
   which took the first four digits and showed a bare year for every file in
   a library downloaded from YouTube.

   A year, a year and a month, or a full date - each is shown for what it
   is rather than padded out into a precision nobody supplied. */
function releaseDate(value) {
  const v = String(value || '').trim();
  const m = /^(\d{4})(?:[-/.]?(\d{2}))?(?:[-/.]?(\d{2}))?/.exec(v);
  if (!m) return v;
  const [, year, mon, day] = m;
  // A zero month or day is a tag saying "not known", which some writers
  // spell 2018-00-00 rather than leaving off. Shown as the year it is,
  // not as the nought-th of nothing.
  const name = +mon ? MONTHS[+mon - 1] : null;
  if (!name) return year;
  if (!+day) return `${name} ${year}`;
  return `${name} ${ordinal(+day)}, ${year}`;
}

function ordinal(n) {
  const rest = n % 100;
  if (rest >= 11 && rest <= 13) return n + 'th';
  return n + ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
}

function renderInspector() {
  if (!S.inspectKey) return '';
  const d = S.inspect;
  const row = S.tracks.find(t => t.content_key === S.inspectKey) || {};
  if (!d || d.content_key !== S.inspectKey) {
    return `<aside class="inspector"><div class="pad"><span class="spin"></span></div></aside>`;
  }
  const value = (f) => {
    if (d.overrides && d.overrides[f] !== undefined) return d.overrides[f];
    const v = d.current[f];
    return v === null || v === undefined ? '' : v;
  };
  const line = (label, text, cls) => text === '' || text === null
    || text === undefined ? ''
    : `<dt>${h(label)}</dt><dd class="${cls || ''}">${h(String(text))}</dd>`;
  const edited = (f) => d.overrides && d.overrides[f] !== undefined
    ? ' <span class="tag">edited</span>' : '';

  // The file's own picture first, the release's as the fallback: a download
  // carries a video frame, and seeing that is the whole reason to look.
  const art = `<div class="art">
      <img src="/api/art/${encodeURIComponent(d.content_key)}" alt=""
        ${d.cover ? `data-fallback="${h(d.cover)}"` : ''}
        onerror="this.dataset.fallback &amp;&amp; this.src !== this.dataset.fallback
          ? (this.src = this.dataset.fallback)
          : (this.style.display = 'none', this.parentNode.classList.add('none'))">
      <span class="none-note">no artwork in this file</span>
    </div>`;

  // The controls first: what to do with this track, before the picture of
  // it. Closing is on the right, away from the three that act on the track,
  // so the one that undoes the panel is not next to the one that rewrites
  // the file.
  const controls = `<div class="hstack tools">
      ${row.id ? `<button class="btn sm" data-play="${row.id}"
        title="Listen to it">${icon('i-play')} Play</button>` : ''}
      <button class="btn sm" data-sel-act="edit">Edit metadata</button>
      <button class="btn sm" data-sel-act="enrich">Identify again</button>
      <span class="grow" style="flex:1"></span>
      <button class="btn sm" data-close-inspector="1" title="Close">&times;</button>
    </div>`;

  return `<aside class="inspector">
    ${controls}
    ${art}
    <div class="in">
      <h3 class="pick">${h(value('title') || (d.rel_path || '').split('/').pop())}${edited('title')}</h3>
      <div class="muted pick">${h(value('artist') || 'unknown artist')}</div>
      <div class="hstack" style="margin:6px 0">
        ${stateTag({ state: d.state, confidence: d.confidence,
                     enrich_source: d.source,
                     overrides: Object.keys(d.overrides || {}).length })}
        ${d.copies > 1 ? `<span class="tag" title="the same audio appears ${d.copies} times">${d.copies} copies</span>` : ''}
        ${d.mbid ? `<a class="tag" target="_blank" rel="noopener"
          href="https://musicbrainz.org/recording/${h(d.mbid)}">musicbrainz</a>` : ''}
      </div>
      <dl class="kv">
        ${line('album', value('album'))}
        ${line('album artist', value('album_artist'))}
        ${line('genre', value('genre') || '\u2014')}
        ${line('released', releaseDate(value('date') || value('year')))}
        ${line('track', [value('track_no'), value('disc_no')
          ? 'disc ' + value('disc_no') : ''].filter(Boolean).join(' \u00b7 '))}
        ${line('length', dur(d.duration))}
        ${line('format', [(row.ext || '').replace('.', '').toUpperCase(),
          row.bitrate ? Math.round(row.bitrate / 1000) + ' kbps' : '',
          bytes(row.size)].filter(Boolean).join(' \u00b7 '))}
        ${line('isrc', value('isrc'))}
        ${line('added', when(row.added_at))}
        ${line('updated', when(row.updated_at))}
        ${line('modified', when(row.mtime))}
      </dl>
      <div class="path pick mono" title="${h(d.path || '')}">${h(d.path || '')}</div>
      ${d.purl ? `<a class="mono" style="font-size:10px;word-break:break-all"
        href="${h(d.purl)}" target="_blank" rel="noreferrer">${h(d.purl)}</a>` : ''}
      ${Object.keys(d.proposed || {}).length && d.state === 'awaiting'
        ? `<div class="notice warn" style="margin-top:8px">${icon('i-warn')}
            <div>A match is waiting on you. Open <b>Edit metadata</b> to see
            it field by field, or accept it here.</div></div>
           <div class="hstack" style="margin-top:6px">
             <button class="btn sm" data-sel-act="accept">Accept</button>
             <button class="btn sm" data-sel-act="reject">Reject</button>
           </div>` : ''}
      ${renderVersions(d)}
      ${renderOrigin(d)}
      <div class="pl-seen">
        <h4>As seen in</h4>
        ${(d.playlists || []).length
          ? `<ul class="pl-list">${d.playlists.map(pl => `<li>
              <button class="linkish clip" data-playlist="${pl.id}"
                title="Open ${h(pl.name)}">${h(pl.name)}</button>
              <span class="faint mono">#${num(pl.pos + 1)} of ${num(pl.entries)}</span>
            </li>`).join('')}</ul>`
          : '<div class="muted">no playlists - this track is in the library '
            + 'only, so nothing carries it to a player.</div>'}
      </div>
    </div>
  </aside>`;
}

/* Where a downloaded track came from, and what it was when it arrived.

   The row above is the file now - renamed, re-tagged, identified. This is
   the file before any of that: the video, every URL that asked for it,
   where yt-dlp first put it and what its tags said then. */
const ORIGIN_FIELDS = [['artist', 'artist'], ['title', 'title'],
  ['album', 'album'], ['album_artist', 'album artist'], ['year', 'year'],
  ['genre', 'genre']];

function renderOrigin(d) {
  const o = d.origin;
  if (!o) return '';
  const was = o.initial_tags || {};
  const now = (f) => (d.overrides && d.overrides[f] !== undefined
    ? d.overrides[f] : d.current[f]);
  const changed = ORIGIN_FIELDS.filter(([f]) => was[f] !== undefined
    && String(was[f]) !== String(now(f) === null || now(f) === undefined ? '' : now(f)));
  return `<div class="pl-seen">
    <h4>Where it came from</h4>
    <dl class="kv">
      ${o.url ? `<dt>video</dt><dd><a class="mono" style="word-break:break-all"
        href="${h(o.url)}" target="_blank" rel="noreferrer">${h(o.url)}</a></dd>` : ''}
      ${(o.sources || []).length ? `<dt>asked for by</dt><dd>${o.sources.map(u =>
        `<div class="mono clip" title="${h(u)}" style="font-size:10px">${h(u)}</div>`).join('')}</dd>` : ''}
      ${o.downloaded_at ? `<dt>downloaded</dt><dd>${h(when(o.downloaded_at))}</dd>` : ''}
      ${o.initial_path ? `<dt>first saved as</dt><dd class="mono pick"
        style="font-size:10px;word-break:break-all">${h(o.initial_path)}</dd>` : ''}
      ${changed.map(([f, label]) => `<dt>${h(label)} was</dt>
        <dd>${h(was[f])}</dd>`).join('')}
    </dl>
    ${o.backfilled ? `<div class="faint" style="font-size:10px">Downloaded
      before this was recorded: what it looked like on arrival is not
      known.</div>` : ''}
  </div>`;
}

// The inspector follows the selection: one row, one subject.
function syncInspector() {
  const key = S.sel.size === 1 ? Array.from(S.sel)[0] : null;
  if (key === S.inspectKey) return;
  S.inspectKey = key;
  S.inspect = null;
  if (!key) return;
  api('/enrich/track/' + encodeURIComponent(key)).then((d) => {
    // The selection may have moved on while the request was in flight.
    if (S.inspectKey === key) { S.inspect = d; render(); }
  }, () => { /* an inspector that cannot load is not an error worth a banner */ });
}

function renderLibrary() {
  const counts = (S.stats && S.stats.enrich_states) || {};
  const stateChip = (key, label) => `<button class="chip ${S.filter.state === key ? 'on' : ''}"
      data-state-filter="${key}">${label}${key
        ? `<span class="n">${num(counts[key] || 0)}</span>` : ''}</button>`;

  return renderBrowser() + `
    <div class="hstack" style="padding:6px 10px;border-bottom:1px solid var(--line-2)">
      <span class="faint mono" style="font-size:9px;letter-spacing:.1em">SYNC COLUMN</span>
      ${S.devices.map(d => `<button class="chip ${d.id === S.scopeId ? 'on' : ''}"
          data-scope="${d.id}" title="${h(d.mounted_at || 'not mounted')}">
          ${h(d.name)}<span class="n">${num(d.files)}</span></button>`).join('')
        || '<span class="faint">no devices paired</span>'}
      <span class="grow" style="flex:1"></span>
      <span style="position:relative">
        <input id="q" placeholder="Search title, artist, album" value="${h(S.filter.q)}"
          style="width:230px;border:1px solid var(--line-2);border-radius:3px;padding:2px 6px 2px 22px">
        <svg width="11" height="11" style="position:absolute;left:6px;top:5px;color:var(--faint)"><use href="#i-search"/></svg>
      </span>
    </div>
    <div class="hstack" style="padding:6px 10px;border-bottom:1px solid var(--line-2)">
      <span class="faint mono" style="font-size:9px;letter-spacing:.1em">METADATA</span>
      ${stateChip('', 'All')}
      ${stateChip('raw', 'Raw')}
      ${stateChip('awaiting', 'Awaiting review')}
      ${stateChip('enriched', 'Enriched')}
      ${stateChip('skipped', 'Skipped')}
      <button class="chip ${S.showVersions ? 'on' : ''}" data-versions-toggle="1"
        title="Also list the versions set aside behind each song's master">Every version</button>
      <span class="grow" style="flex:1"></span>
      <span class="faint" style="font-size:10px">New files are identified and
        tagged automatically. Pick tracks to redo, correct or skip them.</span>
    </div>
    ${trackTable({})}`;
}

/* The inbox: everything indexed since it was last emptied, newest first.
   It exists because a library is the wrong place to find the six tracks
   that arrived this morning - they sort into the middle of an alphabet
   thousands of rows long. Emptying it moves a watermark and nothing else:
   no file is touched, no row leaves the library. */
function renderInbox() {
  const n = S.tracksTotal;
  return `
    <div class="hstack" style="padding:6px 10px;border-bottom:1px solid var(--line-2)">
      <span class="faint mono" style="font-size:9px;letter-spacing:.1em">STILL ARRIVING</span>
      <strong style="font-size:11px">${num(n)} track${n === 1 ? '' : 's'}</strong>
      <span class="faint" style="font-size:10px">Downloads and scanned files
        land here first and leave on their own once they have settled -
        identified, skipped, or a day old. What stays is what is still
        waiting on you.</span>
      <span class="grow" style="flex:1"></span>
      <span style="position:relative">
        <input id="q" placeholder="Search title, artist, album" value="${h(S.filter.q)}"
          style="width:230px;border:1px solid var(--line-2);border-radius:3px;padding:2px 6px 2px 22px">
        <svg width="11" height="11" style="position:absolute;left:6px;top:5px;color:var(--faint)"><use href="#i-search"/></svg>
      </span>
      <button class="btn" data-inbox-seen="1" ${n ? '' : 'disabled'}
        title="Clear the rest now, without waiting for them to settle. Nothing is deleted.">Clear the rest</button>
    </div>
    ${filterNotice()}
    ${trackTable({ inbox: true })}`;
}

/* Tracks no playlist holds, so nothing carries them to a player.

   Asked so they can be put somewhere rather than lost. A playlist that holds
   the whole library - a "DAP-master" - answers the question for every track
   at once, so any playlist can be set aside here and stop counting. */
async function loadUnlisted() {
  S.unlistedIgnore = (await api('/unlisted')).ignore;
  await loadLibrary();
}

function renderUnlisted() {
  const ignored = new Set(S.unlistedIgnore);
  const others = S.playlists.filter(p => !ignored.has(p.name));
  return `
    <div class="hstack" style="padding:6px 10px;border-bottom:1px solid var(--line-2);flex-wrap:wrap">
      <span class="faint mono" style="font-size:9px;letter-spacing:.1em">NOT COUNTING</span>
      ${S.unlistedIgnore.map(n => `<button class="chip on" data-unl-drop="${h(n)}"
          title="Count ${h(n)} again">${h(n)} &times;</button>`).join('')
        || '<span class="faint">every playlist counts</span>'}
      <select id="unl-add" style="border:1px solid var(--line-2);border-radius:3px;padding:1px 4px">
        <option value="">+ ignore a playlist...</option>
        ${others.map(p => `<option value="${h(p.name)}">${h(p.name)}</option>`).join('')}
      </select>
      <span class="grow" style="flex:1"></span>
      <span class="faint" style="font-size:10px">Select tracks and add them to
        the sync list, or open a playlist from the inspector.</span>
    </div>
    ${filterNotice()}
    ${trackTable({})}`;
}

function setUnlistedIgnore(names) {
  return guard(async () => {
    S.unlistedIgnore = (await api('/unlisted', {
      method: 'POST', body: JSON.stringify({ ignore: names }),
    })).ignore;
    S.offset = 0;
    await loadCore();
    await loadLibrary();
  });
}

// A finished job, said out loud. Errors are the point: a run that failed
// used to report `0 enriched` and leave the reason in a counter nobody sees.
function fingerprintReady() {
  return !!(S.stats && S.stats.fingerprint && S.stats.fingerprint.ready);
}

function renderOutcome() {
  const o = S.outcome;
  if (!o) return '';
  // A plain sentence, for the things that are not jobs: a rule added, a
  // list applied. Same place on the page as a finished run's report, so
  // there is one spot to look for "what did that do".
  if (o.text) {
    return `<div class="notice ${o.ok === false ? 'bad' : 'ok'}"
        style="margin:8px 10px">
      ${icon(o.ok === false ? 'i-warn' : 'i-check')}<div>${h(o.text)}</div>
      <span class="grow" style="flex:1"></span>
      <button class="btn sm" data-dismiss-outcome="1">Dismiss</button>
    </div>`;
  }
  const r = o.result || {};
  const errors = r.errors || [];
  const bad = o.state === 'failed' || errors.length
    || (o.kind === 'write-tags' && r.failed);
  const line = o.kind === 'enrich'
    ? [`${num(r.applied || 0)} enriched`,
       `${num(r.candidates || 0)} awaiting review`,
       r.unmatched ? `${num(r.unmatched)} nothing found` : '',
       r.failed ? `${num(r.failed)} failed` : '',
       r.skipped ? `${num(r.skipped)} left alone (skipped)` : '',
      ].filter(Boolean).join(' \u00b7 ')
    : [`${num(r.written || 0)} file${r.written === 1 ? '' : 's'} rewritten`,
       r.failed ? `${num(r.failed)} not written` : '',
      ].filter(Boolean).join(' \u00b7 ');
  const title = o.state === 'failed'
    ? (o.kind === 'enrich' ? 'Lookup failed' : 'Writing tags failed')
    : o.kind === 'enrich' ? 'Lookup finished' : 'Finished writing tags';
  return `<div class="notice ${bad ? 'warn' : 'ok'}"
      style="margin:8px 10px;align-items:flex-start">
    ${icon(bad ? 'i-warn' : 'i-check')}
    <div style="flex:1;min-width:0">
      <strong>${h(title)}</strong> \u2014 ${h(line)}
      ${o.error ? `<div style="margin-top:4px">${h(o.error)}</div>` : ''}
      ${errors.length ? `<ul style="margin:6px 0 0 16px;padding:0">
        ${errors.map(e => `<li style="margin-bottom:2px">${h(e.message)}${
          e.count > 1 ? ` <span class="faint">(${num(e.count)} files)</span>` : ''}${
          e.track ? ` <span class="faint mono" style="font-size:10px">${h(e.track)}</span>` : ''
        }</li>`).join('')}</ul>` : ''}
      ${o.kind === 'enrich' && r.unmatched && !fingerprintReady()
        ? `<div style="margin-top:6px">MusicBrainz had nothing under that
             artist and title. For a download whose artist is a channel name
             the text search has nothing to work with - identifying it by
             sound is what that case needs.
             <code>enrich config --acoustid-key</code> and
             <code>fpcalc</code> turn it on.</div>`
        : ''}
      ${o.kind === 'enrich' && r.candidates
        ? `<div style="margin-top:6px"><button class="btn sm"
             data-state-filter="awaiting">Show the ${num(r.candidates)} awaiting review</button></div>`
        : ''}
    </div>
    <button class="btn sm" data-dismiss-outcome="1">Dismiss</button>
  </div>`;
}

function renderSelectionBar(allShown) {
  const n = S.sel.size;
  if (!n) return '';
  // Which actions make sense is decided by what is actually selected: an
  // Accept over a selection with no candidate in it would be a button that
  // does nothing, which reads as broken rather than as inapplicable.
  const picked = S.tracks.filter(t => S.sel.has(t.content_key));
  const has = (st) => picked.some(t => t.state === st);
  const offPage = n - picked.length;
  const btn = (act, label, key, cls, on) => `<button class="btn sm ${cls || ''}"
      data-sel-act="${act}" ${on === false ? 'disabled' : ''}
      title="${h(label)} (${key})">${label} <span class="faint">${key}</span></button>`;
  return `<div class="hstack" style="padding:6px 10px;gap:6px;
      border-bottom:1px solid var(--line-2);background:var(--sel-2)">
    <strong style="font-size:11px">${num(n)} selected</strong>
    ${offPage ? `<span class="faint" style="font-size:10px">(${num(offPage)} not on this page)</span>` : ''}
    <button class="btn sm" data-sel-act="all-matching">Select all ${num(S.tracksTotal)} matching</button>
    <button class="btn sm" data-sel-act="clear">Clear</button>
    <span class="grow" style="flex:1"></span>
    ${btn('to-sync-list', 'Add to sync list', '+')}
    ${btn('enrich', 'Enrich', 'E', 'primary')}
    ${btn('accept', 'Accept', 'A', '', offPage > 0 || has('awaiting'))}
    ${btn('reject', 'Reject', 'R', '', offPage > 0 || has('awaiting'))}
    ${btn('skip', 'Skip', 'S')}
    ${btn('raw', 'Mark raw', 'U')}
    ${btn('enriched', 'Mark enriched', 'N')}
    ${btn('edit', 'Edit metadata', '\u21b5')}
    ${btn('write', 'Write tags to files', 'W', 'danger')}
  </div>`;
}

// -------------------------------------------------------------- playlist

/* A playlist, shown the way the library is shown.

   It used to be its own table: seven columns of its own, no selection, no
   inspector, no way to play anything or fix a tag from where you noticed it
   was wrong. A playlist is a question about the library - "which of these
   are in it, and in what order" - so it gets the same answer, narrowed.

   What is genuinely its own lives in the header: where it came from, which
   devices carry it, and the entries that never matched a file, which have
   no track row and so cannot be rows in a table of tracks. */
function renderPlaylist() {
  const d = S.playlistDetail;
  if (!d) return '<div class="empty">Select a playlist.</div>';
  const pl = d.playlist;
  const unmatched = d.entries.filter(e => !e.track_id);
  // Only a playlist that knows where it came from can be fetched again.
  const fetchable = pl.origin === 'youtube' && !!pl.source_uri;
  const running = !!(S.job && S.job.kind === 'download'
                     && S.job.state === 'running');

  return `<div class="pad" style="padding-bottom:0">
    <div class="card">
      <header>
        <h3>${h(pl.name)}</h3>
        <span class="tag">${h(originLabel(pl))}</span>
        ${d.fresh ? `<span class="tag new">${num(d.fresh)} new</span>` : ''}
        <span class="muted">${num(d.entries.length)} entries ·
          ${num(d.entries.length - unmatched.length)} matched
          ${unmatched.length ? `· <b style="color:var(--bad)">${
            num(unmatched.length)} unmatched</b>` : ''}</span>
        <span style="flex:1"></span>
        <button class="btn sm" data-pl-tolist="${h(pl.name)}"
          title="Prepare this playlist for a device, connected or not"
          >Add to sync list</button>
        ${fetchable ? `<button class="btn" data-pl-refresh="${pl.id}"
          ${running ? 'disabled' : ''}
          title="Fetch this playlist from ${h(pl.source_uri)} again: new entries, and any track not in the library yet"
          >${icon('i-sync')} ${running ? 'Fetching...' : 'Fetch from ' + h(originLabel(pl))}</button>` : ''}
        ${S.devices.map(dv => {
          const on = d.devices.some(x => x.id === dv.id);
          return `<button class="chip ${on ? 'on' : ''}" data-plsync="${dv.id}"
            data-name="${h(pl.name)}" title="${on ? 'Remove from' : 'Add to'} ${h(dv.name)}">
            ${on ? '&#10003; ' : '+ '}${h(dv.name)}</button>`;
        }).join('')}
      </header>
      ${pl.source_uri ? `<div class="in"><span class="faint mono" style="font-size:10px">
        MIRRORED FROM</span> <a href="${h(pl.source_uri)}" target="_blank"
        rel="noreferrer" class="mono" style="font-size:10.5px">${h(pl.source_uri)}</a></div>` : ''}
      ${S.view === 'playlist' && S.job && S.job.kind === 'download'
        ? `<div class="in">
        ${renderBatch((S.job && S.job.batch) || (S.dlResult && S.dlResult.batch),
                      S.job.state === 'running')}
        <div class="hstack"><span class="${
          S.job.state === 'running' ? 'spin' : ''}"></span>
          <span class="muted clip">${h(S.job.detail || S.job.state)}</span></div>
        ${S.job.state === 'failed' ? `<div class="notice bad" style="margin-top:8px">
          ${icon('i-warn')}<div>${h(S.job.error)}</div></div>` : ''}
      </div>` : ''}
      ${unmatched.length ? `<div class="in">
        <div class="notice warn">${icon('i-warn')}<div>
          ${num(unmatched.length)} entr${unmatched.length === 1 ? 'y names a file' : 'ies name files'}
          the catalog does not hold, so ${unmatched.length === 1 ? 'it is' : 'they are'}
          not in the table below.
          <div class="mono faint" style="margin-top:4px">${unmatched.slice(0, 6)
            .map(e => h(e.title_hint || e.raw_path)).join('<br>')}
            ${unmatched.length > 6 ? `<br>... ${num(unmatched.length - 6)} more` : ''}</div>
        </div></div>
      </div>` : ''}
    </div>
  </div>
  ${filterNotice()}
  ${trackTable()}`;
}

/* What is being filtered out of this page, and one click to stop.

   The library says this with its facet columns, which are always on screen.
   The inbox and a playlist have no such column, so a filter carried in
   from a click elsewhere would be invisible. */
function filterNotice() {
  const f = S.filter;
  if (!anyFilter(f)) return '';
  const bits = [
    f.q ? `search "${h(f.q)}"` : '',
    f.decade ? 'decade ' + h(f.decade) : '',
    f.genre ? 'genre ' + h(f.genre) : '',
    f.artist ? 'artist ' + h(f.artist) : '',
    f.album ? 'album ' + h(f.album) : '',
    f.state ? 'metadata ' + h(f.state) : '',
  ].filter(Boolean);
  return `<div class="hstack" style="padding:5px 10px;border-bottom:1px solid var(--line-2)">
    <span class="tag warn">FILTERED</span>
    <span class="muted">showing only ${bits.join(' · ')}</span>
    <span class="grow" style="flex:1"></span>
    <button class="btn sm" data-clear-filters="1">Clear</button>
  </div>`;
}

/* A card packed before there is a card.

   Choosing what goes on a player used to need the player: the rules lived
   on a device row, so deciding anything meant finding the cable first. This
   holds the same rules against no device at all - prepare it on the train,
   apply it when you get home - and because the rules are rules rather than
   a frozen list of files, a playlist that gains a track gains it here too.
*/
function renderSyncList() {
  const d = S.syncList;
  if (!d) return '<div class="empty"><span class="spin"></span></div>';
  const KIND = { playlist: 'PLAYLIST', artist: 'ARTIST', album: 'ALBUM',
                 track: 'TRACK' };
  const label = (r) => r.kind === 'track'
    ? (r.ref.split('/').pop() || r.ref) : r.ref;

  return `${renderOutcome()}<div class="pad stack">
    <div class="card">
      <header><h3>Prepared to sync</h3>
        <span class="tag">${num(d.tracks)} tracks</span>
        <span class="tag">${h(bytes(d.bytes))}</span>
        <span class="muted">no device needed to build this. Rules, not a
          frozen list - a playlist that gains a track gains it here.</span>
        <span class="grow" style="flex:1"></span>
        ${d.rules.length ? '<button class="btn sm" data-sl-clear="1">Empty the list</button>' : ''}
      </header>
      <div class="in stack">
        ${d.rules.length ? `<table class="tbl"><tbody>
          ${d.rules.map(r => `<tr>
            <td style="width:74px"><span class="tag">${h(KIND[r.kind] || r.kind)}</span></td>
            <td class="clip" title="${h(r.ref)}">${h(label(r))}</td>
            <td class="mono num" style="width:120px">${num(r.tracks)} tracks</td>
            <td class="mono num" style="width:90px">${h(bytes(r.bytes))}</td>
            <td style="width:70px"><button class="btn sm"
              data-sl-remove="${h(r.kind)}" data-ref="${h(r.ref)}">Drop</button></td>
          </tr>`).join('')}
        </tbody></table>` : `<div class="empty">Nothing prepared yet. Select
          tracks in the library and press <b>Add to sync list</b>, or add a
          whole playlist from its page.</div>`}
      </div>
    </div>

    <div class="card">
      <header><h3>Send it to a device</h3>
        <span class="muted">the rules are copied onto the device; the list
          stays as it is, because the same set usually goes on more than one
          card.</span></header>
      <div class="in stack">
        ${d.devices.length ? d.devices.map(dv => `<div class="hstack">
          <span class="clip" style="flex:1"><b>${h(dv.name)}</b>
            <span class="faint mono" style="font-size:10px">${dv.mounted_at
              ? h(dv.mounted_at) : 'not connected - the rules will be waiting'
            }</span></span>
          <button class="btn ${dv.mounted_at ? 'primary' : ''}"
            data-sl-apply="${dv.id}" ${d.rules.length ? '' : 'disabled'}
            >Add to ${h(dv.name)}</button>
        </div>`).join('') : `<div class="empty">No devices paired yet. The list
          keeps until there is one.</div>`}
      </div>
    </div>
  </div>`;
}

/* Not built yet, and saying so.

   Loudness is the one thing a portable player cannot fix for you: an album
   mastered quiet and a single mastered loud sit next to each other in a
   playlist and you reach for the volume knob every third track. The plan is
   to measure each file once and either write the gain tags a player already
   understands or apply it on the way to the card - the second being the
   only thing that helps a player that ignores the tags.

   This page exists so the intention is visible and the shape is agreed
   before anything writes to anybody's files. */
function renderNormalize() {
  const st = S.stats || {};
  return `<div class="pad stack">
    <div class="notice warn">${icon('i-warn')}<div>
      <b>Not built yet.</b> Nothing on this page does anything to your files.
      It is here to say what is planned and to be argued with first.</div></div>

    <div class="card">
      <header><h3>What it would do</h3></header>
      <div class="in stack">
        <p class="muted">Measure every track once with EBU R128 - the same
          loudness measure streaming services use - and store the result
          beside the track, so a file is measured once and not again on
          every sync.</p>
        <p class="muted">Then one of two things, per device, because players
          differ:</p>
        <ul class="muted" style="margin-left:16px">
          <li><b>Write the tags.</b> <span class="mono">replaygain_track_gain</span>
            and <span class="mono">replaygain_album_gain</span>, which a
            player that understands them applies without re-encoding. The
            audio is untouched; only the tags change.</li>
          <li><b>Apply on the way out.</b> For a player that ignores the
            tags: the gain is baked into the copy written to the card, and
            the library keeps its original.</li>
        </ul>
        <p class="muted">Album gain and track gain both, kept apart: an album
          played end to end wants its own quiet and loud passages left as the
          record has them, while a shuffled playlist wants every track at the
          same level.</p>
      </div>
    </div>

    <div class="card">
      <header><h3>What it needs</h3></header>
      <div class="in stack">
        <div class="hstack"><span class="tag ${
          (S.dl && S.dl.bundled && S.dl.bundled.tools
           && S.dl.bundled.tools.ffmpeg) ? 'ok' : ''}">ffmpeg</span>
          <span class="muted">already used for downloading; it carries the
            loudness filter this would measure with.</span></div>
        <div class="hstack"><span class="tag">${num(st.tracks || 0)} tracks</span>
          <span class="muted">to measure the first time, once each.</span></div>
      </div>
      <div class="in hstack">
        <button class="btn" disabled title="Not built yet">Measure the library</button>
        <span class="faint">disabled until this is real.</span>
      </div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- device

/* Playlist files the sync would leave sitting next to the ones it writes.
   This happens when the device spells its playlists its own way - HiBy's
   are "<name>-<owner>.m3u8" - and it is the one thing a dry run listing
   only its own output cannot tell you: the card ends up showing both, the
   stale one included. */
function playlistStrayNotice(d, p) {
  const strays = (p.playlist_strays || []);
  const shadowing = strays.filter(x => x.shadows);
  if (!strays.length) return '';
  const sample = strays.slice(0, 4).map(x => x.filename).join(', ');
  const more = strays.length > 4 ? ` and ${num(strays.length - 4)} more` : '';
  return `<div class="notice warn" style="margin-top:10px">${icon('i-warn')}
    <div><b>${num(strays.length)} playlist file${strays.length > 1 ? 's' : ''}
    already on the card would be left in place.</b><br>
    <span class="mono faint">${h(sample)}</span>${h(more)}
    ${shadowing.length ? `<br>This device names its playlists differently, so
      the sync would add a second copy of ${num(shadowing.length)} of them
      rather than replacing what is there.
      <button class="btn sm" data-detect-pl="${d.id}">Use the card's naming</button>`
      : ''}</div></div>`;
}

function renderDevice() {
  const d = S.devices.find(x => x.id === S.deviceId);
  if (!d) return '<div class="empty">Select a device.</div>';
  const p = S.plan;
  const job = S.job && S.job.kind === 'sync' ? S.job : null;
  const running = job && job.state === 'running';
  const space = d.space;

  const rules = d.rules.map(r => `
    <span class="chip">${h(r.kind)}: ${h(r.ref)}
      <button class="btn sm" data-unset="${h(r.kind)}|${h(r.ref)}" title="Remove">&times;</button>
    </span>`).join('') || '<span class="faint">nothing selected yet</span>';

  let planBody;
  if (!d.mounted_at) {
    planBody = `<div class="notice warn">${icon('i-warn')}
      <div><b>${h(d.name)} is not mounted.</b><br>
      Plug it in; Lemon Zest finds it by its volume label and the marker file at
      the card root. Last seen at <span class="mono">${h(d.root || 'unknown')}</span>.</div></div>`;
  } else if (S.planning) {
    planBody = '<div class="hstack"><span class="spin"></span> working out what needs to change...</div>';
  } else if (S.planError) {
    planBody = `<div class="notice bad">${icon('i-warn')}<div>${h(S.planError)}</div></div>`;
  } else if (p) {
    const nothing = !p.copies_total && !p.deletes_total && !p.playlist_deletes.length;
    planBody = `
      <dl class="kv">
        <dt>tracks in set</dt><dd>${num(p.tracks_desired)}</dd>
        <dt>already correct</dt><dd>${num(p.unchanged)}</dd>
        <dt>to copy</dt><dd><b>${num(p.copies_total)}</b> \u00b7 ${bytes(p.bytes_in)}</dd>
        <dt>to remove</dt><dd>${num(p.deletes_total)} \u00b7 ${bytes(p.bytes_out)}</dd>
        <dt>playlists</dt><dd>${num(p.playlists.length)} to write${
          p.playlist_deletes.length ? `, ${num(p.playlist_deletes.length)} to remove` : ''}
          ${p.playlist_template ? `<span class="faint mono">as ${h(p.playlist_template)}</span>` : ''}</dd>
        ${p.missing_total ? `<dt>skipped</dt><dd style="color:var(--warn)">${num(p.missing_total)}
          <span class="faint">${h([...new Set(p.missing_source.map(m => m.why))].join(', '))}</span></dd>` : ''}
      </dl>
      ${playlistStrayNotice(d, p)}
      ${!p.space.fits
        ? `<div class="notice bad" style="margin-top:10px">${icon('i-warn')}
            <div><b>This will not fit.</b> Short by ${bytes(p.space.shortfall)}.
            Nothing has been written - untick something and plan again.</div></div>`
        : nothing
          ? `<div class="notice ok" style="margin-top:10px">${icon('i-check')}
              <div>The card is already correct. Nothing to copy.</div></div>`
          : ''}
      ${p.copies.length ? `<div style="margin-top:10px">
        <div class="faint mono" style="font-size:9px;letter-spacing:.1em;margin-bottom:4px">FIRST ${Math.min(8, p.copies.length)} OF ${num(p.copies_total)} COPIES</div>
        ${p.copies.slice(0, 8).map(c => `<div class="mono clip" style="font-size:10.5px;color:var(--ink-3)">
          <span style="color:var(--ok)">+</span> ${h(c.rel)}
          <span class="faint">${h(c.reason)}, ${bytes(c.size)}</span></div>`).join('')}
      </div>` : ''}
      ${p.deletes.length ? `<div style="margin-top:8px">
        <div class="faint mono" style="font-size:9px;letter-spacing:.1em;margin-bottom:4px">FIRST ${Math.min(6, p.deletes.length)} OF ${num(p.deletes_total)} REMOVALS</div>
        ${p.deletes.slice(0, 6).map(c => `<div class="mono clip" style="font-size:10.5px;color:var(--ink-3)">
          <span style="color:var(--bad)">-</span> ${h(c.rel)}
          <span class="faint">${h(c.reason)}</span></div>`).join('')}
      </div>` : ''}`;
  } else {
    planBody = '<div class="faint">Press <b>Dry run</b> to see what a sync would do.</div>';
  }

  const usage = space ? `
    <div class="usage">
      <i class="existing" style="width:${(100 * space.used / space.total).toFixed(1)}%"></i>
      <i class="adding" style="width:${p ? Math.min(100 * p.bytes_in / space.total, 100 - 100 * space.used / space.total).toFixed(1) : 0}%"></i>
    </div>
    <div class="legend">
      <span><i style="background:#b3aa98"></i>In use ${bytes(space.used)}</span>
      ${p && p.bytes_in ? `<span><i style="background:var(--accent)"></i>Adding ${bytes(p.bytes_in)}</span>` : ''}
      <span><i style="background:#ddd6c8"></i>Free ${bytes(space.free)}</span>
    </div>` : '<div class="faint">Not mounted - no capacity reading.</div>';

  return `<div class="pad stack">
    <div class="card">
      <header>
        <h3>${h(d.name)}</h3>
        <span class="tag">${h(d.profile)}</span>
        ${d.label ? `<span class="tag">${h(d.label)}</span>` : ''}
        <span class="muted mono" style="font-size:10.5px">
          ${d.mounted_at ? h(d.mounted_at) : 'not mounted'} \u00b7
          ${num(d.files)} files \u00b7 ${bytes(d.bytes)}</span>
        <span style="flex:1"></span>
        <span class="faint" style="font-size:10.5px">Last sync ${h(when(d.last_sync))}</span>
        <button class="btn" data-plan="${d.id}" ${!d.mounted_at || running ? 'disabled' : ''}>Dry run</button>
        <button class="btn primary" data-sync="${d.id}"
          ${!d.mounted_at || running || (p && !p.space.fits) ? 'disabled' : ''}>
          ${running ? 'Syncing...' : 'Sync now'}</button>
      </header>
      ${running ? `<div class="in">
        <div class="hstack" style="margin-bottom:6px">
          <b>${job.total ? Math.round(100 * job.done / job.total) : 0}%</b>
          <span class="muted">${num(job.done)} of ${num(job.total)} files</span>
          <span style="flex:1"></span>
          <span class="mono faint clip" style="font-size:10px;max-width:50%">${h(job.detail || '')}</span>
        </div>
        <div class="bar"><i style="width:${job.total ? (100 * job.done / job.total) : 0}%"></i></div>
        <div class="muted" style="margin-top:6px;font-size:11px">
          Safe to keep browsing. Don't unplug the card.</div>
      </div>` : ''}
      ${job && job.state === 'failed' ? `<div class="in"><div class="notice bad">
        ${icon('i-warn')}<div>${h(job.error)}</div></div></div>` : ''}
      ${job && job.state === 'done' && job.result ? `<div class="in"><div class="notice ok">
        ${icon('i-check')}<div><b>Done.</b>
        ${num(job.result.copied)} copied (${bytes(job.result.bytes)}),
        ${num(job.result.deleted)} removed,
        ${num(job.result.playlists)} playlists written,
        ${num(job.result.skipped_unchanged)} already correct.
        ${job.result.failed ? `<br><b style="color:var(--bad)">${num(job.result.failed)} failed.</b>
          ${h((job.result.errors || []).slice(0, 3).join('; '))}` : ''}
        </div></div></div>` : ''}
    </div>

    <div class="grid two">
      <div class="card">
        <header><h3>What goes on it</h3><span style="flex:1"></span>
          <button class="btn sm" data-pick-set="${d.id}">Choose playlists</button></header>
        <div class="in"><div class="hstack">${rules}</div></div>
      </div>
      <div class="card">
        <header><h3>Plan</h3></header>
        <div class="in">${planBody}</div>
      </div>
    </div>

    <div class="grid two">
      <div class="card">
        <header><h3>Card usage</h3></header>
        <div class="in">${usage}</div>
      </div>
      <div class="card">
        <header><h3>Activity</h3><span style="flex:1"></span>
          <span class="faint mono" style="font-size:9px">NEWEST FIRST</span></header>
        <div class="log" style="max-height:220px;overflow:auto">
          ${S.log.length ? S.log.map(l => `<div class="line">
            <span class="t">${h(clock(l.at))}</span>
            <span class="k ${h(l.kind)}">${h(l.kind)}</span>
            <span class="m" title="${h(l.detail || '')}">${h(l.detail || '')}</span>
            ${l.size ? `<span class="t">${bytes(l.size)}</span>` : ''}
          </div>`).join('') : '<div class="empty">Nothing yet.</div>'}
        </div>
      </div>
    </div>
  </div>`;
}

// ------------------------------------------------------- other views

function renderAddDevice() {
  return `<div class="pad stack">
    <div class="card">
      <header><h3>Add a device</h3><span style="flex:1"></span>
        <button class="btn" data-volumes="1">Rescan volumes</button></header>
      <div class="in stack">
        <p class="muted">Pick a mounted volume, or type a folder to treat as
          one. Lemon Zest identifies a device by its volume label and writes a
          <span class="mono">.lemon-zest-id</span> marker at the root, so two cards
          with the same name never get their plans crossed.</p>
        <div id="volumes"><span class="spin"></span></div>
        <form id="add-device-form" class="grid two" style="align-items:end">
          <label class="field"><span>Folder or drive</span>
            <input name="root" placeholder="E:/ or a folder path" required></label>
          <label class="field"><span>Name</span>
            <input name="name" placeholder="HiBy R1"></label>
          <label class="field"><span>Profile</span>
            <select name="profile">
              <option value="hiby">HiBy OS DAP - m3u8, encoded paths</option>
              <option value="ums">Generic USB mass storage - m3u8</option>
              <option value="rockbox">Rockbox - playlists in /Playlists</option>
            </select></label>
          <div class="field"><button class="btn primary" type="submit">Pair device</button></div>
        </form>
      </div>
    </div>
  </div>`;
}

/* Where music comes into the library from: a folder that is already on
   this machine, a folder of playlists, or a URL. All three used to be two
   pages - "Scan & import" and "Download" - which meant the answer to "how
   do I get music in" depended on where it happened to be already. */
function renderSources() {
  const st = S.stats || {};
  const job = S.job;
  return `
    <div class="card">
      <header><h3>Library folders</h3>
        <span class="muted">music already on this machine. A scan also reads
          the playlists inside the folder.</span></header>
      <div class="in stack">
        ${(st.root_detail || []).map(r => `<div class="hstack">
          <span class="mono clip" style="flex:1;${r.hidden
            ? 'color:var(--fainter);text-decoration:line-through' : ''}"
            title="${h(r.root)}">${h(r.root)}</span>
          ${r.hidden ? '<span class="tag">hidden</span>' : ''}
          ${r.exists === false ? `<span class="tag warn"
            title="Moved, renamed, or brought from another computer">not found</span>
            <input data-relocate-to="${h(r.root)}" placeholder="where is it now?"
              style="width:180px;border:1px solid var(--line-2);border-radius:3px;padding:2px 6px">
            <button class="btn sm" data-relocate="${h(r.root)}">Point here</button>` : ''}
          <span class="faint mono" style="font-size:10px">${num(r.tracks)} tracks
            \u00b7 ${bytes(r.bytes)}</span>
          <button class="btn sm" data-scan="${h(r.root)}"
            ${r.hidden ? 'disabled' : ''}>Rescan</button>
          <button class="btn sm" data-root-hide="${h(r.root)}"
            data-hidden="${r.hidden ? '0' : '1'}"
            title="${r.hidden
              ? 'Show this folder in the library again'
              : 'Keep it indexed but leave it out of the library, the facets and the inbox'
            }">${r.hidden ? 'Show' : 'Hide'}</button>
          <button class="btn sm danger" data-root-remove="${h(r.root)}"
            title="Forget this folder and its tracks. No file is deleted."
            >Remove</button></div>`).join('')
          || '<span class="faint">No folders indexed yet.</span>'}
        <span class="faint" style="font-size:10.5px">Hiding keeps the catalog
          and drops the folder out of the library; removing forgets its rows.
          Neither one deletes a file.</span>
        <form id="scan-form" class="hstack">
          <input name="root" placeholder="C:/Users/you/Music/library" required
            style="flex:1;border:1px solid var(--line-2);border-radius:3px;padding:4px 7px">
          <button class="btn primary" type="submit">Scan folder</button>
        </form>
        ${job && job.kind === 'scan' ? `<div>
          <div class="hstack"><span class="${job.state === 'running' ? 'spin' : ''}"></span>
            <span class="muted">${h(job.detail || job.state)}</span></div>
          <div class="bar" style="margin-top:5px"><i style="width:${
            job.total ? (100 * job.done / job.total) : 0}%"></i></div>
          ${job.state === 'done' && job.result ? `<div class="notice ok" style="margin-top:8px">
            ${icon('i-check')}<div>${num(job.result.seen)} files -
            ${num(job.result.added)} added, ${num(job.result.updated)} updated,
            ${num(job.result.unchanged)} unchanged, ${num(job.result.removed)} removed.</div></div>` : ''}
          ${job.state === 'failed' ? `<div class="notice bad" style="margin-top:8px">
            ${icon('i-warn')}<div>${h(job.error)}</div></div>` : ''}
        </div>` : ''}
      </div>
    </div>
    <div class="card">
      <header><h3>Import playlists</h3></header>
      <div class="in stack">
        <p class="muted">Point this at a folder of m3u/m3u8 files. Importing a
          second folder that holds the same playlists enriches the first rather
          than replacing it, so the copy whose paths resolve and the copy that
          carries the provider URIs both count.</p>
        <form id="pl-form" class="hstack">
          <input name="directory" placeholder="C:/Users/you/Music/playlists" required
            style="flex:1;border:1px solid var(--line-2);border-radius:3px;padding:4px 7px">
          <button class="btn primary" type="submit">Import</button>
        </form>
        <div id="pl-result"></div>
      </div>
    </div>`;
}

/* What each file that has landed is doing now.

   A file is not finished when yt-dlp stops writing it: it still has to be
   catalogued, looked up and tagged, and until that happens it sits in the
   library under a channel name with a video frame for a cover. Those steps
   run per file as it arrives, so the row says which one it is on. */
const ITEM = {
  queued:      { tag: '',     label: 'waiting' },
  indexing:    { tag: '',     label: 'cataloguing' },
  identifying: { tag: '',     label: 'identifying' },
  done:        { tag: 'ok',   label: 'done' },
  skipped:     { tag: '',     label: 'skipped' },
  failed:      { tag: 'warn', label: 'failed' },
};

function renderItems(items) {
  if (!items || !items.length) return '';
  // Newest first: the interesting end of a forty-track run is the one still
  // moving, and it would otherwise walk off the bottom of the card.
  const rows = items.slice().reverse().map(it => {
    const spec = ITEM[it.state] || ITEM.queued;
    const busy = it.state === 'indexing' || it.state === 'identifying';
    return `<div class="item ${h(it.state)}">
      <span class="faint mono n">${num(it.n)}</span>
      <span class="clip" style="flex:1" title="${h(it.path || it.title)}"
        >${h(it.title)}</span>
      ${busy ? '<span class="spin"></span>' : ''}
      <span class="clip detail" title="${h(it.detail || '')}">${
        h(it.detail || '')}</span>
      <span class="tag ${spec.tag}">${h(spec.label)}</span>
    </div>`;
  }).join('');
  return `<div class="items">${rows}</div>`;
}

/* The identification queue, as a card.

   Everything that arrives goes through here, one file at a time, because
   MusicBrainz answers one request a second and asking faster gets the whole
   program refused for a while. That is worth showing rather than hiding:
   a download that finishes in a minute and a queue that takes ten is not a
   program that has stopped. */
function renderQueue() {
  const q = (S.stats || {}).enrich_queue;
  if (!q) return '';
  const busy = q.waiting || q.current;
  if (!busy && !q.done) return '';
  const STATE = { applied: 'ok', candidate: 'warn', none: '', failed: 'warn',
                  skipped: '' };
  return `<div class="card">
    <header><h3>Identifying</h3>
      ${busy ? `<span class="tag">${num(q.waiting)} waiting</span>` : ''}
      ${q.deferred ? `<span class="tag warn">${num(q.deferred)} retrying</span>` : ''}
      ${q.paused ? '<span class="tag warn">paused</span>' : ''}
      <span class="muted">one file at a time, because MusicBrainz answers
        one request a second${q.interval && q.interval > 1.5
          ? ` - currently ${q.interval.toFixed(1)}s, having been asked to slow down`
          : ''}.</span>
      <span class="grow" style="flex:1"></span>
      ${busy ? `<button class="btn sm" data-queue="${q.paused ? 'resume' : 'pause'}"
        >${q.paused ? 'Resume' : 'Pause'}</button>` : ''}
      ${q.waiting ? '<button class="btn sm" data-queue="clear">Drop the backlog</button>' : ''}
    </header>
    <div class="in stack">
      <div class="hstack">
        <span class="muted">${[
          q.applied ? `${num(q.applied)} identified` : '',
          q.candidates ? `${num(q.candidates)} to review` : '',
          q.unmatched ? `${num(q.unmatched)} not found` : '',
          q.gave_up ? `${num(q.gave_up)} gave up` : '',
        ].filter(Boolean).join(' \u00b7 ') || 'nothing yet'}</span>
        ${q.current ? `<span class="spin"></span>` : ''}
      </div>
      ${q.last_error && q.deferred ? `<div class="notice warn">${icon('i-warn')}
        <div>${h(q.last_error)} - trying again shortly.</div></div>` : ''}
      ${(q.recent || []).length ? `<table class="tbl"><tbody>
        ${q.recent.map(r => `<tr>
          <td class="clip" title="${h(r.label || r.key)}">${h(r.label || r.key)}</td>
          <td class="clip muted" style="width:45%">${h(r.detail || '')}</td>
          <td style="width:78px"><span class="tag ${STATE[r.state] || ''}"
            >${h(r.state || '')}</span></td>
        </tr>`).join('')}
      </tbody></table>` : ''}
    </div>
  </div>`;
}

/* Downloads paused or stopped part way, and what each has left.

   A library of thousands takes hours; a run that was paused - or cut off by
   closing the program - is kept here and resumed with one click, which
   fetches only what it had not reached. */
function renderPaused(d) {
  const rows = d.paused || [];
  if (!rows.length) return '';
  const running = !!(S.job && S.job.kind === 'download'
                     && S.job.state === 'running');
  return `<div class="card">
    <header><h3>Paused downloads</h3><span class="tag">${num(rows.length)}</span></header>
    <div class="in scrollbox">${rows.map(r => `<div class="urlrow">
      <span class="clip" style="flex:1" title="${h(r.urls.join(' '))}">
        <b>${h(r.label)}</b>
        <span class="tag">${num(r.remaining.length)} left</span>
        <span class="u">${h(r.state)} ${h(ago(r.at))}</span></span>
      <button class="btn sm primary" data-dl-resume="${h(r.id)}"
        ${running ? 'disabled' : ''}>Resume</button>
      <button class="btn sm" data-dl-forget="${h(r.id)}">Forget</button>
    </div>`).join('')}</div>
  </div>`;
}

/* The last few URLs asked for.

   The alternative is going back to the browser to find the link again,
   which is the one part of downloading that Lemon Zest was making worse
   rather than better. Click one and it goes back in the box. */
function renderRecent(d) {
  const rows = d.recent || [];
  if (!rows.length) return '';
  return `<div class="card">
    <header><h3>Recent URLs</h3>
      <span class="muted">the last ${num(rows.length)} asked for - click one
        to put it back in the box.</span></header>
    <div class="in">${rows.map(r => `<div class="urlrow">
      <button class="btn sm" data-useurl="${h(r.url)}">Use</button>
      <span class="clip" style="flex:1" title="${h(r.url)}">
        ${h(r.title || r.url)}
        ${r.is_playlist ? `<span class="tag">${num(r.item_count)} items</span>` : ''}
        ${/list=OLAK5uy_/.test(r.url) ? '<span class="tag">album</span>' : ''}
        <span class="u">${h(r.url)}</span></span>
      <span class="faint mono" style="font-size:10px">${h(ago(r.last_used))}</span>
      ${r.is_playlist && !r.kept ? `<button class="btn sm" data-keep="${h(r.url)}"
        title="Keep this playlist and fetch it again later"
        >${icon('i-keep')} Keep</button>` : ''}
    </div>`).join('')}</div>
  </div>`;
}

/* The playlists being kept an eye on.

   A playlist somebody follows is not a one-off download: tracks are added
   to it for as long as it exists. Keeping the URL means the answer to "has
   anything been added" is one click, and that click fetches only what is
   new - the download archive skips the rest - appends it to the same
   playlist, and rewrites the playlist file beside the library. Syncing it
   to a player is still a sync, done when you choose. */
function renderKept(d) {
  const rows = d.kept || [];
  if (!rows.length) return '';
  const running = !!(S.job && S.job.kind === 'download'
                     && S.job.state === 'running');
  return `<div class="card">
    <header><h3>Kept playlists</h3>
      <span class="tag">${num(rows.length)}</span>
      <span class="muted">fetch one again and it takes whatever is new,
        nothing else.</span></header>
    <div class="in">${rows.map(r => `<div class="urlrow">
      <span class="clip" style="flex:1" title="${h(r.url)}">
        <b>${h(r.playlist_name || r.title || r.url)}</b>
        <span class="tag">${num(r.entries)} in the playlist</span>
        <span class="u">${h(r.url)}</span></span>
      <span class="faint mono" style="font-size:10px">${r.last_checked
        ? 'checked ' + h(ago(r.last_checked))
          + (r.last_added ? ', ' + num(r.last_added) + ' added' : ', nothing new')
        : 'never checked'}</span>
      <button class="btn sm" data-keeprun="${h(r.url)}" ${running ? 'disabled' : ''}
        >${icon('i-sync')} Update</button>
      <button class="btn sm" data-unkeep="${h(r.url)}">Forget</button>
    </div>`).join('')}</div>
  </div>`;
}

/* A download of forty tracks, as one picture.

   The old page had two numbers - the bytes of whatever file yt-dlp happened
   to be fetching, and a log - and neither answers "how far through is it".
   This one counts items: how many the batch set out to fetch, how many are
   done, and what became of each. The bytes are still here, but where they
   belong: on the line describing the track being fetched right now. */
function renderBatch(batch, running) {
  if (!batch) return '';
  const total = batch.total || 0;
  const done = Math.min(batch.done || 0, total || batch.done || 0);
  const pct = total ? Math.min(100, (100 * done) / total) : 0;
  const elapsed = (batch.finished || Date.now() / 1000) - batch.started;
  // Per-item average rather than per-byte: items are what is left to do,
  // and a three-minute track and a twenty-second one average out over a
  // playlist in a way bytes-per-second never does mid-file.
  const left = done > 0 && total > done && running
    ? (elapsed / done) * (total - done) : null;

  // One bar per worker: several videos are fetched at once, and each has
  // its own bytes. Older reports carry only `current`.
  const active = (batch.active && batch.active.length
    ? batch.active : [batch.current]).filter(Boolean);
  const nowBar = (c) => {
    const cpct = c.bytes_total ? (100 * c.bytes) / c.bytes_total : 0;
    return `<div class="now">
        <div class="hstack">
          <span class="tag">NOW</span>
          <span class="clip" style="flex:1" title="${h(c.title)}">${h(c.title)}</span>
          ${c.index && c.count ? `<span class="faint mono" style="font-size:10px"
            >#${num(c.index)} of ${num(c.count)} in this list</span>` : ''}
        </div>
        <div class="bar" style="margin-top:5px"><i style="width:${cpct.toFixed(1)}%"></i></div>
        <div class="hstack faint mono" style="font-size:10px;margin-top:4px">
          <span>${h(bytes(c.bytes))}${c.bytes_total
            ? ' of ' + h(bytes(c.bytes_total)) : ''}</span>
          <span class="grow" style="flex:1"></span>
          <span>${h([rate(c.speed), c.eta ? span(c.eta) + ' left' : '']
            .filter(Boolean).join(' \u00b7 '))}</span>
        </div>
      </div>`;
  };
  // Identified, not merely fetched: a track is only finished when its tags
  // are right, so that is the number worth putting beside the downloads.
  const named = (batch.items || []).filter(i => i.enrich === 'applied').length;
  // Named for what they are, so the three of them visibly account for the
  // total. "Already had" read as "already in this playlist" - which is a
  // different number, printed a few lines further down - and left the sum
  // looking like it did not work out.
  const counted = [
    batch.downloaded ? `${num(batch.downloaded)} fetched` : '',
    named ? `${num(named)} identified` : '',
    batch.skipped ? `${num(batch.skipped)} already downloaded before` : '',
    batch.failed ? `${num(batch.failed)} could not be fetched` : '',
  ].filter(Boolean).join(' \u00b7 ');
  const sum = (batch.downloaded || 0) + (batch.skipped || 0) + (batch.failed || 0);
  const reckoning = total
    ? `${num(total)} item${total === 1 ? '' : 's'} asked for: `
      + `${num(batch.downloaded || 0)} fetched, `
      + `${num(batch.skipped || 0)} already in the download archive, `
      + `${num(batch.failed || 0)} could not be fetched`
      + (sum === total ? '' : ` (${num(total - sum)} still going)`)
    : '';

  return `<div class="card batch">
    <header>
      <h3>${running ? 'Downloading' : 'Batch'}</h3>
      ${running ? '<span class="spin"></span>' : ''}
      <span class="count">${num(done)} <span class="of">of</span> ${
        total ? num(total) : '?'}</span>
      <span class="muted">item${total === 1 ? '' : 's'}${
        batch.urls > 1 ? ` from ${num(batch.urls)} URLs` : ''}</span>
      <span class="grow" style="flex:1"></span>
      <span class="faint mono" style="font-size:10px">${
        h(span(elapsed))} elapsed${left !== null ? ` \u00b7 ~${h(span(left))} left` : ''}</span>
    </header>
    <div class="in">
      <div class="bar big"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="hstack" style="margin-top:6px">
        <span class="muted" title="${h(reckoning)}">${counted || (batch.listed
          ? 'nothing fetched yet' : 'listing what is at the URLs...')}</span>
        <span class="grow" style="flex:1"></span>
        <span class="faint mono" style="font-size:10px">${pct.toFixed(0)}%</span>
      </div>
      ${active.slice(0, 4).map(nowBar).join('')}
      ${running ? `<div class="hstack" style="margin-top:8px">
        <span class="faint" style="font-size:10px">${num(batch.workers || 1)}
          at a time. Pause lets these finish; Stop cuts them off. Either way
          the rest can be resumed later.</span>
        <span class="grow" style="flex:1"></span>
        <button class="btn sm" data-dl-halt="pause">Pause</button>
        <button class="btn sm" data-dl-halt="stop">Stop now</button>
      </div>` : ''}
      ${!running && batch.stopped ? `<div class="notice warn" style="margin-top:8px">
        ${icon('i-warn')}<div>${h(batch.stopped === 'stopped' ? 'Stopped' : 'Paused')}
        with ${num(batch.remaining)} item${batch.remaining === 1 ? '' : 's'}
        not fetched. Resume them from <b>Paused downloads</b> below.</div></div>` : ''}
      ${renderItems(batch.items)}
    </div>
  </div>`;
}

function renderDownload() {
  const d = S.dl;
  if (!d) {
    return `<div class="empty"><span class="spin"></span>
      <div style="margin-top:8px">Checking what is installed...</div></div>`;
  }
  const cfg = d.config, ck = d.cookies;
  const js = d.js_runtime || { found: [], chosen: null, detail: '' };
  const bn = d.bundled || { frozen: false, tools: {} };
  const bundledTool = (name) => bn.frozen && bn.tools && bn.tools[name];
  const job = S.job && S.job.kind === 'download' ? S.job : null;
  const res = S.dlResult;
  const failed = !!(job && job.state === 'failed');
  // While it runs, the log is the event stream; once it is over, the run's
  // own log, which also carries what happened after yt-dlp exited - what
  // was catalogued, and what went into a playlist.
  const logLines = (res && res.log && res.log.length) ? res.log
    : (job && job.events ? job.events.map(e => e.text) : []);
  const field = 'border:1px solid var(--line-2);border-radius:3px;padding:4px 7px';
  const roots = d.roots.slice();
  if (cfg.root && !roots.includes(cfg.root)) roots.unshift(cfg.root);

  const cookieTag = ck.source === 'firefox'
    ? '<span class="tag ok">FIREFOX</span>'
    : ck.source === 'file' ? '<span class="tag ok">COOKIES.TXT</span>'
      : '<span class="tag warn">NO COOKIES</span>';

  return `<div class="pad stack">
    ${d.ytdlp ? '' : `<div class="notice bad">${icon('i-warn')}<div>
      yt-dlp is not installed, so nothing can be downloaded yet.
      Install it with <span class="mono">pip install yt-dlp</span>
      (and ffmpeg, which it uses to extract the audio).</div></div>`}

    ${bn.frozen ? `<div class="notice ok">${icon('i-check')}<div>
      <b>Everything downloading needs is inside this executable.</b>
      yt-dlp, ffmpeg, Deno and fpcalc ship with it and are used without
      being installed - ${['ffmpeg', 'deno', 'fpcalc']
        .filter(n => bundledTool(n)).join(', ') || 'none found in the bundle'}.
      A copy of any of them on your PATH is still preferred for yt-dlp, so
      one you keep updated wins.</div></div>` : ''}

    <div class="card">
      <header><h3>Download from YouTube</h3>
        <span class="muted">audio only, straight into a library folder and
          into the catalog - no rescan needed.</span></header>
      <div class="in stack">
        <form id="dl-form" class="stack">
          <textarea name="urls" rows="3" required placeholder="https://www.youtube.com/watch?v=...&#10;one URL per line; a playlist or channel URL fetches all of it"
            style="${field};width:100%;font-family:var(--mono);resize:vertical"
            >${h(S.dlUrls)}</textarea>
          ${S.dlUrls.trim() ? `<div class="hstack">
            <span class="faint" style="font-size:10px">${num(S.dlUrls.trim().split(/\s+/).length)}
              URL${S.dlUrls.trim().split(/\s+/).length === 1 ? '' : 's'} kept from last time -
              this box survives a reload.</span>
            <span class="grow" style="flex:1"></span>
            <button class="btn sm" type="button" data-clear-urls="1">Clear the list</button>
          </div>` : ''}
          <div class="hstack">
            <label class="muted">Into</label>
            <select name="root" style="${field};flex:1 1 12em;min-width:0">
              ${roots.map(r => `<option value="${h(r)}" ${r === cfg.root ? 'selected' : ''}>${h(r)}</option>`).join('')
                || '<option value="">no library folder indexed yet</option>'}
            </select>
            <label class="muted">Playlist</label>
            <input name="playlist" list="dl-playlists" placeholder="optional"
              style="${field};width:180px">
            <datalist id="dl-playlists">
              ${S.playlists.map(p => `<option value="${h(p.name)}"></option>`).join('')}
            </datalist>
          </div>
          <div class="hstack">
            <label class="muted"><input type="checkbox" name="single">
              Just this video, not the playlist it sits in</label>
            <label class="muted"><input type="checkbox" name="again">
              Fetch again even if already downloaded</label>
            <span class="grow"></span>
            <button class="btn" type="button" data-probe="1">What is at this URL?</button>
            <button class="btn primary" type="submit" ${d.ytdlp ? '' : 'disabled'}>Download</button>
          </div>
        </form>

        ${S.dlProbe ? `<div class="notice ok">${icon('i-check')}<div>
          <strong>${h(S.dlProbe.title || '')}</strong>
          ${S.dlProbe.uploader ? ' &middot; ' + h(S.dlProbe.uploader) : ''}
          &middot; ${S.dlProbe.is_playlist
            ? num(S.dlProbe.count) + ' items' : dur(S.dlProbe.duration)}
          ${S.dlProbe.is_album ? `<div class="faint" style="margin-top:6px">An
            album, not a playlist: its tracks are filed under the album and no
            playlist is made for it.</div>` : ''}
          ${S.dlProbe.is_playlist && !S.dlProbe.is_album ? `<div class="hstack" style="margin-top:6px">
            <button class="btn sm" data-keep="${h(S.dlProbe.url)}"
              >${icon('i-keep')} Keep this playlist</button>
            <span class="faint">kept playlists sit above the log and fetch
              only what is new.</span></div>` : ''}
        </div></div>` : ''}

        ${renderBatch((job && job.batch) || (res && res.batch),
                      !!(job && job.state === 'running'))}

        ${job ? `<div>
          <div class="hstack"><span class="${job.state === 'running' ? 'spin' : ''}"></span>
            <span class="muted clip">${h(job.detail || job.state)}</span></div>
          ${job.state === 'failed' ? `<div class="notice bad" style="margin-top:8px">
            ${icon('i-warn')}<div>${h(job.error)}</div></div>` : ''}
        </div>` : ''}

        ${res && !failed ? (res.downloaded ? `<div class="notice ok">${icon('i-check')}<div>
            ${num(res.downloaded)} downloaded into
            <span class="mono pick">${h(res.root)}</span> -
            ${num(res.added)} added to the catalog, ${num(res.updated)} updated.
            ${(res.playlists || []).map(pl => `<div>Playlist
              <strong>${h(pl.name)}</strong>: ${num(pl.added)} added${pl.skipped
                ? ', ' + num(pl.skipped) + ' were already in it' : ''} -
              ${num(pl.entries)} entries.</div>`).join('')}
          </div></div>
          <div class="scrollbox"><table class="tbl"><tbody>${res.files.map(f => `<tr>
            <td class="mono clip pick" title="${h(f)}">${h(f.slice(res.root.length + 1))}</td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="notice">${icon('i-warn')}<div>Nothing new - every URL
            was already in the download archive, or nothing could be fetched.
            The log below says which.</div></div>`) : ''}
        ${res && res.errors && res.errors.length ? `<div class="notice bad">
          ${icon('i-warn')}<div class="pick">${res.errors.map(e => h(e)).join('<br>')}</div></div>` : ''}
      </div>
    </div>

    ${renderPaused(d)}
    ${renderQueue()}
    ${renderKept(d)}
    ${renderRecent(d)}
    ${renderSources()}

    <div class="card">
      <header><h3>Log</h3>
        <span class="tag">${num(logLines.length)} lines</span>
        <span class="muted">everything yt-dlp printed, plus what Lemon Zest
          did with it. Selectable - copy it into a bug report.</span>
        <span class="grow"></span>
        <button class="btn sm" data-copylog="1"
          ${logLines.length ? '' : 'disabled'}>Copy</button></header>
      <pre class="console" id="dl-console">${logLines.map(line => {
        const cls = /^\$ /.test(line) ? 'cmd' : /ERROR|error:/.test(line) ? 'err' : '';
        return cls ? `<span class="${cls}">${h(line)}</span>` : h(line);
      }).join('\n') || '<span class="faint">Nothing has run yet.</span>'}</pre>
    </div>

    <div class="card">
      <header><h3>Cookies</h3>${cookieTag}
        <span class="muted">YouTube refuses age-restricted, members-only and
          bot-checked videos to a signed-out client.</span></header>
      <div class="in stack">
        <p class="muted">${h(ck.detail)}</p>
        <form id="dl-cookie-form" class="stack">
          <div class="hstack">
            <label class="muted">Source</label>
            <select name="cookies_mode" style="${field}">
              ${[['auto', 'Automatic - Firefox, then a cookies.txt'],
                 ['firefox', 'Firefox profile'],
                 ['file', 'cookies.txt file'],
                 ['none', 'None']].map(([v, t]) =>
                `<option value="${v}" ${cfg.cookies_mode === v ? 'selected' : ''}>${h(t)}</option>`).join('')}
            </select>
            <label class="muted">Firefox profile</label>
            <select name="firefox_profile" style="${field};flex:1 1 12em;min-width:0">
              <option value="">${ck.profiles.length
                ? 'Most recently used' : 'none found on this machine'}</option>
              ${ck.profiles.map(p => `<option value="${h(p.name)}"
                ${cfg.firefox_profile === p.name ? 'selected' : ''}>${h(p.name)}</option>`).join('')}
            </select>
          </div>
          <div class="hstack">
            <label class="muted">cookies.txt</label>
            <input name="cookies_file" value="${h(cfg.cookies_file)}"
              placeholder="C:/Users/you/cookies.txt - exported by a browser extension"
              style="${field};flex:1;font-family:var(--mono)">
            <button class="btn primary" type="submit">Save</button>
          </div>
        </form>
        <p class="muted">Lemon Zest never copies the cookie database; yt-dlp
          reads it directly when a download runs. Close Firefox first if it
          refuses to read the profile.</p>
      </div>
    </div>

    <div class="card">
      <header><h3>JavaScript runtime</h3>
        ${js.chosen ? `<span class="tag ok">${h(js.chosen.name.toUpperCase())}</span>`
          : '<span class="tag warn">MISSING</span>'}
        <span class="muted">YouTube signs its media URLs with a challenge that
          has to be run; without a runtime, every video fails.</span></header>
      <div class="in stack">
        <p class="muted">${h(js.detail)}</p>
        ${js.found.length ? `<table class="tbl"><tbody>${js.found.map(r => `<tr>
          <td>${h(r.name)}${js.chosen && js.chosen.name === r.name
            ? ' <span class="tag ok">in use</span>' : ''}${
            r.bundled ? ' <span class="tag">bundled</span>' : ''}</td>
          <td class="mono clip pick" title="${h(r.path)}">${h(r.path)}</td>
        </tr>`).join('')}</tbody></table>`
        : `<div class="notice bad">${icon('i-warn')}<div>None installed.
            Install <span class="mono">Deno</span> (deno.com) or
            <span class="mono">Node</span>, then reload this page.</div></div>`}
        <form id="dl-runtime-form" class="hstack">
          <label class="muted">Enable</label>
          <input name="js_runtimes" value="${h(cfg.js_runtimes)}"
            placeholder="node,bun,quickjs"
            style="${field};flex:1 1 12em;min-width:0;font-family:var(--mono)">
          <button class="btn" type="submit">Save</button>
        </form>
        <span class="faint">yt-dlp enables deno on its own; these are the
          others Lemon Zest enables alongside it. Deno wins when installed.</span>
      </div>
    </div>

    <div class="card">
      <header><h3>Where files land</h3>
        <span class="muted">yt-dlp output template, relative to the folder above.</span></header>
      <div class="in stack">
        <form id="dl-output-form" class="hstack">
          <input name="output" value="${h(cfg.output)}"
            style="${field};flex:1;font-family:var(--mono)">
          <label class="muted" title="Videos fetched at once">At a time</label>
          <select name="workers" style="${field}">
            ${['1', '2', '3', '4'].map(n =>
              `<option ${String(cfg.workers) === n ? 'selected' : ''}>${n}</option>`).join('')}
          </select>
          <select name="audio_format" style="${field}">
            ${['m4a', 'mp3', 'opus', 'flac'].map(f =>
              `<option ${cfg.audio_format === f ? 'selected' : ''}>${f}</option>`).join('')}
          </select>
          <button class="btn" type="submit">Save</button>
        </form>
        <span class="faint">yt-dlp ${d.ytdlp ? h(d.ytdlp) : 'not installed'}${
          bn.frozen ? ' · ffmpeg ' + (bundledTool('ffmpeg')
            ? 'bundled with this build' : 'from PATH') : ''}</span>
      </div>
    </div>
  </div>`;
}

function renderProblems() {
  const p = S.problems;
  if (!p) return '<div class="empty"><span class="spin"></span></div>';
  const clean = !p.empty_total && !p.untagged_total && !p.unmatched_total
    && !p.awaiting;
  if (clean) {
    return `<div class="pad"><div class="notice ok">${icon('i-check')}
      <div>Nothing needs attention. Every file has content and tags, and every
      playlist entry resolves.</div></div></div>`;
  }

  const fileCard = (title, note, rows, total) => rows.length ? `
    <div class="card">
      <header><h3>${h(title)}</h3>
        <span class="tag ${total ? 'warn' : ''}">${num(total)}</span>
        <span class="muted">${h(note)}</span></header>
      <table class="tbl"><tbody>
        ${rows.slice(0, 200).map(r => `<tr><td class="mono clip"
          title="${h(r.path)}">${h(r.rel_path)}</td></tr>`).join('')}
      </tbody></table>
      ${total > rows.length || rows.length > 200
        ? `<div class="footnote">SHOWING ${Math.min(rows.length, 200)} OF ${num(total)}</div>` : ''}
    </div>` : '';

  return `<div class="pad stack">
    ${p.awaiting ? `<div class="card">
      <header><h3>Waiting on you</h3>
        <span class="tag warn">${num(p.awaiting)}</span>
        <span class="muted">a match was found but is not certain enough to
          write on its own.</span></header>
      <div class="in hstack">
        <span class="muted">${num(p.awaiting)} track${p.awaiting === 1 ? '' : 's'}
          ${p.awaiting === 1 ? 'has' : 'have'} a proposed match to accept or
          reject.</span>
        <span class="grow" style="flex:1"></span>
        <button class="btn" data-act="review">Review them</button>
      </div>
    </div>` : ''}
    ${fileCard('Empty files', 'zero bytes on disk - failed downloads. Lemon Zest skips these when syncing rather than putting dead entries on the card.', p.empty, p.empty_total)}
    ${fileCard('Untagged files', 'no title tag, so they sort last and their destination path falls back to the filename.', p.untagged, p.untagged_total)}
    ${p.unmatched.length ? `<div class="card">
      <header><h3>Unmatched playlist entries</h3>
        <span class="tag warn">${num(p.unmatched_total)}</span>
        <span class="muted">the path in the playlist resolves to no file in the
          catalog. Kept rather than dropped, but skipped when writing to a device.</span></header>
      <table class="tbl">
        <thead><tr><th>Playlist</th><th class="num">#</th><th>Track</th>
          <th>Path in the playlist</th></tr></thead>
        <tbody>${p.unmatched.map(u => `<tr data-playlist="${u.pid}">
          <td>${h(u.name)}</td><td class="num">${u.pos + 1}</td>
          <td class="clip">${h(u.title_hint || '')}</td>
          <td class="mono clip" title="${h(u.raw_path)}">${h(u.raw_path)}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>` : ''}
  </div>`;
}

/* Versions of one song, found and settled.

   Each card is one suggestion: the files side by side, a column each, and
   a row per field. The first row picks the master - the file every
   playlist and device will use from now on. Every other row picks where
   the master's value comes from, so the cover can come from one version
   and the title from another. Nothing is deleted: the others stay in the
   library, set aside behind the master. */
const DUPE_FIELDS = [['cover', 'cover'], ['title', 'title'], ['artist', 'artist'],
  ['album', 'album'], ['album_artist', 'album artist'], ['year', 'year']];
const DUPE_REASON = {
  source: 'same video', isrc: 'same ISRC', mbid: 'same recording',
  title: 'same title and length',
  'title, other artist': 'same title and length, other artist',
};

function renderDupes() {
  const g = S.dupes;
  if (!g) return '<div class="empty"><span class="spin"></span></div>';
  if (!g.length) {
    return `<div class="pad"><div class="notice ok">${icon('i-check')}
      <div>No duplicates found. Files are compared by source video, ISRC,
      matched recording, and title with length.</div></div></div>`;
  }
  const cell = (t, f) => (f === 'cover'
    ? `<img src="/api/art/${encodeURIComponent(t.content_key)}" alt=""
        style="width:40px;height:40px;object-fit:cover;border:1px solid var(--line-2)"
        onerror="this.replaceWith(document.createTextNode('none'))">`
    : `<span class="clip">${h(t[f])}</span>`);
  return `<div class="pad stack">
    <div class="faint" style="font-size:11px">${num(g.length)} possible
      duplicate${g.length === 1 ? '' : 's'}. Choose the master and, row by
      row, which version each value comes from. The other versions stay in
      the library, hidden behind the master and kept for the record.</div>
    ${g.map((grp, gi) => `<div class="card" data-dupe-card="${gi}">
      <header><h3>${h(grp.tracks[0].title || 'untitled')}</h3>
        <span class="tag">${grp.score.toFixed(2)}</span>
        <span class="muted">${h(grp.reasons.map(r => DUPE_REASON[r] || r).join(' · '))}</span></header>
      <table class="tbl"><thead><tr><th style="width:90px"></th>
        ${grp.tracks.map(t => `<th class="clip mono" title="${h(t.rel_path)}"
          style="font-size:10px">${h(t.rel_path)}</th>`).join('')}</tr></thead>
      <tbody>
        <tr><td class="faint">master</td>${grp.tracks.map(t => `<td><label>
          <input type="radio" name="m-${gi}" value="${h(t.content_key)}"
            ${t.content_key === grp.master ? 'checked' : ''}> use this file</label></td>`).join('')}</tr>
        ${DUPE_FIELDS.map(([f, label]) => `<tr><td class="faint">${h(label)}</td>
          ${grp.tracks.map(t => `<td><label style="display:flex;gap:4px;align-items:center">
            <input type="radio" name="f-${gi}-${f}" value="${h(t.content_key)}"
              ${t.content_key === grp.master ? 'checked' : ''}>
            ${cell(t, f)}</label></td>`).join('')}</tr>`).join('')}
        <tr><td class="faint">length</td>${grp.tracks.map(t =>
          `<td class="mono">${h(dur(t.duration))}</td>`).join('')}</tr>
      </tbody></table>
      <div class="in hstack">
        <span class="grow" style="flex:1"></span>
        <button class="btn" data-dupe-dismiss="${gi}">Not the same song</button>
        <button class="btn primary" data-dupe-merge="${gi}">Merge</button>
      </div>
    </div>`).join('')}
  </div>`;
}

async function loadDupes() {
  S.dupes = (await api('/dupes')).groups;
}

async function mergeDupe(gi) {
  const grp = S.dupes[gi];
  const card = document.querySelector(`[data-dupe-card="${gi}"]`);
  const chosen = (name) => {
    const el = card.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : null;
  };
  const master = chosen('m-' + gi) || grp.master;
  const keys = grp.tracks.map(t => t.content_key).filter(k => k !== master);
  await api('/dupes/merge', { method: 'POST',
    body: JSON.stringify({ master, content_keys: keys }) });
  // Values from another version: a text field is typed onto the master, a
  // cover is written into its file.
  for (const [f] of DUPE_FIELDS) {
    const from = chosen(`f-${gi}-${f}`);
    if (from && from !== master) {
      await api('/dupes/pick', { method: 'POST',
        body: JSON.stringify({ field: f, from_key: from }) });
    }
  }
  await Promise.all([loadDupes(), loadCore()]);
}

function renderVersions(d) {
  const v = d.versions;
  if (!v || !v.tracks) return '';
  const picks = v.picks || {};
  const took = (key) => Object.keys(picks).filter(f => picks[f] === key);
  return `<div class="pl-seen">
    <h4>Versions <span class="faint mono">${v.tracks.length}</span></h4>
    <ul class="pl-list">${v.tracks.map(t => `<li style="flex-wrap:wrap">
      <span class="clip" title="${h(t.rel_path)}">${t.is_master ? '<strong>master</strong> · ' : ''}${h(t.album || t.rel_path)}${t.year ? ' · ' + h(t.year) : ''}</span>
      ${took(t.content_key).length ? `<span class="faint" style="font-size:10px">gives the master its ${h(took(t.content_key).join(', '))}</span>` : ''}
      ${t.source_url ? `<a class="mono faint clip" style="font-size:10px" href="${h(t.source_url)}"
        target="_blank" rel="noreferrer">${h(t.source_url)}</a>` : ''}
      <span class="grow" style="flex:1"></span>
      ${t.is_master ? '' : `<button class="btn sm" data-dupe-master="${h(t.content_key)}">Make master</button>`}
      <button class="btn sm" data-dupe-unmerge="${h(t.content_key)}"
        title="Treat this file as a song of its own again">Unmerge</button>
    </li>`).join('')}</ul>
  </div>`;
}

// ----------------------------------------------------------------- modal

function showModal(title, body, footer) {
  $('#modal-root').innerHTML = `<div class="scrim" data-scrim="1"><div class="modal">
    <header><h3>${h(title)}</h3></header>
    <div class="in">${body}</div>
    <footer>${footer || '<button class="btn" data-close="1">Close</button>'}</footer>
  </div></div>`;
}
function closeModal() { $('#modal-root').innerHTML = ''; }

function pickSetModal(device) {
  const chosen = new Set(device.rules.filter(r => r.kind === 'playlist')
    .map(r => r.ref));
  const body = `<p class="muted" style="margin-bottom:10px">
      Tick the playlists this device should carry. Tracks follow; a track in
      two playlists is copied once.</p>
    <div style="max-height:46vh;overflow:auto;border:1px solid var(--line-2);border-radius:4px">
    <table class="tbl"><tbody>
    ${S.playlists.map(p => `<tr>
      <td class="tick"><input type="checkbox" data-pl="${h(p.name)}"
        ${chosen.has(p.name) ? 'checked' : ''}></td>
      <td>${h(p.name)}</td>
      <td class="num">${num(p.n)}</td>
      <td>${p.unmatched ? `<span class="tag warn">${num(p.unmatched)} unmatched</span>` : ''}</td>
    </tr>`).join('')}
    </tbody></table></div>`;
  showModal('What goes on ' + device.name, body,
    `<button class="btn" data-close="1">Cancel</button>
     <button class="btn primary" data-save-set="${device.id}">Save set</button>`);
}

/* Removing a library folder forgets rows, never files. Said in the dialog
   rather than in a tooltip, because "remove" next to a path is exactly the
   word people expect to mean "delete my music". */
function removeRootModal(root) {
  const info = ((S.stats && S.stats.root_detail) || [])
    .find(r => r.root === root) || { tracks: 0 };
  showModal('Remove this library folder?', `
    <p class="mono" style="margin-bottom:10px">${h(root)}</p>
    <div class="notice ok" style="margin-bottom:10px">${icon('i-check')}
      <div><b>Your audio files are not touched.</b> This removes
      ${num(info.tracks)} row${info.tracks === 1 ? '' : 's'} from the catalog
      and stops the folder being scanned. Scanning it again brings everything
      back.</div></div>
    <p class="muted">Playlist entries pointing at these tracks go back to
      unmatched, and any device set built from them will copy less on its
      next sync. To keep the catalog and only take the folder out of sight,
      hide it instead.</p>`,
    `<button class="btn" data-close="1">Cancel</button>
     <button class="btn" data-root-hide="${h(root)}" data-hidden="1"
       >Hide instead</button>
     <button class="btn danger" data-root-forget="${h(root)}"
       >Remove from library</button>`);
}

// ------------------------------------------------------------- utilities

async function loadResetInfo() {
  S.resetInfo = { loading: true };
  try {
    S.resetInfo = await api('/reset');
  } catch (e) {
    S.resetInfo = { error: e.message };
  }
  if (S.view === 'utilities') render();
}

function renderUtilities() {
  const r = S.resetInfo;
  const ready = r && !r.loading && !r.error;
  const last = S.resetResult;
  return `<div class="pad stack">
    ${renderMaintenance()}
    ${renderPortable()}
    <div class="card">
      <header><h3>Start the library over</h3></header>
      <div class="in stack">
        <p class="muted">Forgets every track, playlist and identification in
          this catalog, and removes the download history
          (<span class="mono">.lemon-zest-downloads.txt</span>) so the same
          videos can be downloaded again.</p>
        <p class="muted"><b>Kept:</b> library folders, paired devices,
          download settings, and the URLs on the Add music page - so
          fetching everything again is a click away.</p>
        ${!ready ? `<p class="muted">${h((r && r.error) || 'Counting...')}</p>`
          : `<div class="hstack">
              <span class="tag">${num(r.tracks)} tracks</span>
              <span class="tag">${num(r.playlists)} playlists</span>
              <span class="tag">${num(r.archives.length)} download archive${
                r.archives.length === 1 ? '' : 's'}</span>
              <span class="tag">${num(r.urls)} URLs kept</span></div>`}
        <label class="hstack"><input type="checkbox" id="reset-files" ${
          S.resetFiles ? 'checked' : ''}>
          <span>Also delete the audio files and playlist files from disk</span></label>
        <div><button class="btn danger" data-reset-open="1"
          ${ready ? '' : 'disabled'}>Reset library...</button></div>
        ${last ? `<div class="notice ${last.error_count ? 'warn' : 'ok'}">${
          icon(last.error_count ? 'i-warn' : 'i-check')}<div>
          <b>Library reset.</b> ${num(last.tracks_forgotten)} tracks and
          ${num(last.playlists_forgotten)} playlists forgotten,
          ${num(last.files_deleted)} files and ${num(last.archives_deleted)}
          download archive${last.archives_deleted === 1 ? '' : 's'} deleted.
          ${last.error_count ? `${num(last.error_count)} could not be removed:
            <div class="mono">${last.errors.map(h).join('<br>')}</div>` : ''}
          </div></div>` : ''}
      </div>
    </div>
  </div>`;
}

/* What the library is missing, filled in on request.

   Each of these asks somebody else's service about every file that lacks
   the thing, so it is a job with a bar rather than something done on every
   visit. New downloads get them on arrival; this is for what came before. */
const MAINTENANCE = [
  ['genres', 'Genres', 'Identified tracks with no genre get the one '
    + 'MusicBrainz has for their release, or else for their artist.'],
  ['lyrics', 'Lyrics', 'Tracks with no lyrics get them from LRCLIB - '
    + 'time-synced where it has them - written into the file.'],
];

function renderMaintenance() {
  const job = S.job && S.job.kind === 'maintenance' ? S.job : null;
  const running = !!(S.job && S.job.state === 'running');
  return `<div class="card">
    <header><h3>Fill in what is missing</h3>
      <span class="muted">new downloads get these on arrival; this is for
        the files that came before.</span></header>
    <div class="in stack">
      ${MAINTENANCE.map(([task, label, what]) => `<div class="hstack">
        <button class="btn" data-maint="${task}" ${running ? 'disabled' : ''}
          style="min-width:90px">${h(label)}</button>
        <span class="muted">${h(what)}</span></div>`).join('')}
      ${job ? `<div class="hstack"><span class="${
          job.state === 'running' ? 'spin' : ''}"></span>
        <span class="muted">${h(job.label)}: ${h(job.error || job.detail
          || job.state)}</span></div>` : ''}
    </div>
  </div>`;
}

/* Taking the library to another computer.

   The folder is the thing to copy. Each library folder carries a copy of
   the catalog, refreshed after every scan and download, so the music, its
   playlists, what was part-downloaded and the history of all of it travel
   together. Credentials stay behind on purpose. */
function renderPortable() {
  const p = S.portable;
  return `<div class="card">
    <header><h3>Move to another computer</h3></header>
    <div class="in stack">
      <p class="muted">Copy each library folder whole - the hidden
        <span class="mono">.lemon-zest</span> folder inside it is the catalog,
        with the download history - and the <span class="mono">Playlists</span>
        folder beside it. Cookies, the Firefox profile and the AcoustID key
        stay on this computer; set them up again on the other one.</p>
      ${p ? p.roots.map(r => `<div class="hstack">
        <span class="mono clip" style="flex:1" title="${h(r.root)}">${h(r.root)}</span>
        <span class="faint mono" style="font-size:10px">${r.packed_at
          ? 'catalog copy ' + h(ago(r.packed_at)) : 'no catalog copy yet'}</span>
      </div>`).join('') : '<span class="faint">Checking...</span>'}
      <div><button class="btn" data-pack="1">Refresh the copies now</button></div>
      <p class="muted"><b>Arriving here from another computer?</b> Point at the
        library folder where you copied it. Its catalog replaces this one and
        is moved to where the folder is now.</p>
      <div class="hstack">
        <input id="unpack-folder" placeholder="C:/Users/you/Music/library"
          style="flex:1;border:1px solid var(--line-2);border-radius:3px;padding:4px 7px">
        <label class="muted"><input type="checkbox" id="unpack-replace">
          replace the ${num(p ? p.tracks : 0)} tracks here</label>
        <button class="btn primary" data-unpack="1">Open it</button>
      </div>
      ${S.unpacked ? `<div class="notice ok">${icon('i-check')}<div>
        ${num(S.unpacked.tracks)} tracks, moved from
        <span class="mono">${h(S.unpacked.from || '?')}</span> to
        <span class="mono">${h(S.unpacked.to)}</span>.
        ${S.unpacked.missing_roots.length ? `Not found here:
          ${S.unpacked.missing_roots.map(h).join(', ')} - point them at their
          new place under <b>Add music</b>.` : ''}</div></div>` : ''}
    </div>
  </div>`;
}

function resetModal() {
  const r = S.resetInfo || {};
  const box = $('#reset-files');
  S.resetFiles = !!(box && box.checked);
  const files = S.resetFiles;
  showModal('Reset the whole library?', `
    <div class="notice warn" style="margin-bottom:10px">${icon('i-warn')}
      <div><b>This cannot be undone.</b> ${num(r.tracks || 0)} tracks and
      ${num(r.playlists || 0)} playlists will be forgotten and the download
      history removed.${files
        ? ` <b>${num(r.tracks || 0)} audio files (${h(bytes(r.bytes || 0))})
          will be permanently deleted from disk</b>, along with the playlist
          files beside them.`
        : ' Your audio files stay on disk; a rescan brings them back.'}
      </div></div>
    ${(r.roots || []).length ? `<p class="muted">Library folders:</p>
      <p class="mono" style="margin-bottom:10px">${
        r.roots.map(h).join('<br>')}</p>` : ''}
    <p>Type <b class="mono">RESET</b> to confirm.</p>
    <input id="reset-word" autocomplete="off" spellcheck="false"
      style="width:100%;margin-top:6px">`,
    `<button class="btn" data-close="1">Cancel</button>
     <button class="btn danger" id="reset-go" data-reset-go="1" disabled
       >${files ? 'Delete files and reset' : 'Reset library'}</button>`);
  const input = $('#reset-word');
  if (input) input.focus();
}

function runReset() {
  const word = ($('#reset-word') || {}).value || '';
  if (word.trim() !== 'RESET') return;
  const files = S.resetFiles;
  closeModal();
  return guard(async () => {
    S.resetResult = await api('/reset', {
      method: 'POST',
      body: JSON.stringify({ confirm: 'RESET', delete_files: files }),
    });
    S.resetInfo = null;
    S.sel.clear(); S.anchor = null;
    S.playlistDetail = null;
    await loadCore();
  });
}

// ---------------------------------------------------------------- render

const TITLES = {
  library: () => `MUSIC \u2014 ${num(S.tracksTotal)} TRACKS`,
  inbox: () => `INBOX \u2014 ${num(S.tracksTotal)} NEW`,
  unlisted: () => `NOT IN A PLAYLIST \u2014 ${num(S.tracksTotal)} TRACKS`,
  device: () => {
    const d = S.devices.find(x => x.id === S.deviceId);
    return d ? (d.name + ' \u2014 SYNC PLAN').toUpperCase() : 'DEVICE';
  },
  playlist: () => S.playlistDetail
    ? ('PLAYLIST \u2014 ' + S.playlistDetail.playlist.name).toUpperCase() : 'PLAYLIST',
  addDevice: () => 'ADD DEVICE',
  download: () => 'ADD MUSIC',
  syncList: () => `SYNC LIST — ${num((S.syncList || {}).tracks || 0)} TRACKS`,
  normalize: () => 'NORMALIZE VOLUME',
  utilities: () => 'UTILITIES',
  problems: () => 'NEEDS ATTENTION',
  dupes: () => 'DUPLICATES' + (S.dupes ? ' \u2014 ' + num(S.dupes.length) + ' TO SETTLE' : ''),
};

function renderStatus() {
  const job = S.job;
  const meter = $('#meter');
  if (job && job.state === 'running') {
    meter.classList.remove('idle');
    const VERB = {
      sync: 'Syncing \u2192 ', download: 'Downloading ',
      enrich: 'Identifying ', 'write-tags': 'Writing tags to ',
      scan: 'Scanning ', maintenance: 'Filling in ',
    };
    $('#status-label').textContent =
      (VERB[job.kind] || 'Working on ') + job.label;
    // A download says where it is in the batch, everywhere in the app: the
    // status strip is the only part of the interface visible from the
    // Library page, and "12 of 47" is the whole question.
    const b = job.batch;
    // Whatever is actually moving. Once yt-dlp has written the last file
    // there is no `current` any more, but the run is not over - the last
    // track is still being identified, and the strip says so rather than
    // freezing on a number.
    const busy = b && (b.items || []).find(
      i => i.state === 'indexing' || i.state === 'identifying');
    $('#status-meta').textContent = b && b.total
      ? `${num(b.done)} of ${num(b.total)}`
        + (b.current ? ' \u00b7 ' + b.current.title
           : busy ? ' \u00b7 ' + busy.detail : '')
      : (job.detail || '');
    $('#status-bar').style.width =
      (job.total ? (100 * job.done / job.total) : 0) + '%';
  } else {
    const st = S.stats || {};
    const connected = S.devices.filter(d => d.mounted_at).length;
    const queued = queueMeter();
    if (queued) {
      // Quieter than a job - no bar - because nobody is waiting on it, but
      // said out loud, because it is work the program is doing.
      meter.classList.remove('idle');
      $('#status-label').textContent = queued.label;
      $('#status-meta').textContent = queued.meta;
      $('#status-bar').style.width = '0%';
      return;
    }
    meter.classList.add('idle');
    $('#status-label').textContent = job && job.state === 'failed'
      ? 'Last job failed' : 'Ready';
    $('#status-meta').textContent =
      `${num(st.tracks)} tracks \u00b7 ${num(st.playlists)} playlists \u00b7 ` +
      `${S.devices.length} devices, ${connected} connected`;
    $('#status-bar').style.width = '0%';
  }
}

/* A listen, which is the only way to tell some things.

   A download that produced four seconds of silence, or a file whose audio
   never arrived, is a perfectly healthy row in every column of the table:
   right title, right length, right bitrate. The only check is to hear it.
   Deliberately small - one track, the browser's own controls, no queue.

   The audio element is built once and kept out of the re-rendered pane: a
   render while a track is playing must not restart it. */
const PLAYER = { audio: null };

function playerNode() {
  const box = $('#player');
  if (!PLAYER.audio) {
    box.innerHTML = `<div class="what clip">
        <div class="t clip" id="play-title"></div>
        <div class="a clip" id="play-artist"></div>
      </div>
      <audio id="play-audio" controls preload="metadata"></audio>
      <span class="bad" id="play-error"></span>
      <span class="grow" style="flex:1"></span>
      <button class="btn sm" data-play-close="1">Close</button>`;
    PLAYER.audio = $('#play-audio');
    PLAYER.audio.addEventListener('error', () => {
      // Worth saying which of the two it is: a browser that cannot decode
      // the container is not a broken file, and telling somebody their
      // download is corrupt when it plays fine on the player is worse than
      // saying nothing.
      $('#play-error').textContent =
        'this browser could not play that file - try it on the device';
    });
  }
  return box;
}

function playTrack(id) {
  const t = (S.tracks || []).find(x => x.id === id)
    || (S.detail && S.detail.track && S.detail.track.id === id
        ? S.detail.track : null);
  S.play = { id, title: (t && (t.title || (t.rel_path || '').split('/').pop()))
                        || 'track ' + id,
             artist: (t && [t.artist, t.album].filter(Boolean).join(' · ')) || '' };
  const box = playerNode();
  box.hidden = false;
  document.body.classList.add('playing');
  $('#play-title').textContent = S.play.title;
  $('#play-artist').textContent = S.play.artist;
  $('#play-error').textContent = '';
  PLAYER.audio.src = '/api/audio/' + id;
  PLAYER.audio.play().catch(() => { /* the controls are right there */ });
}

function stopPlaying() {
  if (PLAYER.audio) { PLAYER.audio.pause(); PLAYER.audio.removeAttribute('src'); }
  S.play = null;
  const box = $('#player');
  if (box) box.hidden = true;
  document.body.classList.remove('playing');
}

function render() {
  saveUiState();
  if (isTrackView()) syncInspector();
  renderSidebar();
  $('#titlebar').innerHTML = `<span>${h((TITLES[S.view] || (() => S.view))())}</span>
    <span class="grow"></span>
    ${S.error ? `<span style="color:var(--bad);text-transform:none;font-family:var(--sans);font-size:11px">${h(S.error)}</span>` : ''}`;
  const body = {
    library: renderLibrary, inbox: renderInbox, unlisted: renderUnlisted,
    device: renderDevice, playlist: renderPlaylist,
    addDevice: renderAddDevice, normalize: renderNormalize,
    utilities: renderUtilities,
    syncList: renderSyncList, problems: renderProblems, dupes: renderDupes,
    download: renderDownload,
  }[S.view];
  $('#pane').innerHTML = body ? body() : '';
  renderStatus();
  if (S.view === 'addDevice') loadVolumes();
  if (S.view === 'utilities' && !S.resetInfo) loadResetInfo();
  if (S.view === 'utilities' && !S.portable && !S.portableLoading) {
    S.portableLoading = true;
    api('/portable').then((p) => { S.portable = p; render(); },
      () => {}).finally(() => { S.portableLoading = false; });
  }
  // Follow a running download; leave a finished one where the reader put it,
  // so scrolling back through a failure is not undone by the next poll.
  if (S.view === 'download' && S.job && S.job.kind === 'download'
      && S.job.state === 'running') {
    const el = $('#dl-console');
    if (el) el.scrollTop = el.scrollHeight;
  }
}

function selectNode(el) {
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

async function loadVolumes() {
  const el = $('#volumes');
  if (!el) return;
  try {
    const vols = await api('/volumes?all=1');
    el.innerHTML = `<table class="tbl">
      <thead><tr><th>Mount</th><th>Label</th><th>Format</th>
        <th class="num">Capacity</th><th class="num">Free</th><th></th></tr></thead>
      <tbody>${vols.map(v => `<tr>
        <td class="mono">${h(v.mountpoint)}</td>
        <td>${h(v.label || '')}</td>
        <td class="mono">${h(v.fstype)}</td>
        <td class="num">${bytes(v.total)}</td>
        <td class="num">${bytes(v.free)}</td>
        <td class="right">${v.device_uid
          ? '<span class="tag ok">paired</span>'
          : `<button class="btn sm" data-usevol="${h(v.mountpoint)}"
               data-label="${h(v.label || '')}">Use this</button>`}</td>
      </tr>`).join('')}</tbody></table>`;
  } catch (e) {
    el.innerHTML = `<div class="notice bad">${h(e.message)}</div>`;
  }
}

// ------------------------------------------------------------- selection

// Selection is deliberately not a checkbox-only affair. A library is browsed
// the way a file manager is - click, shift-click a run, ctrl-click the odd
// extra - and an enrichment pass over "this album except the two live
// takes" is the ordinary case, not an advanced one.

function selectRow(index, ev) {
  const t = S.tracks[index];
  if (!t) return;
  const key = t.content_key;
  if (ev && ev.shiftKey && S.anchor !== null) {
    // Text is selectable everywhere now, so a shift-click on the table
    // would leave a stray highlight dragged across the rows behind the
    // real selection. Drop it: the rows are the selection here.
    const native = window.getSelection();
    if (native) native.removeAllRanges();
    const [a, b] = S.anchor <= index ? [S.anchor, index] : [index, S.anchor];
    // A shift-click extends rather than replaces, so two ranges can be
    // collected without holding a third modifier down.
    if (!ev.ctrlKey && !ev.metaKey) S.sel.clear();
    for (let i = a; i <= b; i++) {
      if (S.tracks[i]) S.sel.add(S.tracks[i].content_key);
    }
  } else if (ev && (ev.ctrlKey || ev.metaKey)) {
    if (S.sel.has(key)) S.sel.delete(key); else S.sel.add(key);
    S.anchor = index;
  } else {
    S.sel.clear();
    S.sel.add(key);
    S.anchor = index;
  }
  render();
}

function selectPage(on) {
  for (const t of S.tracks) {
    if (on) S.sel.add(t.content_key); else S.sel.delete(t.content_key);
  }
  render();
}

function selectAllMatching() {
  return guard(async () => {
    const p = libraryParams();
    const out = await api('/library/keys?' + p);
    S.sel = new Set(out.keys);
    S.anchor = null;
    if (out.capped) {
      S.error = `Selected the first ${num(out.keys.length)} of `
        + `${num(out.total)}; narrow the filter to reach the rest.`;
    }
  });
}

function selectedKeys() { return Array.from(S.sel); }

function selAction(act) {
  if (act === 'clear') { S.sel.clear(); S.anchor = null; return render(); }
  if (act === 'all-matching') return selectAllMatching();
  const keys = selectedKeys();
  if (!keys.length) return;
  if (act === 'to-sync-list') {
    return guard(async () => {
      S.syncList = await api('/sync-list/keys', {
        method: 'POST', body: JSON.stringify({ content_keys: keys }),
      });
      S.outcome = { ok: true, text: `${num(S.syncList.added)} added to the `
        + `sync list, which now holds ${num(S.syncList.tracks)} tracks `
        + `(${bytes(S.syncList.bytes)}).` };
      await loadCore();
    });
  }
  if (act === 'edit') return editModal(keys);
  if (act === 'write') return writeTagsModal(keys);
  if (act === 'enrich') return enrichModal(keys);
  return decide(act, keys);
}

// Accept, reject, skip and mark-raw, over an explicit list of keys. Taken as
// an argument rather than read from the selection, so the dialog's own
// buttons act on the track the dialog is showing whatever else has happened
// to the selection since it opened.
function decide(act, keys) {
  if (!keys || !keys.length) return;
  if (act === 'accept' || act === 'reject') {
    closeModal();
    return guard(async () => {
      await api('/enrich/' + act, {
        method: 'POST', body: JSON.stringify({ content_keys: keys }),
      });
      // A verdict finishes with these tracks; leaving them selected invites
      // pressing it twice.
      S.sel.clear(); S.anchor = null;
      await loadCore();
      await loadLibrary();
    });
  }
  if (act === 'skip' || act === 'raw' || act === 'enriched') {
    closeModal();
    // Filing, not a verdict: the rows stay picked, so changing your mind is
    // one more keystroke rather than another hunt through the table.
    return guard(async () => {
      await api('/enrich/state', {
        method: 'POST',
        body: JSON.stringify({ content_keys: keys,
                               state: act === 'skip' ? 'skipped' : act }),
      });
      await loadCore();
      await loadLibrary();
    });
  }
}

// ------------------------------------------------------- enrich a picking

function enrichModal(keys) {
  if (keys.length === 1) {
    // The terms are worth showing even when nothing is wrong with them:
    // "no result" is unreadable until you can see what was asked.
    return guard(async () => {
      S.detail = await api('/enrich/track/' + encodeURIComponent(keys[0]));
      showEnrichModal(keys);
    });
  }
  S.detail = null;
  showEnrichModal(keys);
}

function showEnrichModal(keys) {
  S.dialogKeys = keys.slice();
  const one = keys.length === 1 ? (S.detail || null) : null;
  const search = (one && one.search) || {};
  const field = (name, label, value, ph) => `<label class="field"
      style="flex:1 1 10em;min-width:0;margin-bottom:0"><span>${h(label)}</span>
    <input name="${name}" value="${h(value || '')}" placeholder="${h(ph || '')}"
      autocomplete="off"></label>`;
  const terms = one ? `
    <div class="card" style="margin-bottom:10px">
      <header><h3>What to search for</h3>
        <span class="muted">the terms this lookup will send</span></header>
      <div class="in">
        <form id="enrich-query" class="hstack" style="align-items:flex-end;gap:8px">
          ${field('artist', 'Artist', search.artist, 'blank searches on the title alone')}
          ${field('title', 'Title', search.title)}
          ${field('album', 'Album', search.album, 'used to prefer one release')}
        </form>
        ${one.origin && one.origin.initial_tags ? `<div class="hstack" style="margin-top:6px">
          <button class="btn sm" type="button" data-use-origin="1">Use what it
            arrived as</button>
          <span class="faint" style="font-size:10px">${h([
            one.origin.initial_tags.artist, one.origin.initial_tags.title]
            .filter(Boolean).join(' - '))}</span></div>` : ''}
        <p class="muted" style="margin-top:8px;font-size:11px">
          These start as what an automatic lookup would use - the tags, or
          the two halves of a video title when the artist tag is a channel
          name. Trim them when the tag is the problem: <em>Song (Single
          Version)</em>, <em>Song - Official Video</em> and
          <em>Song feat. Someone</em> are the three that find nothing, and
          <em>Song</em> finds it. Edited terms are searched exactly as typed,
          once - the automatic rewrites are not tried on top of them.</p>
      </div>
    </div>` : '';
  const body = terms + `<p class="muted" style="margin-bottom:10px">
      Look up ${num(keys.length)} selected track${keys.length === 1 ? '' : 's'}
      against MusicBrainz again. New files are already done automatically as
      they arrive, so this is for asking a second time - after correcting the
      tags a search runs on, or once fingerprinting is set up.</p>
    <p class="muted" style="margin-bottom:10px">Matches confident enough to
      trust land in the catalog; the doubtful ones become
      <em>awaiting review</em>. Anything you have marked
      <strong>skipped</strong> is left out, even when it is selected, and
      nothing is written to your audio files unless you tick the box.</p>
    <label class="hstack" style="gap:6px;margin-bottom:6px">
      <input type="checkbox" id="en-fp" ${fingerprintReady() ? '' : 'disabled'}>
      <span>Also identify by sound (AcoustID fingerprint) when the tags are no
        help.${fingerprintReady() ? ''
          : ` <span class="faint">Not set up: needs <code>fpcalc</code> on PATH
              and an AcoustID key. Without it, a download whose artist is a
              channel name will usually come back empty.</span>`}</span></label>
    <label class="hstack" style="gap:6px">
      <input type="checkbox" id="en-write">
      <span>Write accepted values into the audio files as well
        (<strong>rewrites files on disk</strong>).</span></label>`;
  showModal('Enrich ' + num(keys.length) + ' track'
    + (keys.length === 1 ? '' : 's'), body,
    `<button class="btn" data-close="1">Cancel</button>
     <button class="btn primary" data-run-enrich="1">Look them up</button>`);
}

function startEnrich(keys) {
  const fp = $('#en-fp'), wt = $('#en-write');
  const body = { content_keys: keys, fingerprint: !!(fp && fp.checked),
                 write_tags: !!(wt && wt.checked) };
  // Only sent when a person actually changed something: an unedited box is
  // the automatic search, and passing it back as a query would silently
  // disable the rewrites that make the automatic search work.
  const qf = $('#enrich-query');
  if (qf && S.detail && S.detail.search) {
    const typed = { artist: qf.artist.value.trim(),
                    title: qf.title.value.trim(),
                    album: qf.album.value.trim() };
    const was = S.detail.search;
    const same = ['artist', 'title', 'album']
      .every(k => (typed[k] || '') === ((was[k] || '')));
    if (!same && typed.title) body.query = typed;
  }
  closeModal();
  return guard(async () => {
    const res = await api('/enrich/run', {
      method: 'POST', body: JSON.stringify(body),
    });
    S.outcome = null;
    S.job = { id: res.job, kind: 'enrich', state: 'running', done: 0,
              total: keys.length, label: num(keys.length) + ' tracks' };
    watchJob(res.job, async (job) => {
      S.outcome = job;
      await loadCore();
      await loadLibrary();
    });
  });
}

// ------------------------------------------------------------ edit modal

const EDIT_FIELDS = [
  ['title', 'Title'], ['artist', 'Artist'], ['album', 'Album'],
  ['album_artist', 'Album artist'], ['track_no', 'Track no'],
  ['disc_no', 'Disc no'], ['year', 'Year'], ['date', 'Release date'],
  ['genre', 'Genre'], ['isrc', 'ISRC'],
];

function editModal(keys) {
  if (keys.length === 1) {
    return guard(async () => {
      S.detail = await api('/enrich/track/' + encodeURIComponent(keys[0]));
      showEditModal(keys);
    });
  }
  S.detail = null;
  showEditModal(keys);
}

function showEditModal(keys) {
  S.dialogKeys = keys.slice();
  const d = S.detail;
  const many = keys.length > 1;
  const val = (f) => {
    if (!d) return '';
    if (d.overrides && d.overrides[f] !== undefined) return d.overrides[f];
    return d.current[f] === null || d.current[f] === undefined ? '' : d.current[f];
  };
  const proposedRow = (f, label) => {
    if (!d || !d.proposed || d.proposed[f] === undefined) return '';
    const now = d.current[f];
    if (String(now === null ? '' : now) === String(d.proposed[f])) return '';
    return `<button class="btn sm" data-use-proposed="${f}"
      title="Use the proposed value">${h(String(d.proposed[f]))}</button>`;
  };
  const rows = EDIT_FIELDS.map(([f, label]) => `<tr>
      <td style="white-space:nowrap;color:var(--muted)">${label}</td>
      <td><input name="${f}" value="${h(val(f))}" style="width:100%;
        border:1px solid var(--line-2);border-radius:3px;padding:2px 6px"
        placeholder="${many ? 'leave blank to keep each track’s own value'
                            : 'blank clears any hand-typed value'}"></td>
      <td style="width:34%">${proposedRow(f)}</td>
    </tr>`).join('');

  const head = many
    ? `<p class="muted" style="margin-bottom:10px">Editing
        <strong>${num(keys.length)} tracks</strong>. Only the boxes you fill in
        are written; the rest keep whatever each track already has.</p>`
    : d ? `<div class="hstack" style="gap:10px;margin-bottom:10px;align-items:flex-start">
        ${d.cover ? `<img src="${h(d.cover)}" alt="" style="width:64px;height:64px;
           object-fit:cover;border:1px solid var(--line-2);border-radius:3px">` : ''}
        <div style="min-width:0">
          <div class="mono" style="font-size:10px;color:var(--muted);
            overflow:hidden;text-overflow:ellipsis">${h(d.rel_path)}</div>
          <div style="margin-top:4px">${stateTag({
            state: d.state, confidence: d.confidence, enrich_source: d.source,
            overrides: Object.keys(d.overrides || {}).length })}
            ${d.copies > 1 ? `<span class="tag" title="the same audio appears
              ${d.copies} times in the library; an edit covers every copy"
              >${d.copies} copies</span>` : ''}
            ${d.mbid ? `<a class="tag" target="_blank" rel="noopener"
              href="https://musicbrainz.org/recording/${h(d.mbid)}">musicbrainz</a>` : ''}
          </div>
        </div></div>` : '';

  const note = d && d.proposed && Object.keys(d.proposed).length
    ? `<p class="muted" style="margin-top:8px;font-size:11px">The buttons on the
        right are what the source proposed. Click one to drop it into the box.</p>`
    : '';

  showModal(many ? 'Edit ' + num(keys.length) + ' tracks' : 'Edit metadata',
    head + `<form id="edit-form"><table class="tbl"><tbody>${rows}</tbody></table></form>` + note,
    `<button class="btn" data-close="1">Cancel</button>
     ${d && d.state === 'awaiting'
       ? `<button class="btn" data-detail-act="reject">Reject match</button>
          <button class="btn" data-detail-act="accept">Accept match</button>` : ''}
     <button class="btn primary" data-save-edit="1">Save</button>`);
}

function saveEdit(keys) {
  const form = $('#edit-form');
  if (!form) return;
  const fields = {};
  for (const [f] of EDIT_FIELDS) {
    const el = form.elements[f];
    if (!el) continue;
    const v = el.value.trim();
    // Over a selection, an empty box means "leave this field alone" - there
    // is no single existing value to clear. On one track it means "forget
    // what I typed here", which is the only way back to the source's answer.
    if (keys.length > 1 && v === '') continue;
    fields[f] = v;
  }
  if (!Object.keys(fields).length) return closeModal();
  closeModal();
  return guard(async () => {
    await api('/enrich/edit', {
      method: 'POST', body: JSON.stringify({ content_keys: keys, fields }),
    });
    await loadCore();
    await loadLibrary();
  });
}

// ------------------------------------------------------ write tags modal

function writeTagsModal(keys) {
  S.dialogKeys = keys.slice();
  S.writePreview = null;
  return guard(async () => {
    // Asked for before the dialog opens, not after the write: the one action
    // here that cannot be undone is the one that should be readable first.
    S.writePreview = await api('/enrich/write/preview', {
      method: 'POST', body: JSON.stringify({ content_keys: keys }),
    });
    showWriteModal(keys);
  });
}

const FIELD_LABEL = {
  title: 'Title', artist: 'Artist', album: 'Album',
  album_artist: 'Album artist', track_no: 'Track no', disc_no: 'Disc no',
  year: 'Year', date: 'Release date', genre: 'Genre', isrc: 'ISRC',
};

function showWriteModal(keys) {
  const p = S.writePreview || { files: [], writable: 0, missing: 0,
                                nothing_to_write: 0, no_artwork: 0,
                                total: keys.length, shown: 0 };
  const one = p.files.length === 1 ? p.files[0] : null;

  const changeRows = (f) => {
    const fields = Object.keys(f.changes);
    if (!fields.length) {
      return `<p class="muted">Every tag in this file already matches the
        catalog. Writing would change nothing.</p>`;
    }
    return `<table class="tbl"><thead><tr>
        <th style="width:22%">Field</th><th>In the file now</th>
        <th>Would become</th></tr></thead><tbody>
      ${fields.map(k => `<tr>
        <td style="color:var(--muted)">${h(FIELD_LABEL[k] || k)}</td>
        <td class="clip">${f.changes[k].from === null || f.changes[k].from === undefined
          ? '<span class="faint">empty</span>' : h(String(f.changes[k].from))}</td>
        <td class="clip"><strong>${h(String(f.changes[k].to))}</strong></td>
      </tr>`).join('')}
    </tbody></table>`;
  };

  // Everything that would stop this doing what the user expects, said before
  // they press the button rather than reported as a zero afterwards.
  const warnings = [];
  if (p.missing) {
    warnings.push(`${num(p.missing)} of these files ${p.missing === 1 ? 'is' : 'are'}
      not where the catalog says. Rescan the library folder to fix the paths,
      then try again.`);
  }
  if (p.no_change === p.shown && p.shown && !p.missing) {
    warnings.push(`Nothing would change: ${p.shown === 1 ? 'this file already carries'
      : 'these files already carry'} the values the catalog holds. Identify
      ${p.shown === 1 ? 'it' : 'them'} first, or edit the metadata by hand,
      and there will be something to write.`);
  } else if (p.no_change) {
    warnings.push(`${num(p.no_change)} of these files already match the catalog
      and would not change.`);
  }
  if (p.renormalised) {
    warnings.push(`${num(p.renormalised)} of these files ${p.renormalised === 1
      ? 'is' : 'are'} stored under a differently normalised name than the disk
      uses. They are found and written correctly; a rescan tidies the catalog.`);
  }
  if (p.nothing_to_write) {
    warnings.push(`${num(p.nothing_to_write)} ${p.nothing_to_write === 1 ? 'file has' : 'files have'}
      nothing to write: the catalog holds no values for ${p.nothing_to_write === 1 ? 'it' : 'them'}
      yet. Identify ${p.nothing_to_write === 1 ? 'it' : 'them'} first, or type something in.`);
  }

  const artNote = p.no_artwork === p.shown && p.shown
    ? `<div class="faint" style="font-size:11px;margin-top:4px">
        ${p.shown === 1 ? 'This file is not' : 'None of these files are'} matched to
        a release yet, so there is no cover to fetch. Identify
        ${p.shown === 1 ? 'it' : 'them'} first and the box will have something to do.</div>`
    : '';

  const body = `
    <div class="notice bad" style="margin-bottom:10px">
      This rewrites the audio files themselves. Everything else in this screen
      edits the catalog and can be undone by clicking the other button; this
      cannot.</div>
    ${warnings.map(w => `<div class="notice warn" style="margin-bottom:10px">
      ${icon('i-warn')}<div>${w}</div></div>`).join('')}
    ${one
      ? `<div class="mono" style="font-size:10px;color:var(--muted);margin-bottom:8px;
           overflow:hidden;text-overflow:ellipsis">${h(one.rel_path)}</div>
         ${changeRows(one)}`
      : `<p class="muted">${num(p.writable)} of ${num(p.total)} selected file${p.total === 1 ? '' : 's'}
           would be rewritten with what the catalog now says.
           ${p.total > p.shown ? `Showing the first ${num(p.shown)}.` : ''}</p>
         <div style="max-height:34vh;overflow:auto;border:1px solid var(--line-2);
              border-radius:4px;margin-top:8px">
         <table class="tbl"><tbody>${p.files.map(f => `<tr>
           <td class="clip mono" style="font-size:10px">${h(f.rel_path)}</td>
           <td style="width:34%">${f.missing
             ? '<span class="tag bad">not on disk</span>'
             : Object.keys(f.changes).length
               ? `<span class="tag ok">${Object.keys(f.changes).length} field${
                   Object.keys(f.changes).length === 1 ? '' : 's'}</span>`
               : '<span class="tag">no change</span>'}</td>
         </tr>`).join('')}</tbody></table></div>`}
    <p class="muted" style="margin-top:10px">Each file is written to a copy,
      read back, and only then swapped in, so an interrupted write leaves the
      original intact.</p>
    <label class="hstack" style="gap:6px;margin-top:6px">
      <input type="checkbox" id="wt-art" ${p.no_artwork === p.shown ? 'disabled' : ''}>
      <span>Also replace the cover with the release\u2019s own front cover.
        Worth it for downloads, whose artwork is a video frame.</span></label>
    ${artNote}`;

  showModal('Write tags into ' + num(p.writable || 0) + ' file'
    + (p.writable === 1 ? '' : 's'), body,
    `<button class="btn" data-close="1">Cancel</button>
     <button class="btn danger" data-run-write="1" ${p.writable ? '' : 'disabled'}
       >Write ${num(p.writable || 0)} file${p.writable === 1 ? '' : 's'}</button>`);
}

function startWriteTags(keys) {
  const art = $('#wt-art');
  const artwork = !!(art && art.checked);
  closeModal();
  return guard(async () => {
    const res = await api('/enrich/write', {
      method: 'POST',
      body: JSON.stringify({ content_keys: keys, artwork }),
    });
    S.outcome = null;
    S.job = { id: res.job, kind: 'write-tags', state: 'running', done: 0,
              total: keys.length, label: num(keys.length) + ' files' };
    watchJob(res.job, async (job) => {
      S.outcome = job;
      // Writing tags changes every content key it touches, so the selection
      // now names files that no longer exist under those keys. Dropping it
      // is more honest than leaving a selection that silently acts on
      // nothing.
      S.sel.clear(); S.anchor = null;
      await loadCore();
      await loadLibrary();
    });
  });
}

// ---------------------------------------------------------------- events

async function guard(fn) {
  S.error = null;
  try { await fn(); } catch (e) { S.error = e.message; }
  render();
}

document.addEventListener('click', (ev) => {
  if (!isTrackView()) return;
  const box = ev.target.closest('[data-pick]');
  if (box) {
    // The checkbox is a plain toggle whatever modifier is held: it is the
    // one control on the row whose meaning should not change under ctrl.
    const t = S.tracks[+box.dataset.pick];
    if (!t) return;
    if (S.sel.has(t.content_key)) S.sel.delete(t.content_key);
    else S.sel.add(t.content_key);
    S.anchor = +box.dataset.pick;
    return render();
  }
  if (ev.target.id === 'pick-all') return selectPage(ev.target.checked);
  if (ev.target.closest('[data-dismiss-outcome]')) {
    S.outcome = null;
    return render();
  }
  if (ev.target.closest('[data-clear-filters]')) {
    S.filters[S.view] = BLANK_FILTER();
    S.offset = 0;
    return guard(loadLibrary);
  }
  const goto = ev.target.closest('[data-goto]');
  if (goto) {
    S.offset = (+goto.dataset.goto - 1) * S.limit;
    return guard(loadLibrary);
  }
  const unl = ev.target.closest('[data-unl-drop]');
  if (unl) {
    return setUnlistedIgnore(
      S.unlistedIgnore.filter(n => n !== unl.dataset.unlDrop));
  }
  if (ev.target.closest('[data-cols-reset]')) {
    resetColumnLayout(S.view);
    return render();
  }
  const th = ev.target.closest('[data-sort]');
  // A heading that has just been widened is not a heading that was clicked.
  if (th && !DRAG.justResized && !ev.target.closest('[data-grip]')) {
    const col = th.dataset.sort;
    // The same column again turns it around; a different one starts
    // ascending, which is what "sort by this" means before you have said
    // which way.
    S.sort = S.sort.col === col
      ? { col, dir: S.sort.dir === 'asc' ? 'desc' : 'asc' }
      : { col, dir: 'asc' };
    S.offset = 0;
    return guard(loadLibrary);
  }
  const chip = ev.target.closest('[data-state-filter]');
  if (chip) {
    S.filter.state = chip.dataset.stateFilter;
    S.offset = 0;
    return guard(loadLibrary);
  }
  const act = ev.target.closest('[data-sel-act]');
  if (act) return selAction(act.dataset.selAct);
  const detailAct = ev.target.closest('[data-detail-act]');
  if (detailAct) return decide(detailAct.dataset.detailAct, S.dialogKeys);
  if (ev.target.closest('[data-run-enrich]')) return startEnrich(S.dialogKeys);
  if (ev.target.closest('[data-run-write]')) return startWriteTags(S.dialogKeys);
  if (ev.target.closest('[data-save-edit]')) return saveEdit(S.dialogKeys);
  if (ev.target.closest('[data-use-origin]')) {
    // Search from the file as it arrived rather than as it is now: the
    // way back when a later edit or a wrong match led the tags astray.
    const form = $('#enrich-query');
    const was = (S.detail && S.detail.origin && S.detail.origin.initial_tags) || {};
    if (form) {
      for (const f of ['artist', 'title', 'album']) {
        form.elements[f].value = was[f] || '';
      }
    }
    return;
  }
  const use = ev.target.closest('[data-use-proposed]');
  if (use) {
    const form = $('#edit-form');
    const field = use.dataset.useProposed;
    if (form && form.elements[field] && S.detail) {
      form.elements[field].value = S.detail.proposed[field];
      form.elements[field].focus();
    }
    return;
  }
  // A click on the row body selects; anything inside it that is its own
  // control has already returned above.
  const row = ev.target.closest('[data-row]');
  if (row && !ev.target.closest('a,button,input')) {
    return selectRow(+row.dataset.row, ev);
  }
});

document.addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-act],[data-facet],[data-scope],[data-page],'
    + '[data-plan],[data-sync],[data-unset],[data-pick-set],[data-save-set],'
    + '[data-detect-pl],'
    + '[data-close],[data-scrim],[data-usevol],[data-scan],[data-volumes],'
    + '[data-probe],[data-copylog],[data-inbox-seen],[data-clear-urls],'
    + '[data-play],[data-play-close],[data-useurl],[data-keep],[data-unkeep],'
    + '[data-keeprun],[data-pl-refresh],[data-sl-remove],[data-sl-clear],'
    + '[data-queue],[data-reset-open],[data-reset-go],[data-maint],'
    + '[data-dl-halt],[data-dl-resume],[data-dl-forget],'
    + '[data-pack],[data-unpack],[data-relocate],'
    + '[data-sl-apply],[data-pl-tolist],'
    + '[data-close-inspector],[data-dismiss-outcome],'
    + '[data-root-hide],[data-root-remove],[data-root-forget],'
    + '[data-plsync],[data-playlist],[data-track],'
    + '[data-dupe-merge],[data-dupe-dismiss],[data-dupe-master],'
    + '[data-dupe-unmerge],[data-versions-toggle]');
  if (!t) return;

  const d = t.dataset;
  if (d.scrim && ev.target !== t) return;

  if (d.close || d.scrim) return closeModal();

  if (d.dismissOutcome) { S.outcome = null; return render(); }

  if (d.play) return playTrack(+d.play);
  if (d.playClose) return stopPlaying();
  if (d.useurl) {
    setDownloadUrls(d.useurl);
    render();
    const box = $('#dl-form') && $('#dl-form').urls;
    if (box) box.focus();
    return;
  }
  if (d.keep) {
    // The listing is what names it, so this asks the server, which probes.
    return guard(async () => {
      S.dl = await api('/download/keep', {
        method: 'POST',
        body: JSON.stringify({ url: d.keep,
                               root: (S.dl && S.dl.config.root) || '' }),
      });
    });
  }
  if (d.unkeep) {
    return guard(async () => {
      S.dl = await api('/download/keep', {
        method: 'POST',
        body: JSON.stringify({ url: d.unkeep, kept: false }),
      });
    });
  }
  if (d.keeprun) {
    return guard(async () => {
      const res = await api('/download/keep/run', {
        method: 'POST', body: JSON.stringify({ url: d.keeprun }),
      });
      S.dlResult = null;
      S.job = { id: res.job, kind: 'download', state: 'running', done: 0,
                total: 0, label: d.keeprun };
      watchJob(res.job, async (job) => {
        S.dlResult = job.result || null;
        await loadCore();
        await loadDownload();
        followAutoEnrich(job, 'the new files');
      });
    });
  }

  if (d.pack) {
    return guard(async () => {
      S.portable = await api('/portable/pack', { method: 'POST' });
    });
  }
  if (d.unpack) {
    const folder = ($('#unpack-folder') || {}).value || '';
    const replace = !!($('#unpack-replace') || {}).checked;
    return guard(async () => {
      S.unpacked = await api('/portable/unpack', {
        method: 'POST', body: JSON.stringify({ folder: folder.trim(), replace }),
      });
      S.portable = await api('/portable');
      await loadCore();
    });
  }
  if (d.relocate) {
    const box = document.querySelector(
      `[data-relocate-to="${CSS.escape(d.relocate)}"]`);
    return guard(async () => {
      await api('/roots/relocate', {
        method: 'POST',
        body: JSON.stringify({ old: d.relocate, new: (box ? box.value : '').trim() }),
      });
      await loadCore();
    });
  }
  if (d.dlHalt) {
    if (!S.job) return;
    return guard(async () => {
      await api('/jobs/' + S.job.id + '/' + d.dlHalt, { method: 'POST' });
    });
  }
  if (d.dlResume || d.dlForget) {
    return guard(async () => {
      const res = await api('/download/paused/' + (d.dlResume || d.dlForget)
        + (d.dlResume ? '/resume' : ''), { method: d.dlResume ? 'POST' : 'DELETE' });
      if (res.job) {
        S.dlResult = null;
        S.job = { id: res.job, kind: 'download', state: 'running', done: 0,
                  total: 0, label: 'resume' };
        watchJob(res.job, (job) => jobFinished(job));
      }
      await loadDownload();
    });
  }
  if (d.maint) {
    return guard(async () => {
      const res = await api('/maintenance/' + d.maint, { method: 'POST' });
      S.job = { id: res.job, kind: 'maintenance', state: 'running', done: 0,
                total: 0, label: d.maint };
      watchJob(res.job, (job) => jobFinished(job));
    });
  }
  if (d.act === 'review') {
    // Out of the problems page and into the library, asking the one
    // question the card was about.
    S.view = 'library'; S.filter.state = 'awaiting';
    S.offset = 0; S.sel.clear(); S.anchor = null;
    return guard(loadLibrary);
  }
  if (d.dupeMerge) return guard(() => mergeDupe(+d.dupeMerge));
  if (d.dupeDismiss) {
    const grp = S.dupes[+d.dupeDismiss];
    return guard(async () => {
      await api('/dupes/dismiss', { method: 'POST', body: JSON.stringify({
        content_keys: grp.tracks.map(t => t.content_key) }) });
      await loadDupes();
    });
  }
  if (d.dupeMaster || d.dupeUnmerge) {
    return guard(async () => {
      await api('/dupes/' + (d.dupeMaster ? 'master' : 'unmerge'), {
        method: 'POST', body: JSON.stringify({
          content_key: d.dupeMaster || d.dupeUnmerge }) });
      S.inspectKey = null;
      await loadLibrary();
    });
  }
  if (d.versionsToggle) {
    S.showVersions = !S.showVersions;
    S.offset = 0;
    return guard(loadLibrary);
  }
  if (d.act === 'view') {
    S.view = d.arg;
    if (d.arg === 'unlisted') {
      S.offset = 0; S.sel.clear(); S.anchor = null;
      S.sort = { col: null, dir: 'asc' };
      return guard(loadUnlisted);
    }
    if (d.arg === 'library' || d.arg === 'inbox') {
      // The views share the table, and the offset, selection and sort
      // belong to the question that was asked, not to the one being left.
      S.offset = 0; S.sel.clear(); S.anchor = null;
      S.sort = { col: null, dir: 'asc' };
      return guard(loadLibrary);
    }
    if (d.arg === 'utilities') {
      S.portable = null;
      S.unpacked = null;
      S.resetInfo = null;
      S.resetResult = null;
    }
    if (d.arg === 'syncList') {
      S.syncList = null;
      render();
      return guard(async () => { S.syncList = await api('/sync-list'); });
    }
    if (d.arg === 'dupes') {
      S.dupes = null;
      render();
      return guard(loadDupes);
    }
    if (d.arg === 'problems') {
      S.problems = null;
      render();
      return guard(async () => { S.problems = await api('/problems'); });
    }
    if (d.arg === 'download') {
      S.dlProbe = null;
      // Painted first, loaded second: the config asks yt-dlp its version and
      // looks for a JavaScript runtime, and a click that waits on both is a
      // click that feels broken.
      render();
      return guard(loadDownload);
    }
    return render();
  }
  if (d.act === 'device' || d.playlistJump) { /* handled below */ }
  if (d.act === 'device') {
    S.view = 'device'; S.deviceId = +d.arg; S.plan = null; S.job = null;
    return guard(async () => {
      await loadLog(+d.arg);
      const dev = S.devices.find(x => x.id === +d.arg);
      if (dev && dev.mounted_at) await loadPlan(+d.arg);
    });
  }
  if (d.slRemove) {
    const body = { remove: [[d.slRemove, t.dataset.ref]] };
    return guard(async () => {
      S.syncList = await api('/sync-list', {
        method: 'POST', body: JSON.stringify(body) });
      await loadCore();
    });
  }
  if (d.slClear) {
    return guard(async () => {
      S.syncList = await api('/sync-list', {
        method: 'POST', body: JSON.stringify({ clear: true }) });
      await loadCore();
    });
  }
  if (d.slApply) {
    const did = +d.slApply;
    return guard(async () => {
      const out = await api('/sync-list/apply', {
        method: 'POST', body: JSON.stringify({ device: did }) });
      S.outcome = { ok: true, text: `${num(out.applied)} rules added - that `
        + `device is now set to carry ${num(out.tracks)} tracks `
        + `(${bytes(out.bytes)})`
        + (out.mounted ? '.' : ', and will copy them when it is connected.') };
      await loadCore();
      S.syncList = await api('/sync-list');
    });
  }
  if (d.queue) {
    const body = { pause: d.queue === 'pause', resume: d.queue === 'resume',
                   clear: d.queue === 'clear' };
    return guard(async () => {
      await api('/enrich/queue', { method: 'POST', body: JSON.stringify(body) });
      await loadCore();
    });
  }
  if (d.plRefresh) {
    const id = +d.plRefresh;
    return guard(async () => {
      const res = await api('/playlists/' + id + '/refresh', { method: 'POST' });
      S.dlResult = null;
      S.job = { id: res.job, kind: 'download', state: 'running', done: 0,
                total: 0, label: S.playlistDetail.playlist.name };
      watchJob(res.job, async (job) => {
        S.dlResult = job.result || null;
        // The playlist, the library and every device plan that mentions it
        // have all just changed.
        await loadCore();
        await loadPlaylist(id, { peek: true });
        await loadLibrary();
        followAutoEnrich(job, 'the new files');
      });
    });
  }
  if (d.plTolist) {
    return guard(async () => {
      S.syncList = await api('/sync-list', {
        method: 'POST',
        body: JSON.stringify({ add: [['playlist', d.plTolist]] }) });
      S.outcome = { ok: true, text: `Playlist added. The sync list now holds `
        + `${num(S.syncList.tracks)} tracks (${bytes(S.syncList.bytes)}).` };
      await loadCore();
    });
  }
  if (d.act === 'playlist') return openPlaylist(+d.arg);
  if (d.playlist) return openPlaylist(+d.playlist);
  if (d.facet !== undefined) {
    S.filter[d.facet] = d.value;
    // The panes read left to right, so choosing in one clears the choices
    // to its right rather than leaving a pair that matches nothing.
    if (d.facet === 'decade') {
      S.filter.genre = ''; S.filter.artist = ''; S.filter.album = '';
    }
    if (d.facet === 'genre') { S.filter.artist = ''; S.filter.album = ''; }
    if (d.facet === 'artist') { S.filter.album = ''; }
    S.offset = 0;
    return guard(loadLibrary);
  }
  if (d.scope) { S.scopeId = +d.scope; return guard(loadLibrary); }
  if (d.page) {
    S.offset = Math.max(0, S.offset + (+d.page) * S.limit);
    return guard(loadLibrary);
  }
  if (d.plan) return guard(() => loadPlan(+d.plan));
  if (d.detectPl) {
    const id = +d.detectPl;
    return guard(async () => {
      const r = await api('/devices/' + id + '/detect-playlists',
                          { method: 'POST', body: JSON.stringify({}) });
      await loadPlan(id);
      if (!r.applied) {
        // The re-planned view would otherwise look identical, which reads
        // as a dead button rather than as a refusal to guess.
        throw new Error('None of the ' + r.total + ' playlist files on the '
          + 'card carry a name this catalog knows, so the naming was left '
          + 'alone. Set it by hand with device config --playlist-template.');
      }
    });
  }
  if (d.sync) {
    const id = +d.sync;
    return guard(async () => {
      const res = await api('/devices/' + id + '/sync', {
        method: 'POST', body: JSON.stringify({}),
      });
      S.job = { id: res.job, kind: 'sync', state: 'running', done: 0, total: 0,
                label: (S.devices.find(x => x.id === id) || {}).name || '' };
      watchJob(res.job, async () => {
        await loadCore();
        await loadLog(id);
        await loadPlan(id);
      });
    });
  }
  if (d.unset) {
    const [kind, ref] = d.unset.split('|');
    return guard(async () => {
      await api('/devices/' + S.deviceId + '/set', {
        method: 'POST', body: JSON.stringify({ remove: [[kind, ref]] }),
      });
      await loadCore();
      await loadPlan(S.deviceId);
    });
  }
  if (d.pickSet) {
    const dev = S.devices.find(x => x.id === +d.pickSet);
    return pickSetModal(dev);
  }
  if (d.saveSet) {
    const id = +d.saveSet;
    const dev = S.devices.find(x => x.id === id);
    const have = new Set(dev.rules.filter(r => r.kind === 'playlist').map(r => r.ref));
    const add = [], remove = [];
    document.querySelectorAll('[data-pl]').forEach(cb => {
      const name = cb.dataset.pl;
      if (cb.checked && !have.has(name)) add.push(['playlist', name]);
      if (!cb.checked && have.has(name)) remove.push(['playlist', name]);
    });
    closeModal();
    return guard(async () => {
      await api('/devices/' + id + '/set', {
        method: 'POST', body: JSON.stringify({ add, remove }),
      });
      await loadCore();
      const dv = S.devices.find(x => x.id === id);
      if (dv && dv.mounted_at) await loadPlan(id);
    });
  }
  if (d.plsync) {
    const id = +d.plsync, name = d.name;
    const dev = S.devices.find(x => x.id === id);
    const on = dev.rules.some(r => r.kind === 'playlist' && r.ref === name);
    return guard(async () => {
      await api('/devices/' + id + '/set', {
        method: 'POST',
        body: JSON.stringify(on ? { remove: [['playlist', name]] }
                                : { add: [['playlist', name]] }),
      });
      await loadCore();
      await loadPlaylist(S.playlistId, { peek: true });
    });
  }
  if (d.usevol) {
    const form = $('#add-device-form');
    if (form) {
      form.root.value = d.usevol;
      if (!form.name.value) form.name.value = d.label || d.usevol;
      form.name.focus();
    }
    return;
  }
  if (d.copylog) {
    const el = $('#dl-console');
    if (!el) return;
    const text = el.innerText;
    // The clipboard API needs a secure context; 127.0.0.1 counts as one.
    // Selecting the text is the fallback, and is what a user would have
    // done by hand anyway.
    const say = (ok) => {
      t.textContent = ok ? 'Copied' : 'Select and copy';
      setTimeout(() => { t.textContent = 'Copy'; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => say(true), () => {
        selectNode(el); say(false);
      });
    } else {
      selectNode(el);
      say(false);
    }
    return;
  }
  if (d.clearUrls) {
    setDownloadUrls('');
    const form = $('#dl-form');
    if (form) { form.urls.value = ''; form.urls.focus(); }
    return render();
  }
  if (d.probe) {
    const form = $('#dl-form');
    const url = ((form && form.urls.value) || '').trim().split(/\s+/)[0];
    if (!url) return;
    S.dlProbe = null;
    return guard(async () => {
      S.dlProbe = await api('/download/probe', {
        method: 'POST', body: JSON.stringify({ url }),
      });
    });
  }
  if (d.inboxSeen) {
    return guard(async () => {
      await api('/inbox/seen', { method: 'POST', body: JSON.stringify({}) });
      S.sel.clear(); S.anchor = null; S.offset = 0;
      await loadCore();
      await loadLibrary();
    });
  }
  if (d.rootHide) {
    const hidden = d.hidden === '1';
    return guard(async () => {
      await api('/roots/hide', {
        method: 'POST',
        body: JSON.stringify({ root: d.rootHide, hidden }),
      });
      await loadCore();
      if (isTrackView()) await loadLibrary();
    });
  }
  if (d.rootRemove) return removeRootModal(d.rootRemove);
  if (d.resetOpen) return resetModal();
  if (d.resetGo) return runReset();
  if (d.rootForget) {
    const root = d.rootForget;
    closeModal();
    return guard(async () => {
      await api('/roots/remove', {
        method: 'POST',
        body: JSON.stringify({ root, forget_tracks: true }),
      });
      await loadCore();
      if (isTrackView()) await loadLibrary();
    });
  }
  if (d.closeInspector) {
    S.sel.clear(); S.anchor = null;
    return render();
  }
  if (d.volumes) return loadVolumes();
  if (d.scan) return startScan(d.scan);
});

function startScan(root) {
  return guard(async () => {
    const res = await api('/scan', {
      method: 'POST', body: JSON.stringify({ root }),
    });
    S.job = { id: res.job, kind: 'scan', label: root, state: 'running',
              done: 0, total: 0 };
    watchJob(res.job, async (job) => {
      await loadCore();
      if (S.view === 'library') await loadLibrary();
      followAutoEnrich(job, root);
    });
  });
}

/* Identification is no longer a job that a scan or a download starts.

   It is a queue the program works through at one request a second, whatever
   else is going on - so what a finished download hands back is how many
   files it put on that queue, and the queue says the rest itself, in the
   status strip and on its own card. Kept as a function because both callers
   still want the library to catch up with whatever just landed. */
async function followAutoEnrich(job, label) {
  await loadCore();
  if (isTrackView()) await loadLibrary();
  render();
}

/* The backlog, in the status strip.

   Shown only when nothing louder is happening: a download has a progress
   bar of its own and this would only compete with it. What it says is the
   thing that used to be invisible - that the program is still working
   through the files that arrived twenty minutes ago. */
function queueMeter() {
  const q = (S.stats || {}).enrich_queue;
  if (!q || (!q.waiting && !q.current)) return null;
  const bits = [`${num(q.waiting)} waiting`];
  if (q.paused) bits.unshift('paused');
  if (q.deferred) bits.push(`${num(q.deferred)} retrying`);
  return { label: 'Identifying', meta: bits.join(' \u00b7 ') };
}

document.addEventListener('submit', (ev) => {
  const f = ev.target;
  ev.preventDefault();
  if (f.id === 'scan-form') return startScan(f.root.value.trim());
  if (f.id === 'add-device-form') {
    return guard(async () => {
      await api('/devices', {
        method: 'POST',
        body: JSON.stringify({ root: f.root.value.trim(),
                               name: f.name.value.trim(),
                               profile: f.profile.value }),
      });
      await loadCore();
      S.view = 'device';
      S.deviceId = S.devices[S.devices.length - 1].id;
      await loadLog(S.deviceId);
    });
  }
  if (f.id === 'dl-form') {
    const urls = f.urls.value.split(/\s+/).filter(Boolean);
    if (!urls.length) return;
    S.dlProbe = null; S.dlResult = null;
    return guard(async () => {
      const res = await api('/download', {
        method: 'POST',
        body: JSON.stringify({
          urls, root: f.root.value, playlist: f.playlist.value.trim(),
          no_playlist: f.single.checked, archive: !f.again.checked,
        }),
      });
      S.job = { id: res.job, kind: 'download', state: 'running', done: 0,
                total: 0, label: urls.length === 1 ? urls[0] : urls.length + ' URLs' };
      watchJob(res.job, async (job) => {
        S.dlResult = job.result || null;
        // A download changes the library, the playlists and every device
        // plan that mentions them, so the whole core is reloaded.
        await loadCore();
        await loadDownload();
        followAutoEnrich(job, 'the new files');
      });
    });
  }
  if (f.id === 'dl-cookie-form' || f.id === 'dl-output-form'
      || f.id === 'dl-runtime-form') {
    const body = {};
    for (const el of f.elements) if (el.name) body[el.name] = el.value;
    return guard(async () => { S.dl = await api('/download/config', {
      method: 'POST', body: JSON.stringify(body),
    }); });
  }
  if (f.id === 'pl-form') {
    return guard(async () => {
      const out = await api('/playlists/import', {
        method: 'POST',
        body: JSON.stringify({ directory: f.directory.value.trim() }),
      });
      await loadCore();
      const tot = out.results.reduce((a, r) => a + r.total, 0);
      const mat = out.results.reduce((a, r) => a + r.matched, 0);
      const enr = out.results.reduce((a, r) => a + (r.enriched || 0), 0);
      render();
      const el = $('#pl-result');
      if (el) {
        el.innerHTML = `<div class="notice ok">${icon('i-check')}<div>
          ${out.results.length} playlists, ${num(tot)} entries,
          ${num(mat)} matched${enr ? `, ${num(enr)} source URIs added` : ''}.</div></div>`;
      }
    });
  }
});

function setDownloadUrls(text) {
  S.dlUrls = text;
  try {
    if (text.trim()) localStorage.setItem('lz.dl.urls', text);
    else localStorage.removeItem('lz.dl.urls');
  } catch (e) { /* private mode, or storage full: the box still works */ }
}

// The page picker. A select rather than a number box: the pages that
// exist are known, so offering any other number would be offering a
// mistake.
document.addEventListener('change', (ev) => {
  if (ev.target.id === 'unl-add' && ev.target.value) {
    return setUnlistedIgnore(S.unlistedIgnore.concat([ev.target.value]));
  }
  if (ev.target.id !== 'page-jump') return;
  S.offset = Math.max(0, (+ev.target.value - 1) * S.limit);
  guard(loadLibrary);
});

let filterTimer = null;
document.addEventListener('input', (ev) => {
  const el = ev.target;
  if (el.name === 'urls' && el.form && el.form.id === 'dl-form') {
    // Kept on every keystroke rather than on submit: the list is worth most
    // exactly when the download failed and has to be tried again.
    return setDownloadUrls(el.value);
  }
  if (el.id === 'reset-files') {
    S.resetFiles = el.checked;
    return;
  }
  if (el.id === 'reset-word') {
    const go = $('#reset-go');
    if (go) go.disabled = el.value.trim() !== 'RESET';
    return;
  }
  if (el.dataset.filter) {
    S.navFilter[el.dataset.filter] = el.value;
    renderSidebar();
    const again = document.querySelector(`[data-filter="${el.dataset.filter}"]`);
    if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
    return;
  }
  if (el.dataset.pane) {
    S.paneFilter[el.dataset.pane] = el.value;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      const pos = el.selectionStart;
      render();
      const again = document.querySelector(`[data-pane="${el.dataset.pane}"]`);
      if (again) { again.focus(); again.setSelectionRange(pos, pos); }
    }, 60);
    return;
  }
  if (el.id === 'q') {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      S.filter.q = el.value; S.offset = 0;
      guard(loadLibrary).then(() => {
        const again = $('#q');
        if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
      });
    }, 220);
  }
});

$('#btn-rescan').addEventListener('click', () => {
  const roots = (S.stats && S.stats.roots) || [];
  if (!roots.length) { S.view = 'download'; return render(); }
  startScan(roots[0]);
});

document.addEventListener('keydown', (ev) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName);
  const modal = !!$('#modal-root').firstChild;

  if (ev.key === 'Escape') {
    if (modal) return closeModal();
    if (isTrackView() && S.sel.size) {
      S.sel.clear(); S.anchor = null;
      return render();
    }
    return;
  }
  if (modal) {
    // Enter submits the edit form, which is what a dialog full of text
    // boxes is expected to do.
    if (ev.key === 'Enter' && $('#edit-form') && ev.target.tagName !== 'BUTTON') {
      ev.preventDefault();
      return saveEdit(S.dialogKeys);
    }
    return;
  }
  if (typing || !isTrackView()) return;

  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'a') {
    ev.preventDefault();
    return selectPage(true);
  }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

  // Arrows walk the table; shift extends the run, which is how every other
  // list on the machine behaves.
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    const step = ev.key === 'ArrowDown' ? 1 : -1;
    const at = S.anchor === null ? (step > 0 ? -1 : S.tracks.length) : S.anchor;
    const next = Math.max(0, Math.min(S.tracks.length - 1, at + step));
    if (ev.shiftKey && S.anchor !== null) {
      const t = S.tracks[next];
      if (t) S.sel.add(t.content_key);
      S.anchor = next;
      return render();
    }
    return selectRow(next, null);
  }
  if (!S.sel.size) return;
  const key = ev.key.toLowerCase();
  if (key === 'e') { ev.preventDefault(); return selAction('enrich'); }
  if (key === 'a') { ev.preventDefault(); return selAction('accept'); }
  if (key === 'r') { ev.preventDefault(); return selAction('reject'); }
  if (key === 's') { ev.preventDefault(); return selAction('skip'); }
  if (key === 'u') { ev.preventDefault(); return selAction('raw'); }
  if (key === 'n') { ev.preventDefault(); return selAction('enriched'); }
  if (key === 'w') { ev.preventDefault(); return selAction('write'); }
  if (ev.key === 'Enter') { ev.preventDefault(); return selAction('edit'); }
});

// ------------------------------------------------------------------ boot

/* Where you were, so a reload puts you back there.

   Only the page and what it is about - not the filters, which would be a
   search you did not type reappearing on a page you thought was fresh, and
   not the selection, which would be an action pointed at rows you cannot
   remember choosing. */
const UI_KEY = 'lz.ui';

function saveUiState() {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify({
      view: S.view, playlistId: S.playlistId, deviceId: S.deviceId,
      scopeId: S.scopeId,
    }));
  } catch (e) { /* private mode: the session is as long as the page */ }
}

function readUiState() {
  try { return JSON.parse(localStorage.getItem(UI_KEY) || 'null') || {}; }
  catch (e) { return {}; }
}

(async function boot() {
  const was = readUiState();
  try {
    await loadCore();
    if (was.scopeId && S.devices.some(d => d.id === was.scopeId)) {
      S.scopeId = was.scopeId;
    }
    // Restored before the first load, so the first thing drawn is the page
    // that was open rather than the library flashing past on the way to it.
    if (was.view === 'playlist' && was.playlistId
        && S.playlists.some(p => p.id === was.playlistId)) {
      S.view = 'playlist';
      S.playlistId = was.playlistId;
      await loadPlaylist(was.playlistId);
    } else if (['inbox', 'unlisted', 'download', 'problems', 'dupes', 'syncList', 'normalize',
                'utilities',
                'device'].includes(was.view)) {
      S.view = was.view;
      if (was.view === 'device' && S.devices.some(d => d.id === was.deviceId)) {
        S.deviceId = was.deviceId;
      } else if (was.view === 'device') {
        S.view = 'library';
      }
    }
    if (S.view === 'unlisted') await loadUnlisted();
    else if (isTrackView()) await loadLibrary();
    if (S.view === 'download') await loadDownload();
    if (S.view === 'problems') S.problems = await api('/problems');
    if (S.view === 'dupes') await loadDupes();
    if (S.view === 'syncList') S.syncList = await api('/sync-list');
    if (S.view === 'device' && S.deviceId) {
      await loadLog(S.deviceId);
      const dev = S.devices.find(x => x.id === S.deviceId);
      if (dev && dev.mounted_at) await loadPlan(S.deviceId);
    }
    if (!S.stats.tracks) S.view = 'download';
    // Whatever the program is already doing, this tab now shows.
    await adoptRunningJob();
  } catch (e) {
    S.error = e.message;
  }
  render();
})();
