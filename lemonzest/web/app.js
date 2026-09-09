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
  deviceId: null,        // device being viewed
  scopeId: null,         // device the library's tick column edits
  playlistId: null,
  facets: { genre: [], artist: [], album: [] },
  filter: { q: '', genre: '', artist: '', album: '', state: '' },
  paneFilter: { genre: '', artist: '', album: '' },
  navFilter: { device: '', playlist: '' },
  tracks: [], tracksTotal: 0, offset: 0, limit: 200,
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

function libraryParams() {
  const p = new URLSearchParams();
  for (const k of ['q', 'genre', 'artist', 'album', 'state']) {
    if (S.filter[k]) p.set(k, S.filter[k]);
  }
  return p;
}

// The library table and the inbox are the same table over two questions.
function isTrackView() { return S.view === 'library' || S.view === 'inbox'; }

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
    ${opts.pill ? `<span class="pill">${h(opts.pill)}</span>` : ''}
    ${opts.n !== undefined ? `<span class="n">${h(opts.n)}</span>` : ''}
  </button>`;
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
      navRow({ act: 'view', arg: 'problems', label: 'Needs attention',
               icon: 'i-warn', n: num(st.attention), on: S.view === 'problems' })) +

    sect('Devices', String(S.devices.length),
      devs.map(d => navRow({
        act: 'device', arg: d.id, label: d.name, icon: 'i-device',
        pill: d.label || '', n: num(d.files),
        on: S.view === 'device' && S.deviceId === d.id,
        title: d.mounted_at ? d.name + ' - ' + d.mounted_at : d.name + ' - not mounted',
      })).join('') +
      navRow({ act: 'view', arg: 'addDevice', label: 'Add device...',
               icon: 'i-plus', add: true, on: S.view === 'addDevice' }),
      S.devices.length > 6 ? 'device' : null,
      'Filter ' + S.devices.length + ' devices',
      S.devices.length + ' paired \u00b7 ' + connected + ' connected',
      S.devices.length > 5) +

    sect('Playlists', String(S.playlists.length),
      pls.map(p => navRow({
        act: 'playlist', arg: p.id, label: p.name, icon: 'i-list',
        n: num(p.n), pill: p.origin === 'apple_music' ? 'APPL'
          : p.origin === 'spotify' ? 'SPOT' : 'LOCAL',
        on: S.view === 'playlist' && S.playlistId === p.id,
      })).join('') || '<div class="foot">nothing imported yet</div>',
      S.playlists.length > 6 ? 'playlist' : null,
      'Filter ' + S.playlists.length + ' playlists',
      plFilter ? pls.length + ' of ' + S.playlists.length + ' shown' : null,
      S.playlists.length > 5) +

    sect('Tools', undefined,
      navRow({ act: 'view', arg: 'import', label: 'Scan & import',
               icon: 'i-clock', on: S.view === 'import' }) +
      navRow({ act: 'view', arg: 'download', label: 'Download',
               icon: 'i-dl', on: S.view === 'download',
               title: 'Fetch audio from YouTube into the library' }));
}

// --------------------------------------------------------------- library

function renderBrowser() {
  const pane = (key, label) => {
    const all = S.facets[key] || [];
    const f = S.paneFilter[key].toLowerCase();
    const shown = f ? all.filter(x => x.value.toLowerCase().includes(f)) : all;
    return `<div class="col">
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
  return `<div class="browser">${pane('genre', 'Genre')}${pane('artist', 'Artist')}${pane('album', 'Album')}</div>`;
}

// The four enrichment states, and how each one reads in the table. The
// wording is the promise: `skipped` says "never" because that is literally
// what the backend does with it, and a softer word would be a lie the next
// run tells.
const STATE_TAG = {
  raw:      { cls: '',     label: 'raw',      hint: 'never looked up. A run will pick this one up.' },
  awaiting: { cls: 'warn', label: 'review',   hint: 'a match is waiting for your decision.' },
  enriched: { cls: 'ok',   label: 'enriched', hint: 'values are in the catalog.' },
  skipped:  { cls: 'bad',  label: 'skipped',  hint: 'excluded. Never looked up again until you change it.' },
};

