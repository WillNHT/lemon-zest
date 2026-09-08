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
  stats: null,
  devices: [],
  playlists: [],
  deviceId: null,        // device being viewed
  scopeId: null,         // device the library's tick column edits
  playlistId: null,
  facets: { genre: [], artist: [], album: [] },
  filter: { q: '', genre: '', artist: '', album: '' },
  paneFilter: { genre: '', artist: '', album: '' },
  navFilter: { device: '', playlist: '' },
  tracks: [], tracksTotal: 0, offset: 0, limit: 200,
  playlistDetail: null,
  problems: null,
  plan: null, planning: false, planError: null,
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

async function loadLibrary() {
  const p = new URLSearchParams();
  for (const k of ['q', 'genre', 'artist', 'album']) {
    if (S.filter[k]) p.set(k, S.filter[k]);
  }
  p.set('limit', S.limit); p.set('offset', S.offset);
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
               icon: 'i-clock', on: S.view === 'import' }));
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

function renderLibrary() {
  const scope = S.devices.find(d => d.id === S.scopeId);
  const rows = S.tracks.map(t => `
    <tr data-track="${t.id}">
      <td class="tick">${scope
        ? `<span title="${t.on_device ? 'on ' + h(scope.name) : 'not on ' + h(scope.name)}"
             style="color:${t.on_device ? 'var(--accent)' : 'var(--line-2)'}">
             ${t.on_device ? '&#9679;' : '&#9675;'}</span>`
        : ''}</td>
      <td class="num">${t.track_no || ''}</td>
      <td class="clip" title="${h(t.path)}">${h(t.title || (t.rel_path || '').split('/').pop())}
        ${t.empty ? '<span class="tag bad">empty</span>' : ''}
        ${!t.title && !t.empty ? '<span class="tag warn">untagged</span>' : ''}</td>
      <td class="num">${dur(t.duration)}</td>
      <td class="clip">${h(t.artist || '')}</td>
      <td class="clip">${h(t.album || '')}</td>
      <td class="mono">${h((t.ext || '').replace('.', '').toUpperCase())}
        ${t.bitrate ? Math.round(t.bitrate / 1000) : ''}</td>
      <td class="mono">${h(t.isrc || '')}</td>
    </tr>`).join('');

  const from = S.offset + 1, to = Math.min(S.offset + S.limit, S.tracksTotal);
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
    <table class="tbl">
      <thead><tr>
        <th class="tick" title="On the device selected above">ON</th>
        <th class="num" style="width:34px">#</th>
        <th style="width:30%">Name</th>
        <th class="num" style="width:48px">Time</th>
        <th style="width:22%">Artist</th>
        <th style="width:22%">Album</th>
        <th style="width:88px">Format</th>
        <th style="width:104px">ISRC</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="empty">No tracks match.</td></tr>'}</tbody>
    </table>
    <div class="footnote hstack">
      <span>${S.tracksTotal ? `SHOWING ${num(from)}\u2013${num(to)} OF ${num(S.tracksTotal)}` : 'NOTHING TO SHOW'}</span>
      <span style="flex:1"></span>
      <button class="btn sm" data-page="-1" ${S.offset === 0 ? 'disabled' : ''}>Previous</button>
      <button class="btn sm" data-page="1" ${to >= S.tracksTotal ? 'disabled' : ''}>Next</button>
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
        ${(st.roots || []).map(r => `<div class="hstack">
          <span class="mono clip" style="flex:1">${h(r)}</span>
          <button class="btn sm" data-scan="${h(r)}">Rescan</button></div>`).join('')
          || '<span class="faint">No folders indexed yet.</span>'}
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

// ---------------------------------------------------------------- render

const TITLES = {
  library: () => `MUSIC \u2014 ${num(S.tracksTotal)} TRACKS`,
  device: () => {
    const d = S.devices.find(x => x.id === S.deviceId);
    return d ? (d.name + ' \u2014 SYNC PLAN').toUpperCase() : 'DEVICE';
  },
  playlist: () => S.playlistDetail
    ? ('PLAYLIST \u2014 ' + S.playlistDetail.playlist.name).toUpperCase() : 'PLAYLIST',
  addDevice: () => 'ADD DEVICE',
  import: () => 'SCAN & IMPORT',
  problems: () => 'NEEDS ATTENTION',
};

function renderStatus() {
  const job = S.job;
  const meter = $('#meter');
  if (job && job.state === 'running') {
    meter.classList.remove('idle');
    $('#status-label').textContent =
      job.kind === 'sync' ? 'Syncing \u2192 ' + job.label : 'Scanning ' + job.label;
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
  renderSidebar();
  $('#titlebar').innerHTML = `<span>${h((TITLES[S.view] || (() => S.view))())}</span>
    <span class="grow"></span>
    ${S.error ? `<span style="color:var(--bad);text-transform:none;font-family:var(--sans);font-size:11px">${h(S.error)}</span>` : ''}`;
  const body = {
    library: renderLibrary, device: renderDevice, playlist: renderPlaylist,
    addDevice: renderAddDevice, import: renderImport, problems: renderProblems,
  }[S.view];
  $('#pane').innerHTML = body ? body() : '';
  renderStatus();
  if (S.view === 'addDevice') loadVolumes();
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

// ---------------------------------------------------------------- events

async function guard(fn) {
  S.error = null;
  try { await fn(); } catch (e) { S.error = e.message; }
  render();
}

document.addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-act],[data-facet],[data-scope],[data-page],'
    + '[data-plan],[data-sync],[data-unset],[data-pick-set],[data-save-set],'
    + '[data-detect-pl],'
    + '[data-close],[data-scrim],[data-usevol],[data-scan],[data-volumes],'
    + '[data-plsync],[data-playlist],[data-track]');
  if (!t) return;

  const d = t.dataset;
  if (d.scrim && ev.target !== t) return;

  if (d.close || d.scrim) return closeModal();

  if (d.act === 'view') {
    S.view = d.arg;
    if (d.arg === 'library') return guard(loadLibrary);
    if (d.arg === 'problems') {
      S.problems = null;
      return guard(async () => { S.problems = await api('/problems'); });
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
    watchJob(res.job, async () => {
      await loadCore();
      if (S.view === 'library') await loadLibrary();
    });
  });
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

let filterTimer = null;
document.addEventListener('input', (ev) => {
  const el = ev.target;
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
  if (ev.key === 'Escape') closeModal();
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