function stateTag(t) {
  const st = STATE_TAG[t.state] || STATE_TAG.raw;
  const conf = (t.state === 'awaiting' || t.state === 'enriched') && t.confidence
    ? ` ${t.confidence >= 1 ? '1.00' : t.confidence.toFixed(2)}` : '';
  const via = t.enrich_source ? ` via ${t.enrich_source}` : '';
  return `<span class="tag ${st.cls}" title="${h(st.label + ': ' + st.hint + via)}"
    >${st.label}${conf}</span>`
    + (t.overrides ? '<span class="tag" title="carries hand-typed values">edited</span>' : '');
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

/* The track table, shared by the library and the inbox. They are the same
   rows over two questions - "what do I have" and "what just arrived" - so
   they are one table with one selection model and one set of shortcuts.
   The only difference is the second column: the library asks whether a
   track is on the device you are syncing, the inbox asks when it landed. */
function trackTable(opts) {
  const inbox = !!(opts && opts.inbox);
  const scope = S.devices.find(d => d.id === S.scopeId);
  const rows = S.tracks.map((t, i) => `
    <tr data-track="${t.id}" data-row="${i}" data-key="${h(t.content_key)}"
        class="${S.sel.has(t.content_key) ? 'sel' : ''}">
      <td class="tick"><input type="checkbox" data-pick="${i}" tabindex="-1"
        ${S.sel.has(t.content_key) ? 'checked' : ''}></td>
      ${inbox
        ? `<td class="mono" title="${h(when(t.added_at))}">${h(ago(t.added_at))}</td>`
        : `<td class="tick">${scope
            ? `<span title="${t.on_device ? 'on ' + h(scope.name) : 'not on ' + h(scope.name)}"
                 style="color:${t.on_device ? 'var(--accent)' : 'var(--line-2)'}">
                 ${t.on_device ? '&#9679;' : '&#9675;'}</span>`
            : ''}</td>`}
      <td class="num">${t.track_no || ''}</td>
      <td class="clip" title="${h(t.path)}">${h(t.title || (t.rel_path || '').split('/').pop())}
        ${t.empty ? '<span class="tag bad">empty</span>' : ''}
        ${!t.title && !t.empty ? '<span class="tag warn">untagged</span>' : ''}</td>
      <td class="num">${dur(t.duration)}</td>
      <td class="clip">${h(t.artist || '')}</td>
      <td class="clip">${h(t.album || '')}</td>
      <td>${stateTag(t)}</td>
      <td class="mono">${h((t.ext || '').replace('.', '').toUpperCase())}
        ${t.bitrate ? Math.round(t.bitrate / 1000) : ''}</td>
      ${inbox ? '' : `<td class="mono" title="added ${h(when(t.added_at))}
        \u00b7 file modified ${h(when(t.mtime))}">${h(ago(t.added_at))}</td>`}
      <td class="mono">${h(t.isrc || '')}</td>
    </tr>`).join('');

  const allShown = S.tracks.length > 0
    && S.tracks.every(t => S.sel.has(t.content_key));
  const from = S.offset + 1, to = Math.min(S.offset + S.limit, S.tracksTotal);
  const empty = inbox
    ? 'Nothing new. Everything indexed has been looked at.'
    : 'No tracks match.';

  return renderOutcome() + renderSelectionBar(allShown) + `
    <div class="withside">
    <div class="withside-main">
    <table class="tbl" id="lib-table">
      <thead><tr>
        <th class="tick"><input type="checkbox" id="pick-all"
          title="Select everything on this page" ${allShown ? 'checked' : ''}></th>
        ${inbox
          ? '<th style="width:104px">Added</th>'
          : '<th class="tick" title="On the device selected above">ON</th>'}
        <th class="num" style="width:34px">#</th>
        <th style="width:26%">Name</th>
        <th class="num" style="width:48px">Time</th>
        <th style="width:19%">Artist</th>
        <th style="width:19%">Album</th>
        <th style="width:120px" title="Enrichment state">Metadata</th>
        <th style="width:82px">Format</th>
        ${inbox ? '' : `<th style="width:96px"
          title="When the catalog first saw this file. Hover a cell for the exact time and the file's own modified date."
          >Added</th>`}
        <th style="width:104px">ISRC</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="${inbox ? 10 : 11}" class="empty">${h(empty)}</td></tr>`}</tbody>
    </table>
    <div class="footnote hstack">
      <span>${S.tracksTotal ? `SHOWING ${num(from)}–${num(to)} OF ${num(S.tracksTotal)}` : 'NOTHING TO SHOW'}</span>
      <span style="flex:1"></span>
      <span style="text-transform:none;letter-spacing:0">click select · shift+click range ·
        ctrl+click add · ctrl+A page · E enrich · A accept ·
        R reject · S skip · U raw · Enter edit · Esc clear</span>
      <span style="flex:1"></span>
      <button class="btn sm" data-page="-1" ${S.offset === 0 ? 'disabled' : ''}>Previous</button>
      <button class="btn sm" data-page="1" ${to >= S.tracksTotal ? 'disabled' : ''}>Next</button>
    </div>
    </div>
    ${renderInspector()}
    </div>`;
}

/* What one track is, beside the table. Opened by clicking a row, because
   that is the gesture that already means "this one" - and a library row is
   nine columns of the fields that fit, which is never the artwork, the
   path, or where the values came from. */
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

  return `<aside class="inspector">
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
        ${line('year', value('year'))}
        ${line('track', [value('track_no'), value('disc_no')
          ? 'disc ' + value('disc_no') : ''].filter(Boolean).join(' \u00b7 '))}
        ${line('length', dur(d.duration))}
        ${line('format', [(row.ext || '').replace('.', '').toUpperCase(),
          row.bitrate ? Math.round(row.bitrate / 1000) + ' kbps' : '',
          bytes(row.size)].filter(Boolean).join(' \u00b7 '))}
        ${line('isrc', value('isrc'))}
        ${line('added', when(row.added_at))}
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
      <div class="hstack" style="margin-top:8px">
        <button class="btn sm" data-sel-act="edit">Edit metadata</button>
        <button class="btn sm" data-sel-act="enrich">Identify again</button>
        <span class="grow" style="flex:1"></span>
        <button class="btn sm" data-close-inspector="1" title="Close">&times;</button>
      </div>
    </div>
  </aside>`;
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
      <span class="faint mono" style="font-size:9px;letter-spacing:.1em">NEW SINCE LAST EMPTIED</span>
      <strong style="font-size:11px">${num(n)} track${n === 1 ? '' : 's'}</strong>
      <span class="faint" style="font-size:10px">Downloads and scanned files
        land here first. Identification runs on them automatically; this is
        where you check what it did.</span>
      <span class="grow" style="flex:1"></span>
      <span style="position:relative">
        <input id="q" placeholder="Search title, artist, album" value="${h(S.filter.q)}"
          style="width:230px;border:1px solid var(--line-2);border-radius:3px;padding:2px 6px 2px 22px">
        <svg width="11" height="11" style="position:absolute;left:6px;top:5px;color:var(--faint)"><use href="#i-search"/></svg>
      </span>
      <button class="btn" data-inbox-seen="1" ${n ? '' : 'disabled'}
        title="Stop treating everything indexed so far as new. Nothing is deleted.">Mark all as seen</button>
    </div>
    ${trackTable({ inbox: true })}`;
}

// A finished job, said out loud. Errors are the point: a run that failed
// used to report `0 enriched` and leave the reason in a counter nobody sees.
function fingerprintReady() {
  return !!(S.stats && S.stats.fingerprint && S.stats.fingerprint.ready);
}

function renderOutcome() {
  const o = S.outcome;
  if (!o) return '';
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
    ${btn('enrich', 'Enrich', 'E', 'primary')}
    ${btn('accept', 'Accept', 'A', '', offPage > 0 || has('awaiting'))}
    ${btn('reject', 'Reject', 'R', '', offPage > 0 || has('awaiting'))}
    ${btn('skip', 'Skip', 'S')}
    ${btn('raw', 'Mark raw', 'U')}
    ${btn('edit', 'Edit metadata', '\u21b5')}
    ${btn('write', 'Write tags to files', 'W', 'danger')}
  </div>`;
}

// -------------------------------------------------------------- playlist

function renderPlaylist() {
  const d = S.playlistDetail;
  if (!d) return '<div class="empty">Select a playlist.</div>';
  const pl = d.playlist;
  const rows = d.entries.map(e => `
    <tr>
      <td class="num">${e.pos + 1}</td>
      <td class="clip">${h(e.title || (e.title_hint || '').split(' - ').slice(1).join(' - ') || e.title_hint || '')}</td>
      <td class="num">${dur(e.duration)}</td>
      <td class="clip">${h(e.artist || (e.title_hint || '').split(' - ')[0] || '')}</td>
      <td class="mono">${h((e.ext || '').replace('.', '').toUpperCase())}</td>
      <td>${e.track_id
        ? '<span class="tag ok">matched</span>'
        : '<span class="tag bad">unmatched</span>'}</td>
      <td class="mono clip faint" title="${h(e.source_uri || '')}">${h(e.source_uri || '')}</td>
    </tr>`).join('');
  const unmatched = d.entries.filter(e => !e.track_id).length;

  return `<div class="pad stack">
    <div class="card">
      <header>
        <h3>${h(pl.name)}</h3>
        <span class="tag">${h(pl.origin || 'local')}</span>
        <span class="muted">${num(d.entries.length)} entries \u00b7
          ${num(d.entries.length - unmatched)} matched
          ${unmatched ? `\u00b7 <b style="color:var(--bad)">${num(unmatched)} unmatched</b>` : ''}</span>
        <span style="flex:1"></span>
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
    </div>
    <div class="card">
      <table class="tbl">
        <thead><tr><th class="num">#</th><th>Name</th><th class="num">Time</th>
          <th>Artist</th><th>Format</th><th>Match</th><th>Source URI</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="footnote">ORDER AS PUBLISHED BY SOURCE</div>
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

function renderImport() {
  const st = S.stats || {};
  const job = S.job;
  return `<div class="pad stack">
    <div class="card">
      <header><h3>Library folders</h3></header>
      <div class="in stack">
        ${(st.root_detail || []).map(r => `<div class="hstack">
          <span class="mono clip" style="flex:1;${r.hidden
            ? 'color:var(--fainter);text-decoration:line-through' : ''}"
            title="${h(r.root)}">${h(r.root)}</span>
          ${r.hidden ? '<span class="tag">hidden</span>' : ''}
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
    </div>
  </div>`;
}

function renderDownload() {
  const d = S.dl;
  if (!d) return '<div class="empty"><span class="spin"></span></div>';
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
        </div></div>` : ''}

        ${job ? `<div>
          <div class="hstack"><span class="${job.state === 'running' ? 'spin' : ''}"></span>
            <span class="muted clip">${h(job.detail || job.state)}</span></div>
          <div class="bar" style="margin-top:5px"><i style="width:${
            job.total ? (100 * job.done / job.total) : 0}%"></i></div>
          ${job.state === 'failed' ? `<div class="notice bad" style="margin-top:8px">
            ${icon('i-warn')}<div>${h(job.error)}</div></div>` : ''}
        </div>` : ''}

        ${res && !failed ? (res.downloaded ? `<div class="notice ok">${icon('i-check')}<div>
            ${num(res.downloaded)} downloaded into
            <span class="mono pick">${h(res.root)}</span> -
            ${num(res.added)} added to the catalog, ${num(res.updated)} updated.
            ${res.playlist ? `Playlist <strong>${h(res.playlist.name)}</strong>:
              ${num(res.playlist.added)} added${res.playlist.skipped
                ? ', ' + num(res.playlist.skipped) + ' already there' : ''}.` : ''}
          </div></div>
          <table class="tbl"><tbody>${res.files.map(f => `<tr>
            <td class="mono clip pick" title="${h(f)}">${h(f.slice(res.root.length + 1))}</td>
          </tr>`).join('')}</tbody></table>`
        : `<div class="notice">${icon('i-warn')}<div>Nothing new - every URL
            was already in the download archive, or nothing could be fetched.
            The log below says which.</div></div>`) : ''}
        ${res && res.errors && res.errors.length ? `<div class="notice bad">
          ${icon('i-warn')}<div class="pick">${res.errors.map(e => h(e)).join('<br>')}</div></div>` : ''}
      </div>
    </div>

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
            bundledTool(r.name) && r.path === norm(bundledTool(r.name))
              ? ' <span class="tag">bundled</span>' : ''}</td>
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
  const clean = !p.empty_total && !p.untagged_total && !p.unmatched_total;
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

// ---------------------------------------------------------------- render

const TITLES = {
  library: () => `MUSIC \u2014 ${num(S.tracksTotal)} TRACKS`,
  inbox: () => `INBOX \u2014 ${num(S.tracksTotal)} NEW`,
  device: () => {
    const d = S.devices.find(x => x.id === S.deviceId);
    return d ? (d.name + ' \u2014 SYNC PLAN').toUpperCase() : 'DEVICE';
  },
  playlist: () => S.playlistDetail
    ? ('PLAYLIST \u2014 ' + S.playlistDetail.playlist.name).toUpperCase() : 'PLAYLIST',
  addDevice: () => 'ADD DEVICE',
  import: () => 'SCAN & IMPORT',
  download: () => 'DOWNLOAD',
  problems: () => 'NEEDS ATTENTION',
};

function renderStatus() {
  const job = S.job;
  const meter = $('#meter');
  if (job && job.state === 'running') {
    meter.classList.remove('idle');
    const VERB = {
      sync: 'Syncing \u2192 ', download: 'Downloading ',
      enrich: 'Identifying ', 'write-tags': 'Writing tags to ',
      scan: 'Scanning ',
    };
    $('#status-label').textContent =
      (VERB[job.kind] || 'Working on ') + job.label;
    $('#status-meta').textContent = job.detail || '';
    $('#status-bar').style.width =
      (job.total ? (100 * job.done / job.total) : 0) + '%';
  } else {
    meter.classList.add('idle');
    const st = S.stats || {};
    const connected = S.devices.filter(d => d.mounted_at).length;
    $('#status-label').textContent = job && job.state === 'failed'
      ? 'Last job failed' : 'Ready';
    $('#status-meta').textContent =
      `${num(st.tracks)} tracks \u00b7 ${num(st.playlists)} playlists \u00b7 ` +
      `${S.devices.length} devices, ${connected} connected`;
    $('#status-bar').style.width = '0%';
  }
}

function render() {
  if (isTrackView()) syncInspector();
  renderSidebar();
  $('#titlebar').innerHTML = `<span>${h((TITLES[S.view] || (() => S.view))())}</span>
    <span class="grow"></span>
    ${S.error ? `<span style="color:var(--bad);text-transform:none;font-family:var(--sans);font-size:11px">${h(S.error)}</span>` : ''}`;
  const body = {
    library: renderLibrary, inbox: renderInbox,
    device: renderDevice, playlist: renderPlaylist,
    addDevice: renderAddDevice, import: renderImport, problems: renderProblems,
    download: renderDownload,
  }[S.view];
  $('#pane').innerHTML = body ? body() : '';
  renderStatus();
  if (S.view === 'addDevice') loadVolumes();
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
  if (act === 'skip' || act === 'raw') {
    closeModal();
    // Filing, not a verdict: the rows stay picked, so changing your mind is
    // one more keystroke rather than another hunt through the table.
    return guard(async () => {
      await api('/enrich/state', {
        method: 'POST',
        body: JSON.stringify({ content_keys: keys,
                               state: act === 'skip' ? 'skipped' : 'raw' }),
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
  ['disc_no', 'Disc no'], ['year', 'Year'], ['isrc', 'ISRC'],
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
  year: 'Year', isrc: 'ISRC',
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
    + '[data-close-inspector],'
    + '[data-root-hide],[data-root-remove],[data-root-forget],'
    + '[data-plsync],[data-playlist],[data-track]');
  if (!t) return;

  const d = t.dataset;
  if (d.scrim && ev.target !== t) return;

  if (d.close || d.scrim) return closeModal();

  if (d.act === 'view') {
    S.view = d.arg;
    if (d.arg === 'library' || d.arg === 'inbox') {
      // The two views share the table, and the offset and selection belong
      // to the question that was asked, not to the one being left.
      S.offset = 0; S.sel.clear(); S.anchor = null;
      return guard(loadLibrary);
    }
    if (d.arg === 'problems') {
      S.problems = null;
      return guard(async () => { S.problems = await api('/problems'); });
    }
    if (d.arg === 'download') {
      S.dlProbe = null;
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
  if (d.act === 'playlist') {
    S.view = 'playlist';
    return guard(() => loadPlaylist(+d.arg));
  }
  if (d.playlist) {
    S.view = 'playlist';
    return guard(() => loadPlaylist(+d.playlist));
  }
  if (d.facet !== undefined) {
    S.filter[d.facet] = d.value;
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
      await loadPlaylist(S.playlistId);
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

// A scan and a download each hand back the id of the identification pass they
// started. Picking it up is what keeps that pass visible: it is the longer of
// the two jobs by far, and an hour of work with no progress bar reads as
// nothing happening.
function followAutoEnrich(job, label) {
  const id = job && job.result && job.result.enrich_job;
  if (!id) return;
  S.outcome = null;
  S.job = { id, kind: 'enrich', state: 'running', done: 0, total: 0,
            label: label || 'new files' };
  watchJob(id, async (done) => {
    S.outcome = done;
    await loadCore();
    if (S.view === 'library') await loadLibrary();
  });
  render();
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

let filterTimer = null;
document.addEventListener('input', (ev) => {
  const el = ev.target;
  if (el.name === 'urls' && el.form && el.form.id === 'dl-form') {
    // Kept on every keystroke rather than on submit: the list is worth most
    // exactly when the download failed and has to be tried again.
    return setDownloadUrls(el.value);
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
  if (!roots.length) { S.view = 'import'; return render(); }
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
  if (key === 'w') { ev.preventDefault(); return selAction('write'); }
  if (ev.key === 'Enter') { ev.preventDefault(); return selAction('edit'); }
});

// ------------------------------------------------------------------ boot

(async function boot() {
  try {
    await loadCore();
    await loadLibrary();
    if (!S.stats.tracks) S.view = 'import';
  } catch (e) {
    S.error = e.message;
  }
  render();
})();
