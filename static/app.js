/* VGTracker front end - plain JavaScript, no build step. */

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const YEAR = String(new Date().getFullYear());
const STATUS = [
  ['playing', 'Playing'], ['completed', 'Completed'], ['planning', 'Planning'], ['paused', 'Paused'], ['dropped', 'Dropped'],
];
const STATUS_LABEL = { playing: 'Playing', completed: 'Completed', planning: 'Planning', paused: 'Paused', dropped: 'Dropped', ignored: 'Hidden', '': 'Library' };
const srcLabel = s => ({ steam: 'Steam', psn: 'PlayStation', xbox: 'Xbox', manual: 'Logged by you', import: 'Imported' }[s] || s);
const hasAccounts = () => S.settings.has_steam_key || S.settings.has_psn || S.settings.has_xbox;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function hrs(min, short = false) {
  if (min == null) return '—';
  if (min < 60) return `${Math.round(min)}m`;
  const h = min / 60;
  if (short || h >= 100) return `${Math.round(h)}h`;
  return `${Math.floor(h)}h${min % 60 ? ' ' + Math.round(min % 60) + 'm' : ''}`;
}
const hrsNum = min => (min / 60).toFixed(min >= 6000 ? 0 : 1).replace(/\.0$/, '');
const fmtDate = (s, o = { day: 'numeric', month: 'short', year: 'numeric' }) => s ? new Date(s.length <= 10 ? s + 'T00:00:00' : s).toLocaleDateString('en-GB', o) : '—';
const ago = s => {
  if (!s) return '';
  const d = Math.floor((Date.now() - new Date(s.length <= 10 ? s + 'T12:00:00' : s)) / 86400000);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 30 ? `${d} days ago` : d < 365 ? `${Math.round(d / 30)} months ago` : fmtDate(s, { month: 'short', year: 'numeric' });
};
const metaCls = m => m >= 75 ? 'hi' : m >= 50 ? 'mid' : 'lo';
const metaBadge = m => m ? `<span class="meta ${metaCls(m)}" title="Metascore">${m}</span>` : '';
const stars = r => { if (r == null) return ''; const s = r / 2; return '★'.repeat(Math.floor(s)) + (s % 1 >= .5 ? '⯪' : '') + '☆'.repeat(5 - Math.ceil(s)); };

let S = null;
let view = location.hash.slice(1) || 'dashboard';
let lib = { q: '', status: 'tracked', platform: '', year: '', sort: 'recent' };
let roundYear = null;
let wizStep = 0;
let searchTimer = null;
let lastSearch = { q: '', results: [], provider: '' };

/* ------------------------------------------------------------------ API */

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  if (!r.ok) { const t = (await r.text()).replace(/<[^>]+>/g, '').trim(); throw new Error(t.slice(0, 220) || r.statusText); }
  return r.json();
}
async function load() { S = await api('GET', '/api/state'); render(); }
async function refresh() { S = await api('GET', '/api/state'); }

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 3200);
}
async function guarded(fn) { try { return await fn(); } catch (e) { if (e.message !== 'login') toast(e.message); } }

/* ------------------------------------------------------------------ helpers */

const game = id => S.games.find(g => g.id === id);
const playedIn = (g, y) => !!(g.year_minutes[y] || (g.last_played || '').startsWith(y) || (g.first_played || '').startsWith(y)
  || (g.started || '').startsWith(y) || (g.completed_date || '').startsWith(y));
const tracked = g => g.status && g.status !== 'ignored';
const pct = g => g.hltb_main && g.minutes ? Math.min(100, g.minutes / 60 / g.hltb_main * 100) : 0;

function coverImg(g, inner = '') {
  const src = g.cover || g.banner || '';
  return `<div class="cover">${src ? `<img src="${esc(src)}" alt="" loading="lazy" onerror="imgFail(this)" data-alt="${esc(g.banner && g.banner !== src ? g.banner : '')}">` : ''}
    <div class="ph" ${src ? 'hidden' : ''}>${esc(g.title)}</div>${inner}</div>`;
}
// Older Steam games have no portrait art - fall back to the landscape header, then to a title card
window.imgFail = el => {
  const alt = el.dataset.alt;
  if (alt && el.src !== alt) { el.src = alt; el.dataset.alt = ''; return; }
  const ph = el.parentElement?.querySelector('.ph');
  if (ph) { ph.hidden = false; el.remove(); } else el.style.visibility = 'hidden';
};

function tile(g, opts = {}) {
  const y = opts.year;
  const time = y ? g.year_minutes[y] : g.minutes;
  const p = pct(g);
  return `<div class="tile" data-game="${g.id}">${opts.n ? `<span class="n">${opts.n}</span>` : ''}
    ${coverImg(g, `<div class="badges">${g.status ? `<span class="pill ${g.status}">${STATUS_LABEL[g.status]}</span>` : '<span></span>'}${metaBadge(g.metascore)}</div>
    ${p && g.status === 'playing' ? `<div class="bar"><div style="width:${p}%"></div></div>` : ''}`)}
    <div class="t">${esc(g.title)}</div>
    <div class="s"><span>${time ? hrs(time) : (y && playedIn(g, y) ? 'played' : '')}${opts.extra ? ' · ' + opts.extra : ''}</span>${g.rating != null ? `<span class="score">★ ${g.rating}</span>` : ''}</div></div>`;
}

/* ------------------------------------------------------------------ routing */

function render() {
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  const v = $('#view');
  if (S.settings.setup_done !== '1') { $('#nav').style.visibility = 'hidden'; $('#sync-btn').hidden = true; v.innerHTML = renderWizard(); bindWizard(); return; }
  $('#nav').style.visibility = ''; $('#sync-btn').hidden = !hasAccounts();
  const fn = { dashboard: renderDashboard, library: renderLibrary, add: renderAdd, year: renderYear, settings: renderSettings }[view] || renderDashboard;
  v.innerHTML = fn();
  bind(v);
}

function bind(root) {
  $$('[data-game]', root).forEach(el => el.onclick = () => openGame(+el.dataset.game));
  $$('[data-go]', root).forEach(el => el.onclick = () => {
    if (el.dataset.lib) lib.status = el.dataset.lib;
    if (el.dataset.year) roundYear = el.dataset.year;
    go(el.dataset.go);
  });
  if (view === 'library') bindLibrary();
  if (view === 'add') bindAdd();
  if (view === 'year') loadYear();
  if (view === 'settings') bindSettings();
}
function go(v) { view = v; location.hash = v; render(); window.scrollTo(0, 0); }
window.addEventListener('hashchange', () => { const v = location.hash.slice(1); if (v && v !== view) { view = v; render(); } });
$$('#nav a').forEach(a => a.onclick = () => go(a.dataset.view));
$('#sync-btn').onclick = () => syncNow();

async function syncNow(btn = $('#sync-btn')) {
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spin">⟳</span> Syncing…';
  try { const r = await api('POST', '/api/sync'); toast(r.message); await load(); }
  catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.innerHTML = old; }
}

/* ------------------------------------------------------------------ dashboard */

function renderDashboard() {
  const gs = S.games.filter(g => g.status !== 'ignored');
  const yearGames = gs.filter(g => playedIn(g, YEAR));
  const yMin = gs.reduce((a, g) => a + (g.year_minutes[YEAR] || 0), 0);
  const done = gs.filter(g => (g.completed_date || '').startsWith(YEAR));
  const rated = yearGames.filter(g => g.rating != null);
  const avg = rated.length ? (rated.reduce((a, g) => a + g.rating, 0) / rated.length).toFixed(1) : '—';
  const playing = gs.filter(g => g.status === 'playing').sort((a, b) => (b.last_played || '').localeCompare(a.last_played || ''));
  const planning = gs.filter(g => g.status === 'planning').sort((a, b) => (b.added || '').localeCompare(a.added || '')).slice(0, 12);
  const recentDone = gs.filter(g => g.status === 'completed').sort((a, b) => (b.completed_date || '').localeCompare(a.completed_date || '')).slice(0, 6);
  const month = new Date().getMonth();
  let banner = '';
  if (month === 11) banner = `<div class="banner" data-go="year" data-year="${YEAR}"><span class="big">🏆</span><div><b>Your ${YEAR} round-up is ready</b><div class="muted">${yearGames.length} games, ${hrs(yMin, true)} played. See how your year went.</div></div><span class="spacer"></span><span class="btn primary">View round-up</span></div>`;
  else if (month === 0) banner = `<div class="banner" data-go="year" data-year="${+YEAR - 1}"><span class="big">🏆</span><div><b>Your ${+YEAR - 1} round-up</b><div class="muted">Look back at last year's games.</div></div><span class="spacer"></span><span class="btn primary">View round-up</span></div>`;

  // recent activity grouped by day
  const days = {};
  for (const p of S.recent) (days[p.day] ||= []).push(p);
  const activity = Object.entries(days).slice(0, 8).map(([d, ps]) => `<li><div class="grow"><b>${fmtDate(d, { weekday: 'short', day: 'numeric', month: 'short' })}</b>
    <small>${Object.entries(ps.reduce((o, p) => ((o[p.title] = (o[p.title] || 0) + p.minutes), o), {})).map(([t, mn]) => `${esc(t)} (${hrs(mn)})`).join(' · ')}</small></div><span class="muted">${hrs(ps.reduce((a, p) => a + p.minutes, 0))}</span></li>`).join('');

  return `<div class="container">
    ${banner}
    <div class="page-head"><div><h1>${S.settings.display_name ? `Hey ${esc(S.settings.display_name)}` : 'Your games'}</h1>
      <div class="muted">${YEAR} so far${S.settings.last_sync ? ` · last synced ${ago(S.settings.last_sync)}` : ''}${S.enrich_pending ? ` · fetching details for ${S.enrich_pending} games…` : ''}</div></div>
      <button class="btn primary" data-go="add">＋ Add game</button></div>
    <div class="stats">
      <div class="stat"><span>Hours in ${YEAR}</span><b>${hrsNum(yMin)}</b><small>tracked playtime</small></div>
      <div class="stat"><span>Games played</span><b>${yearGames.length}</b><small>this year</small></div>
      <div class="stat"><span>Completed</span><b>${done.length}</b><small>this year</small></div>
      <div class="stat"><span>Average rating</span><b>${avg}</b><small>${rated.length} rated this year</small></div>
    </div>

    <div class="section"><h2>Now playing</h2><a class="small" href="#library" data-go="library" data-lib="playing">See all</a></div>
    ${playing.length ? `<div class="now">${playing.slice(0, 6).map(nowCard).join('')}</div>`
      : `<div class="empty">Nothing marked as playing. Games you play on Steam, PlayStation or Xbox show up here automatically after a sync.</div>`}

    <div class="grid2" style="margin-top:1.6rem">
      <div class="card"><h2>Recent activity</h2>${activity ? `<ul class="list">${activity}</ul>` : '<p class="muted">Playtime picked up by syncs and sessions you log appears here.</p>'}</div>
      <div class="card"><h2>Recently completed</h2>${recentDone.length ? `<ul class="list">${recentDone.map(g => `<li data-game="${g.id}" style="cursor:pointer">
        <img class="thumb" src="${esc(g.cover || g.banner)}" alt="" onerror="this.style.visibility='hidden'"><div class="grow"><b>${esc(g.title)}</b><small>${fmtDate(g.completed_date)} · ${hrs(g.minutes)}</small></div>
        ${g.rating != null ? `<span class="score">★ ${g.rating}</span>` : ''}</li>`).join('')}</ul>` : '<p class="muted">Mark a game as completed to see it here.</p>'}</div>
    </div>

    <div class="section"><h2>Up next</h2><a class="small" href="#library" data-go="library" data-lib="planning">Planning (${gs.filter(g => g.status === 'planning').length})</a></div>
    ${planning.length ? `<div class="grid">${planning.map(g => tile(g)).join('')}</div>` : `<div class="empty">Add games you want to play with the status <b>Planning</b>.</div>`}
  </div>`;
}

function nowCard(g) {
  const p = pct(g);
  return `<div class="np" data-game="${g.id}"><div class="bg" style="background-image:url('${esc(g.banner || g.cover)}')"></div>
    ${coverImg(g)}<div class="info"><h3>${esc(g.title)}</h3>
    <div class="row small"><span>${esc(g.platforms || '')}</span>${g.last_played ? `<span>· ${ago(g.last_played)}</span>` : ''}</div>
    <div class="row" style="gap:.9rem"><span><b>${hrs(g.minutes)}</b> <span class="small">total</span></span>${g.year_minutes[YEAR] && g.year_minutes[YEAR] !== g.minutes ? `<span><b>${hrs(g.year_minutes[YEAR])}</b> <span class="small">this year</span></span>` : ''}</div>
    ${g.hltb_main ? `<div class="small">${Math.round(p)}% of the ${g.hltb_main}h main story (HLTB)</div><div class="prog"><div style="width:${p}%"></div></div>` : ''}
    </div></div>`;
}

/* ------------------------------------------------------------------ library */

function libGames() {
  let gs = S.games;
  const st = lib.status;
  if (st === 'tracked') gs = gs.filter(tracked);
  else if (st === 'all') gs = gs.filter(g => g.status !== 'ignored');
  else gs = gs.filter(g => (g.status || '') === (st === 'library' ? '' : st));
  if (lib.platform) gs = gs.filter(g => (g.platforms || '').split(',').map(s => s.trim()).includes(lib.platform));
  if (lib.year) gs = gs.filter(g => playedIn(g, lib.year));
  if (lib.q) { const q = lib.q.toLowerCase(); gs = gs.filter(g => g.title.toLowerCase().includes(q)); }
  const by = {
    recent: (a, b) => (b.last_played || b.updated || '').localeCompare(a.last_played || a.updated || ''),
    title: (a, b) => a.title.localeCompare(b.title),
    hours: (a, b) => (lib.year ? (b.year_minutes[lib.year] || 0) - (a.year_minutes[lib.year] || 0) : b.minutes - a.minutes),
    rating: (a, b) => (b.rating ?? -1) - (a.rating ?? -1),
    metascore: (a, b) => (b.metascore ?? -1) - (a.metascore ?? -1),
    added: (a, b) => (b.added || '').localeCompare(a.added || ''),
    hltb: (a, b) => (a.hltb_main ?? 1e9) - (b.hltb_main ?? 1e9),
  }[lib.sort];
  return [...gs].sort(by);
}

function renderLibrary() {
  const count = st => S.games.filter(g => st === 'tracked' ? tracked(g) : st === 'all' ? g.status !== 'ignored' : (g.status || '') === (st === 'library' ? '' : st)).length;
  const chips = [['tracked', 'Tracked'], ...STATUS, ['library', 'Unsorted'], ['all', 'Everything'], ['ignored', 'Hidden']];
  const plats = [...new Set(S.games.flatMap(g => (g.platforms || '').split(',').map(s => s.trim())).filter(Boolean))].sort();
  const years = [...new Set(S.games.flatMap(g => [...Object.keys(g.year_minutes), (g.last_played || '').slice(0, 4), (g.completed_date || '').slice(0, 4)]).filter(Boolean))].sort().reverse();
  return `<div class="container">
    <div class="page-head"><h1>Library</h1><button class="btn primary" data-go="add">＋ Add game</button></div>
    <div class="toolbar">
      <input type="search" id="lib-q" placeholder="Search your games…" value="${esc(lib.q)}">
      <select id="lib-plat"><option value="">All platforms</option>${plats.map(p => `<option ${p === lib.platform ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select>
      <select id="lib-year"><option value="">Any year</option>${years.map(y => `<option value="${y}" ${y === lib.year ? 'selected' : ''}>Played in ${y}</option>`).join('')}</select>
      <select id="lib-sort">${[['recent', 'Last played'], ['title', 'Title'], ['hours', 'Hours played'], ['rating', 'My rating'], ['metascore', 'Metascore'], ['hltb', 'Shortest (HLTB)'], ['added', 'Recently added']]
        .map(([k, l]) => `<option value="${k}" ${k === lib.sort ? 'selected' : ''}>${l}</option>`).join('')}</select>
    </div>
    <div class="chips">${chips.map(([k, l]) => `<span class="chip ${lib.status === k ? 'on' : ''}" data-st="${k}">${l}<b>${count(k)}</b></span>`).join('')}</div>
    ${lib.status === 'library' ? '<div class="note">Games synced from your accounts that you haven\'t given a status yet. Played ones move to <b>Playing</b> automatically; give the rest a status, or hide them (tools, demos, soundtracks).</div>' : ''}
    <div id="lib-grid"></div></div>`;
}
function renderLibGrid() {
  const gs = libGames();
  const el = $('#lib-grid');
  const LIMIT = 300;
  el.innerHTML = gs.length ? `<div class="grid">${gs.slice(0, LIMIT).map(g => tile(g, { year: lib.year || null })).join('')}</div>
    ${gs.length > LIMIT ? `<p class="muted" style="text-align:center">Showing ${LIMIT} of ${gs.length}. Search or filter to narrow it down.</p>` : ''}`
    : `<div class="empty">No games here${lib.q ? ` matching “${esc(lib.q)}”. <a data-go="add" href="#add" onclick="lastSearch.q=lib.q">Search to add it?</a>` : '.'}</div>`;
  $$('[data-game]', el).forEach(t => t.onclick = () => openGame(+t.dataset.game));
}
function bindLibrary() {
  renderLibGrid();
  $('#lib-q').oninput = e => { lib.q = e.target.value; renderLibGrid(); };
  $('#lib-plat').onchange = e => { lib.platform = e.target.value; renderLibGrid(); };
  $('#lib-year').onchange = e => { lib.year = e.target.value; if (lib.year && lib.sort === 'recent') lib.sort = 'hours'; render(); };
  $('#lib-sort').onchange = e => { lib.sort = e.target.value; renderLibGrid(); };
  $$('[data-st]').forEach(c => c.onclick = () => { lib.status = c.dataset.st; render(); });
}

/* ------------------------------------------------------------------ add / search */

function renderAdd() {
  return `<div class="container">
    <div class="page-head"><div><h1>Add a game</h1><div class="muted">Search ${S.settings.has_rawg_key ? 'every platform (RAWG)' : 'the Steam store'} for cover art and details${S.settings.has_rawg_key ? '' : ' — add a free RAWG key in Settings to search console games too'}.</div></div>
      <button class="btn" id="add-manual">Add manually</button></div>
    <input type="search" id="q" class="big-search" placeholder="Search for a game…" value="${esc(lastSearch.q)}" autofocus>
    <div id="results" style="margin-top:1.2rem"></div></div>`;
}
function bindAdd() {
  const q = $('#q');
  q.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => doSearch(q.value.trim()), 380); };
  q.focus(); q.setSelectionRange(q.value.length, q.value.length);
  $('#add-manual').onclick = () => openManual(q.value.trim());
  if (lastSearch.q) showResults(); if (lastSearch.q && !lastSearch.results.length) doSearch(lastSearch.q);
}
async function doSearch(q) {
  lastSearch.q = q;
  if (q.length < 2) { lastSearch.results = []; showResults(); return; }
  $('#results').innerHTML = '<p class="muted"><span class="spin">⟳</span> Searching…</p>';
  try {
    const r = await api('GET', '/api/search?q=' + encodeURIComponent(q));
    if (lastSearch.q !== q) return;
    lastSearch.results = r.results; lastSearch.provider = r.provider; showResults();
  } catch (e) { $('#results').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function showResults() {
  const el = $('#results'); if (!el) return;
  const local = lastSearch.q.length >= 2 ? S.games.filter(g => g.status !== 'ignored' && g.title.toLowerCase().includes(lastSearch.q.toLowerCase())).slice(0, 6) : [];
  const owned = r => S.games.find(g => (r.steam_appid && g.steam_appid === r.steam_appid) || (r.rawg_id && g.rawg_id === r.rawg_id) || g.title.toLowerCase() === (r.title || '').toLowerCase());
  el.innerHTML = (local.length ? `<h3 style="margin-bottom:.6rem">Already in your library</h3><div class="grid" style="margin-bottom:1.6rem">${local.map(g => tile(g)).join('')}</div>` : '')
    + (lastSearch.results.length ? `<div class="results">${lastSearch.results.map((r, i) => {
      const have = owned(r);
      return `<div class="result"><img src="${esc(r.cover)}" alt="" loading="lazy" data-alt="${esc(r.banner !== r.cover ? r.banner : '')}" onerror="imgFail(this)">
      <div class="info"><b>${esc(r.title)}</b><div class="row small">${r.released ? `<span>${r.released.slice(0, 4)}</span>` : ''}${metaBadge(r.metascore)}</div>
      <div class="small" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.platforms || '')}</div>
      <div class="acts">${have && have.status && have.status !== 'ignored' ? `<button class="btn sm" data-game="${have.id}">In library · ${STATUS_LABEL[have.status]}</button>`
        : STATUS.filter(([k]) => k !== 'paused').map(([k, l]) => `<button class="btn sm ${k === 'planning' ? 'primary' : ''}" data-add="${i}" data-st="${k}">${l}</button>`).join('')}</div></div></div>`;
    }).join('')}</div><p class="small" style="margin-top:1rem">Results from ${esc(lastSearch.provider)}.</p>`
      : lastSearch.q.length >= 2 ? `<div class="empty">No matches. <a href="#" onclick="openManual(lastSearch.q);return false">Add “${esc(lastSearch.q)}” manually</a></div>` : '');
  $$('[data-add]', el).forEach(b => b.onclick = () => addResult(lastSearch.results[+b.dataset.add], b.dataset.st, b));
  $$('[data-game]', el).forEach(t => t.onclick = () => openGame(+t.dataset.game));
}
async function addResult(r, status, btn) {
  btn.disabled = true;
  await guarded(async () => {
    const res = await api('POST', '/api/games', { ...r, status, platforms: platformGuess(r) });
    await refresh(); showResults();
    toast(`${r.title} ${res.existed ? 'updated' : 'added'} as ${STATUS_LABEL[status]}`);
  });
  btn.disabled = false;
}
function platformGuess(r) {
  if (r.source === 'steam') return 'Steam';
  const p = (r.platforms || '').split(',').map(s => s.trim());
  return ['PlayStation 5', 'PC', 'Nintendo Switch', 'Xbox Series S/X'].find(x => p.includes(x))?.replace('PlayStation 5', 'PS5') || '';
}

function openManual(title = '') {
  const m = $('#modal');
  $('#modal-body').innerHTML = `<form class="card" style="margin:0;border:0" id="man">
    <h2>Add a game manually</h2>
    <label>Title</label><input name="title" required value="${esc(title)}">
    <div class="grid2"><div><label>Platform</label><input name="platforms" placeholder="e.g. Switch" list="platlist">
      <datalist id="platlist">${['Steam', 'PS5', 'PS4', 'Switch', 'Switch 2', 'Xbox', 'PC', 'Steam Deck', 'Mobile', 'Retro'].map(p => `<option>${p}</option>`).join('')}</datalist></div>
      <div><label>Status</label><select name="status">${STATUS.map(([k, l]) => `<option value="${k}" ${k === 'planning' ? 'selected' : ''}>${l}</option>`).join('')}</select></div></div>
    <label>Cover image URL (optional)</label><input name="cover" placeholder="https://…">
    <div class="row" style="margin-top:1.2rem;justify-content:flex-end"><button type="button" class="btn" onclick="$('#modal').close()">Cancel</button><button class="btn primary">Add</button></div></form>`;
  m.showModal();
  $('#man').onsubmit = e => {
    e.preventDefault();
    guarded(async () => {
      const d = Object.fromEntries(new FormData(e.target));
      const r = await api('POST', '/api/games', d);
      m.close(); await load(); openGame(r.id);
    });
  };
}

/* ------------------------------------------------------------------ game modal */

async function openGame(id) {
  const g = game(id); if (!g) return;
  const m = $('#modal');
  const sessions = await api('GET', `/api/games/${id}/sessions`).catch(() => []);
  const years = Object.entries(g.year_minutes).sort((a, b) => b[0] - a[0]);
  const p = pct(g);
  const hltbRow = g.hltb_main || g.hltb_extra || g.hltb_complete;
  const others = S.games.filter(x => x.id !== g.id && x.status !== 'ignored').sort((a, b) => a.title.localeCompare(b.title));
  $('#modal-body').innerHTML = `
    <div class="gm-hero" style="background-image:url('${esc(g.banner || g.cover)}')"></div>
    <button class="btn sm gm-close" onclick="$('#modal').close()">✕</button>
    <div class="gm">
      <div class="side">${coverImg(g)}<button class="btn sm" id="gm-cover-pick" style="width:100%;justify-content:center;margin-top:.6rem">Change cover</button>
        <div class="small" style="margin-top:.8rem">${g.sources.map(s => `<div>${srcLabel(s.source)} (${esc(s.platform)}): ${hrs(s.minutes)}${s.last_played ? `, last ${fmtDate(s.last_played)}` : ''}</div>`).join('')}</div>
        ${g.steam_appid ? `<a class="small" href="https://store.steampowered.com/app/${g.steam_appid}" target="_blank" rel="noopener">Steam store ↗</a><br>` : ''}
        ${g.hltb_id ? `<a class="small" href="https://howlongtobeat.com/game/${esc(g.hltb_id)}" target="_blank" rel="noopener">HowLongToBeat ↗</a>` : ''}
      </div>
      <div>
        <h2>${esc(g.title)}</h2>
        <div class="row small" style="margin:.3rem 0 1rem">${esc(g.platforms || 'No platform')}${g.released ? ` · ${g.released.slice(0, 4)}` : ''}${g.genres ? ` · ${esc(g.genres)}` : ''}</div>
        <div class="seg" id="gm-status">${STATUS.map(([k, l]) => `<button class="${k} ${g.status === k ? 'on' : ''}" data-s="${k}">${l}</button>`).join('')}</div>
        <div class="rating" style="margin-top:1rem"><b id="gm-rv">${g.rating ?? '–'}</b><div style="flex:1"><input type="range" id="gm-rating" min="0" max="10" step="0.5" value="${g.rating ?? 5}">
          <div class="row small"><span class="stars" id="gm-stars">${stars(g.rating) || 'Not rated'}</span><span class="spacer"></span>${g.rating != null ? '<a href="#" id="gm-unrate">Clear rating</a>' : ''}</div></div></div>
        <div class="facts">
          <div class="fact"><span>Played</span><b>${hrs(g.minutes)}</b></div>
          <div class="fact"><span>Metascore</span><b>${g.metascore ? metaBadge(g.metascore) : '—'}</b></div>
          ${hltbRow ? `<div class="fact"><span>HLTB main</span><b>${g.hltb_main ? g.hltb_main + 'h' : '—'}</b></div>
            <div class="fact"><span>Main + extras</span><b>${g.hltb_extra ? g.hltb_extra + 'h' : '—'}</b></div>
            <div class="fact"><span>Completionist</span><b>${g.hltb_complete ? g.hltb_complete + 'h' : '—'}</b></div>` : `<div class="fact"><span>HLTB</span><b class="small">${S.settings.hltb_enabled === '1' ? 'not found yet' : 'off'}</b></div>`}
        </div>
        ${p ? `<div class="small">${Math.round(p)}% of the main story time</div><div class="prog" style="margin-bottom:1rem"><div style="width:${p}%"></div></div>` : ''}
        ${years.length ? `<label>Hours by year</label><div class="yrs">${years.map(([y, mn]) => `<span>${y}: <b>${hrs(mn)}</b></span>`).join('')}</div>` : ''}
        <div class="grid2"><div><label>Started</label><input type="date" id="gm-started" value="${esc(g.started)}"></div>
          <div><label>Completed</label><input type="date" id="gm-completed" value="${esc(g.completed_date)}"></div></div>
        <label>Notes</label><textarea id="gm-notes" placeholder="Thoughts, where you got to, what's left…">${esc(g.notes)}</textarea>

        <details ${!g.sources.length ? 'open' : ''}><summary>Log time played (${sessions.length} entries)</summary>
          <p class="small">For games that can't sync (Switch, Battle.net, retro…) or play that didn't get picked up.</p>
          <div class="row"><input type="number" id="gm-h" step="0.25" min="0" placeholder="Hours" style="width:110px"><input type="date" id="gm-d" value="${S.today}" style="width:170px"><button class="btn sm" id="gm-log">Add time</button></div>
          ${sessions.length ? `<ul class="list" style="margin-top:.6rem">${sessions.slice(0, 40).map(s => `<li><div class="grow">${fmtDate(s.day)}<small>${['manual', 'import'].includes(s.source) ? srcLabel(s.source) : srcLabel(s.source) + ' sync'}</small></div><b>${hrs(s.minutes)}</b>
            ${['manual', 'import'].includes(s.source) ? `<button class="btn sm ghost" data-del-s="${s.id}" title="Remove">✕</button>` : ''}</li>`).join('')}</ul>` : ''}
        </details>
        <details><summary>Edit details</summary>
          <label>Title</label><input id="gm-title" value="${esc(g.title)}">
          <div class="grid2"><div><label>Platforms</label><input id="gm-plat" value="${esc(g.platforms)}"></div><div><label>Metascore</label><input id="gm-meta" type="number" min="0" max="100" value="${g.metascore ?? ''}"></div></div>
          <label>Cover image URL</label><input id="gm-cover" value="${esc(g.cover)}">
          <div class="grid2"><div><label>HLTB main (hours)</label><input id="gm-hm" type="number" step="0.5" value="${g.hltb_main ?? ''}"></div><div><label>HLTB completionist</label><input id="gm-hc" type="number" step="0.5" value="${g.hltb_complete ?? ''}"></div></div>
          <div class="row" style="margin-top:.8rem"><button class="btn sm" id="gm-save-details">Save details</button><button class="btn sm" id="gm-refetch">Re-fetch Metascore &amp; HLTB</button></div>
          <label>Same game as… (merge, e.g. a Steam and a PlayStation copy)</label>
          <div class="row"><select id="gm-merge" style="flex:1"><option value="">Choose a game</option>${others.map(o => `<option value="${o.id}">${esc(o.title)}${o.platforms ? ` (${esc(o.platforms)})` : ''}</option>`).join('')}</select><button class="btn sm" id="gm-merge-go">Merge</button></div>
        </details>
        <div class="row" style="margin-top:1.2rem">
          <button class="btn sm danger" id="gm-del">${g.sources.length ? (g.status === 'ignored' ? 'Unhide' : 'Hide from library') : 'Delete'}</button>
          ${g.status ? '<button class="btn sm ghost" id="gm-unsort">Remove status</button>' : ''}
          <span class="spacer"></span><button class="btn primary" onclick="$('#modal').close()">Done</button></div>
      </div></div>`;
  if (!m.open) m.showModal();
  const put = async (data, reopen = true) => guarded(async () => { await api('PUT', `/api/games/${id}`, data); await refresh(); if (reopen) openGame(id); render(); });

  $$('#gm-status button').forEach(b => b.onclick = () => put({ status: g.status === b.dataset.s ? '' : b.dataset.s }));
  const rr = $('#gm-rating');
  rr.oninput = () => { $('#gm-rv').textContent = rr.value; $('#gm-stars').textContent = stars(+rr.value); };
  rr.onchange = () => put({ rating: +rr.value });
  if ($('#gm-unrate')) $('#gm-unrate').onclick = e => { e.preventDefault(); put({ rating: null }); };
  $('#gm-started').onchange = e => put({ started: e.target.value }, false);
  $('#gm-completed').onchange = e => put({ completed_date: e.target.value, ...(e.target.value && g.status !== 'completed' ? { status: 'completed' } : {}) });
  $('#gm-notes').onchange = e => put({ notes: e.target.value }, false);
  $('#gm-log').onclick = () => guarded(async () => {
    await api('POST', `/api/games/${id}/sessions`, { hours: $('#gm-h').value, day: $('#gm-d').value }); await refresh(); openGame(id); render(); toast('Time logged');
  });
  $$('[data-del-s]').forEach(b => b.onclick = () => guarded(async () => { await api('DELETE', `/api/sessions/${b.dataset.delS}`); await refresh(); openGame(id); render(); }));
  $('#gm-save-details').onclick = () => put({ title: $('#gm-title').value, platforms: $('#gm-plat').value, metascore: $('#gm-meta').value,
    cover: $('#gm-cover').value, hltb_main: $('#gm-hm').value, hltb_complete: $('#gm-hc').value });
  $('#gm-refetch').onclick = async e => {
    e.target.disabled = true; e.target.innerHTML = '<span class="spin">⟳</span> Fetching…';
    await guarded(async () => { await api('POST', `/api/games/${id}/refresh`); await refresh(); openGame(id); render(); toast('Details refreshed'); });
  };
  $('#gm-cover-pick').onclick = () => openCoverPicker(id);
  $('#gm-merge-go').onclick = () => {
    const into = +$('#gm-merge').value; if (!into) return toast('Choose the game to merge into');
    if (!confirm(`Merge “${g.title}” into “${game(into).title}”? Playtime and accounts are combined and this entry is removed.`)) return;
    guarded(async () => { await api('POST', `/api/games/${id}/merge`, { into }); await refresh(); openGame(into); render(); toast('Merged'); });
  };
  $('#gm-del').onclick = () => {
    if (g.status === 'ignored') return put({ status: '' });
    if (!g.sources.length && !confirm(`Delete “${g.title}”?`)) return;
    guarded(async () => { await api('DELETE', `/api/games/${id}`); m.close(); await load(); toast(g.sources.length ? 'Hidden' : 'Deleted'); });
  };
  if ($('#gm-unsort')) $('#gm-unsort').onclick = () => put({ status: '' });
}

async function openCoverPicker(id, q = '') {
  const g = game(id);
  $('#modal-body').innerHTML = `<div class="card" style="margin:0;border:0">
    <div class="row"><h2 style="flex:1">Choose a cover for ${esc(g.title)}</h2><button class="btn sm" id="cp-back">← Back</button></div>
    <div class="row" style="margin-top:.8rem"><input id="cp-q" value="${esc(q || g.title)}" placeholder="Search for art" style="flex:1"><button class="btn sm" id="cp-search">Search</button></div>
    <div id="cp-grid" style="margin-top:1rem"><p class="muted"><span class="spin">⟳</span> Finding covers…</p></div>
    <details><summary>Use your own image</summary>
      <div class="row" style="margin-top:.6rem"><input id="cp-url" placeholder="Image URL (https://…)" style="flex:1"><button class="btn sm" id="cp-url-go">Use URL</button></div>
      <div class="row" style="margin-top:.6rem"><input type="file" id="cp-file" accept="image/*" style="flex:1"><button class="btn sm" id="cp-file-go">Upload</button></div>
    </details></div>`;
  const pick = async cover => guarded(async () => { await api('PUT', `/api/games/${id}`, { cover }); await refresh(); render(); openGame(id); toast('Cover updated'); });
  $('#cp-back').onclick = () => openGame(id);
  $('#cp-search').onclick = () => openCoverPicker(id, $('#cp-q').value.trim());
  $('#cp-q').onkeydown = e => { if (e.key === 'Enter') $('#cp-search').click(); };
  $('#cp-url-go').onclick = () => { const u = $('#cp-url').value.trim(); if (!/^https?:\/\//.test(u)) return toast('Paste a full image URL'); pick(u); };
  $('#cp-file-go').onclick = () => guarded(async () => {
    const f = $('#cp-file').files[0]; if (!f) return toast('Choose an image first');
    const fd = new FormData(); fd.append('file', f);
    await api('POST', `/api/games/${id}/cover`, fd); await refresh(); render(); openGame(id); toast('Cover updated');
  });
  try {
    const r = await api('GET', `/api/games/${id}/covers?q=${encodeURIComponent(q || g.title)}`);
    const el = $('#cp-grid'); if (!el) return;
    el.innerHTML = (r.options.length ? `<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">${r.options.map((o, i) => `<div class="tile" data-cp="${i}">
        <div class="cover"><img src="${esc(o.thumb)}" alt="" loading="lazy" onerror="this.closest('.tile').remove()"></div><div class="s"><span>${esc(o.source)}</span></div></div>`).join('')}</div>`
      : '<div class="empty">No covers found. Try a different search, or use your own image below.</div>')
      + (r.has_sgdb ? '' : '<p class="small" style="margin-top:.8rem">Add a free SteamGridDB key in Settings for proper box art for every game, including Switch and other consoles.</p>')
      + (r.errors.length ? `<p class="small err">${esc(r.errors.join(' · '))}</p>` : '');
    $$('[data-cp]', el).forEach(t => t.onclick = () => pick(r.options[+t.dataset.cp].url));
  } catch (e) { const el = $('#cp-grid'); if (el) el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------------ year round-up */

function renderYear() {
  return `<div class="container" id="year-root"><p class="muted"><span class="spin">⟳</span> Building your round-up…</p></div>`;
}
async function loadYear() {
  const y = roundYear || YEAR;
  const R = await guarded(() => api('GET', '/api/roundup?year=' + y));
  if (!R || view !== 'year') return;
  const root = $('#year-root');
  const isNow = y === YEAR && new Date().getMonth() < 11;
  const maxM = Math.max(...R.months, 1);
  const maxP = Math.max(...R.platforms.map(p => p[1]), 1);
  const top = R.games.filter(g => g.ymin).slice(0, 10);
  const pickable = R.games.filter(g => g.status !== 'ignored').sort((a, b) => a.title.localeCompare(b.title));
  const g = R.goty;
  const busy = R.busiest_day;
  const longest = R.games.filter(x => x.status === 'completed' && x.ymin).sort((a, b) => b.ymin - a.ymin)[0];
  root.innerHTML = `
    <div class="page-head"><div class="years">${R.years.map(x => `<span class="chip ${x === y ? 'on' : ''}" data-y="${x}">${x}</span>`).join('')}</div></div>
    <div class="wrap-hero">
      <div class="eyebrow">${isNow ? 'So far this year' : 'Year in review'}</div>
      <h1>${S.settings.display_name ? esc(S.settings.display_name) + "'s " : 'Your '}${y} in games</h1>
      <div class="stats">
        <div class="stat"><span>Hours played</span><b>${hrsNum(R.total_minutes)}</b><small>${R.total_minutes ? `about ${(R.total_minutes / 60 / (isNow ? Math.max(1, dayOfYear()) : 365) * 7).toFixed(1)}h a week` : 'no tracked time'}</small></div>
        <div class="stat"><span>Games played</span><b>${R.games.length}</b><small>${R.started.length} started this year</small></div>
        <div class="stat"><span>Completed</span><b>${R.completed.length}</b><small>${R.backlog_cleared ? `${R.backlog_cleared} from the backlog` : `${R.dropped.length} dropped`}</small></div>
        <div class="stat"><span>Average rating</span><b>${R.avg_rating ?? '—'}</b><small>${R.top_rated.length ? 'across rated games' : 'rate games to see this'}</small></div>
      </div></div>

    ${g ? `<div class="card"><div class="goty"><div class="tile" data-game="${g.id}">${coverImg(g)}</div><div style="flex:1">
      <div class="label">🏆 Game of the year${R.goty_picked ? '' : ' (your highest rated)'}</div><h2>${esc(g.title)}</h2>
      <div class="row">${g.rating != null ? `<span class="score" style="font-size:1.1rem">★ ${g.rating}</span>` : ''}${g.year_minutes[y] ? `<span class="muted">${hrs(g.year_minutes[y])} this year</span>` : ''}${metaBadge(g.metascore)}</div>
      <label>Pick your own</label><select id="goty-pick" style="max-width:360px"><option value="">Highest rated (automatic)</option>${pickable.map(x => `<option value="${x.id}" ${R.goty_picked && x.id === g.id ? 'selected' : ''}>${esc(x.title)}</option>`).join('')}</select>
    </div></div></div>` : ''}

    <div class="grid2">
      <div class="card"><h2>Hours by month</h2><div class="bars">${R.months.map((mn, i) => `<div class="b"><em>${mn ? Math.round(mn / 60) : ''}</em><div style="height:${mn / maxM * 100}%"></div><span>${MONTHS[i]}</span></div>`).join('')}</div></div>
      <div class="card"><h2>Platforms</h2>${R.platforms.length ? R.platforms.map(([p, mn]) => `<div class="hbar"><div class="lbl"><span>${esc(p)}</span><b>${hrs(mn, true)}</b></div><div class="prog"><div style="width:${mn / maxP * 100}%"></div></div></div>`).join('') : '<p class="muted">No tracked time yet.</p>'}
        ${R.genres.length ? `<h3 style="margin:1.2rem 0 .6rem">Top genres</h3><div class="tags">${R.genres.map(([n, c]) => `<span>${esc(n)}<b>${c}</b></span>`).join('')}</div>` : ''}</div>
    </div>

    ${top.length ? `<div class="section"><h2>Most played</h2></div><div class="rank">${top.map((x, i) => tile(x, { year: y, n: i + 1 })).join('')}</div>` : ''}

    <div class="section"><h2>Completed in ${y}</h2><span class="muted">${R.completed.length} games</span></div>
    ${R.completed.length ? `<div class="grid">${R.completed.map(x => tile(x, { extra: fmtDate(x.completed_date, { day: 'numeric', month: 'short' }) })).join('')}</div>` : '<div class="empty">Nothing completed yet. Mark games as completed and they appear here.</div>'}

    <div class="grid2" style="margin-top:1.6rem">
      <div class="card"><h2>Top rated</h2>${R.top_rated.length ? `<ul class="list">${R.top_rated.map(x => `<li data-game="${x.id}" style="cursor:pointer"><img class="thumb" src="${esc(x.cover || x.banner)}" alt="" onerror="this.style.visibility='hidden'">
        <div class="grow"><b>${esc(x.title)}</b><small>${STATUS_LABEL[x.status]}</small></div><span class="score">★ ${x.rating}</span></li>`).join('')}</ul>` : '<p class="muted">Rate some games to fill this in.</p>'}</div>
      <div class="card"><h2>Fun facts</h2><ul class="list">
        ${busy ? `<li><div class="grow"><b>Biggest day</b><small>${fmtDate(busy.day, { weekday: 'long', day: 'numeric', month: 'long' })}</small></div><b>${hrs(busy.minutes)}</b></li>` : ''}
        ${longest ? `<li><div class="grow"><b>Longest completion</b><small>${esc(longest.title)}</small></div><b>${hrs(longest.ymin)}</b></li>` : ''}
        ${R.hot_takes.map(x => `<li><div class="grow"><b>${x.rating * 10 > x.metascore ? 'Liked it more than critics' : 'Critics liked it more'}</b><small>${esc(x.title)}: you ${x.rating}/10, Metascore ${x.metascore}</small></div><b>${x.rating * 10 > x.metascore ? '+' : ''}${Math.round(x.rating * 10 - x.metascore)}</b></li>`).join('')}
        <li><div class="grow"><b>Dropped</b><small>${R.dropped.map(x => esc(x.title)).slice(0, 4).join(', ') || 'Nothing. Impressive.'}</small></div><b>${R.dropped.length}</b></li>
      </ul></div>
    </div>
    ${R.untracked ? `<p class="small">${R.untracked} of these games were played before tracking started${R.tracking_since ? ` (${fmtDate(R.tracking_since)})` : ''}, so their hours for ${y} aren't known. Their total playtime is still shown elsewhere.</p>` : ''}
    ${R.games.length > top.length ? `<div class="section"><h2>Everything you played</h2></div><div class="grid">${R.games.map(x => tile(x, { year: y })).join('')}</div>` : ''}`;
  $$('[data-y]', root).forEach(c => c.onclick = () => { roundYear = c.dataset.y; loadYear(); });
  $$('[data-game]', root).forEach(t => t.onclick = () => openGame(+t.dataset.game));
  if ($('#goty-pick')) $('#goty-pick').onchange = e => guarded(async () => { await api('POST', '/api/settings', { [`goty_${y}`]: e.target.value }); loadYear(); });
}
const dayOfYear = () => Math.floor((Date.now() - new Date(new Date().getFullYear(), 0, 0)) / 86400000);

/* ------------------------------------------------------------------ account forms (shared by wizard + settings) */

function steamForm() {
  const s = S.settings;
  return `<div id="steam-form">${s.has_steam_key ? `<p class="ok">✓ Connected (SteamID ${esc(s.steam_id)})</p>` : ''}
    <label>Steam Web API key</label><input id="steam-key" placeholder="${s.has_steam_key ? '•••••••• (saved — leave blank to keep)' : '32-character key'}" autocomplete="off">
    <div class="small">Get one at <a href="https://steamcommunity.com/dev/apikey" target="_blank" rel="noopener">steamcommunity.com/dev/apikey</a> (any domain name works, e.g. <code>localhost</code>).</div>
    <label>Your Steam profile</label><input id="steam-id" value="${esc(s.steam_id)}" placeholder="Profile URL, custom URL name or SteamID64">
    <div class="small">Your profile's <b>Game details</b> must be set to <b>Public</b> in Steam privacy settings.</div>
    <div class="row" style="margin-top:.8rem"><button class="btn sm primary" id="steam-go">${s.has_steam_key ? 'Update' : 'Connect Steam'}</button>${s.has_steam_key ? '<button class="btn sm danger" id="steam-rm">Disconnect</button>' : ''}<span id="steam-msg"></span></div></div>`;
}
function psnForm() {
  const s = S.settings;
  const exp = s.psn_expires ? new Date(+s.psn_expires * 1000) : null;
  return `<div id="psn-form">${s.has_psn ? `<p class="ok">✓ Connected${exp && +s.psn_expires > 0 ? ` <span class="small">· sign-in valid until ${fmtDate(exp.toISOString())}</span>` : ''}</p>` : ''}
    <ol><li>Sign in at <a href="https://www.playstation.com" target="_blank" rel="noopener">playstation.com</a> in your browser.</li>
      <li>In the same browser, open <a href="https://ca.account.sony.com/api/v1/ssocookie" target="_blank" rel="noopener">ca.account.sony.com/api/v1/ssocookie</a>.</li>
      <li>Copy the <code>npsso</code> value (or the whole page) and paste it below.</li></ol>
    <label>NPSSO token</label><input id="psn-npsso" placeholder="${s.has_psn ? '•••••••• (saved)' : 'Paste here'}" autocomplete="off">
    <div class="note">This uses the same unofficial API as the PlayStation app, so treat the token like a password. VGTracker only reads your game list and playtime, a few times a day. The sign-in lasts about two months, after which you paste a fresh token. PS4 and PS5 games only.</div>
    <div class="row"><button class="btn sm primary" id="psn-go">${s.has_psn ? 'Update' : 'Connect PlayStation'}</button>${s.has_psn ? '<button class="btn sm danger" id="psn-rm">Disconnect</button>' : ''}<span id="psn-msg"></span></div></div>`;
}
function xboxForm() {
  const s = S.settings;
  return `<div id="xbox-form">${s.has_xbox ? '<p class="ok">✓ Connected</p>' : ''}
    <ol><li>Go to <a href="https://xbl.io" target="_blank" rel="noopener">xbl.io</a> (OpenXBL) and sign in with your Microsoft account. It's free.</li>
      <li>On your OpenXBL profile page, create an API key and paste it below.</li></ol>
    <label>OpenXBL API key</label><input id="xbox-key" placeholder="${s.has_xbox ? '•••••••• (saved)' : 'Paste your key'}" autocomplete="off">
    <div class="note">Xbox has no public API for players, so this goes through OpenXBL, a third-party service. Covers Xbox consoles and PC games played through the Xbox app. OpenXBL's free plan limits requests, so playtime for a big Xbox history fills in over the first few syncs.</div>
    <div class="row"><button class="btn sm primary" id="xbox-go">${s.has_xbox ? 'Update' : 'Connect Xbox'}</button>${s.has_xbox ? '<button class="btn sm danger" id="xbox-rm">Disconnect</button>' : ''}<span id="xbox-msg"></span></div></div>`;
}
function sgdbForm() {
  return `<div id="sgdb-form" style="margin-top:1.4rem">${S.settings.has_sgdb ? '<p class="ok">✓ SteamGridDB key saved</p>' : ''}
    <label>SteamGridDB API key (optional, free)</label><input id="sgdb-key" placeholder="${S.settings.has_sgdb ? '•••••••• (saved)' : 'Paste your key'}" autocomplete="off">
    <div class="small">Proper portrait box art for every game, including Switch, Battle.net and other non-Steam games. Get a key at <a href="https://www.steamgriddb.com/profile/preferences/api" target="_blank" rel="noopener">steamgriddb.com/profile/preferences/api</a>.</div>
    <div class="row" style="margin-top:.8rem"><button class="btn sm primary" id="sgdb-go">Save key</button>${S.settings.has_sgdb ? '<button class="btn sm danger" id="sgdb-rm">Remove</button>' : ''}<span id="sgdb-msg"></span></div></div>`;
}
function rawgForm() {
  return `<div id="rawg-form">${S.settings.has_rawg_key ? '<p class="ok">✓ RAWG key saved</p>' : ''}
    <label>RAWG API key (optional, free)</label><input id="rawg-key" placeholder="${S.settings.has_rawg_key ? '•••••••• (saved)' : 'Paste your key'}" autocomplete="off">
    <div class="small">Lets you search every platform (not just Steam) and fills in Metascores and genres. Sign up at <a href="https://rawg.io/apidocs" target="_blank" rel="noopener">rawg.io/apidocs</a>.</div>
    <div class="row" style="margin-top:.8rem"><button class="btn sm primary" id="rawg-go">Save key</button>${S.settings.has_rawg_key ? '<button class="btn sm danger" id="rawg-rm">Remove</button>' : ''}<span id="rawg-msg"></span></div></div>`;
}
function yamForm() {
  return `<div id="yam-form"><label>Yamtrack CSV export</label><input type="file" id="yam-file" accept=".csv,text/csv">
    <div class="small">In Yamtrack: <b>Settings → Import/Export → Export CSV</b>. Only games are imported; status, score, playtime, dates and notes come across. Games already here keep what you've set.</div>
    <div class="row" style="margin-top:.8rem"><button class="btn sm primary" id="yam-go">Import</button><span id="yam-msg"></span></div></div>`;
}
function passwordForm() {
  if (S.password_from_env) return '<p class="muted">The password is set with <code>APP_PASSWORD</code> in your Docker config.</p>';
  return `<div id="pw-form"><p class="muted">${S.password_enabled ? '✓ A password is set. Each device stays signed in for 90 days.' : 'No password. Anyone who can reach this page can use it — fine on a home network.'}</p>
    <div class="row"><input type="password" id="pw" placeholder="${S.password_enabled ? 'New password' : 'Choose a password'}" style="max-width:260px" autocomplete="new-password">
    <button class="btn sm primary" id="pw-go">${S.password_enabled ? 'Change' : 'Set password'}</button>${S.password_enabled ? '<button class="btn sm danger" id="pw-rm">Remove password</button>' : ''}<span id="pw-msg"></span></div></div>`;
}

async function busy(btn, msgEl, fn) {
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spin">⟳</span>'; msgEl.innerHTML = '';
  try { await fn(); } catch (e) { msgEl.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  finally { btn.disabled = false; btn.innerHTML = old; }
}
function bindAccountForms(after) {
  const done = async (el, html) => { await refresh(); if (after) after(); else render(); const m = $(el); if (m) m.innerHTML = html; };
  if ($('#steam-go')) $('#steam-go').onclick = e => busy(e.target, $('#steam-msg'), async () => {
    const r = await api('POST', '/api/accounts/steam', { key: $('#steam-key').value, steam_id: $('#steam-id').value });
    await done('#steam-msg', `<span class="ok">Found ${r.games} games (${r.played} played)</span>`);
  });
  if ($('#steam-rm')) $('#steam-rm').onclick = e => busy(e.target, $('#steam-msg'), async () => { await api('POST', '/api/accounts/steam', { remove: true }); await done(); });
  if ($('#psn-go')) $('#psn-go').onclick = e => busy(e.target, $('#psn-msg'), async () => {
    const r = await api('POST', '/api/accounts/psn', { npsso: $('#psn-npsso').value });
    await done('#psn-msg', `<span class="ok">Found ${r.games} played games</span>`);
  });
  if ($('#psn-rm')) $('#psn-rm').onclick = e => busy(e.target, $('#psn-msg'), async () => { await api('POST', '/api/accounts/psn', { remove: true }); await done(); });
  if ($('#xbox-go')) $('#xbox-go').onclick = e => busy(e.target, $('#xbox-msg'), async () => {
    const r = await api('POST', '/api/accounts/xbox', { key: $('#xbox-key').value });
    await done('#xbox-msg', `<span class="ok">Found ${r.games} games</span>`);
  });
  if ($('#xbox-rm')) $('#xbox-rm').onclick = e => busy(e.target, $('#xbox-msg'), async () => { await api('POST', '/api/accounts/xbox', { remove: true }); await done(); });
  if ($('#sgdb-go')) $('#sgdb-go').onclick = e => busy(e.target, $('#sgdb-msg'), async () => {
    await api('POST', '/api/accounts/sgdb', { key: $('#sgdb-key').value }); await done('#sgdb-msg', '<span class="ok">Saved. Covers update in the background.</span>');
  });
  if ($('#sgdb-rm')) $('#sgdb-rm').onclick = e => busy(e.target, $('#sgdb-msg'), async () => { await api('POST', '/api/accounts/sgdb', { key: '' }); await done(); });
  if ($('#rawg-go')) $('#rawg-go').onclick = e => busy(e.target, $('#rawg-msg'), async () => {
    await api('POST', '/api/accounts/rawg', { key: $('#rawg-key').value }); await done('#rawg-msg', '<span class="ok">Saved</span>');
  });
  if ($('#rawg-rm')) $('#rawg-rm').onclick = e => busy(e.target, $('#rawg-msg'), async () => { await api('POST', '/api/accounts/rawg', { key: '' }); await done(); });
  if ($('#yam-go')) $('#yam-go').onclick = e => busy(e.target, $('#yam-msg'), async () => {
    const f = $('#yam-file').files[0]; if (!f) throw new Error('Choose the CSV file first');
    const fd = new FormData(); fd.append('file', f);
    const r = await api('POST', '/api/import/yamtrack', fd);
    await done('#yam-msg', `<span class="ok">${r.added} added, ${r.updated} updated, ${r.skipped} skipped</span>`);
  });
  if ($('#pw-go')) $('#pw-go').onclick = e => busy(e.target, $('#pw-msg'), async () => {
    if (!$('#pw').value) throw new Error('Type a password');
    await api('POST', '/api/password', { password: $('#pw').value }); S.password_enabled = true; toast('Password set'); await done();
  });
  if ($('#pw-rm')) $('#pw-rm').onclick = e => busy(e.target, $('#pw-msg'), async () => { await api('POST', '/api/password', { password: '' }); toast('Password removed'); await done(); });
}

/* ------------------------------------------------------------------ settings */

function renderSettings() {
  const s = S.settings;
  return `<div class="container narrow"><div class="page-head"><h1>Settings</h1></div>
    <div class="card"><h2>Steam</h2>${steamForm()}</div>
    <div class="card"><h2>PlayStation</h2>${psnForm()}</div>
    <div class="card"><h2>Xbox</h2>${xboxForm()}</div>
    <div class="card"><h2>Syncing</h2>
      <p class="muted">${s.last_sync ? `Last sync ${ago(s.last_sync)} (${fmtDate(s.last_sync, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}): ${esc(s.last_sync_result)}` : 'Not synced yet.'}</p>
      <div class="row"><label style="margin:0">Sync every</label><select id="set-sync" style="width:auto">${[['1', 'hour'], ['3', '3 hours'], ['6', '6 hours'], ['12', '12 hours'], ['24', 'day'], ['0', 'never (manual only)']]
        .map(([v, l]) => `<option value="${v}" ${s.sync_hours === v ? 'selected' : ''}>${l}</option>`).join('')}</select><button class="btn sm" id="set-sync-now">Sync now</button></div>
      <p class="small">Steam, PlayStation and Xbox only report total playtime, so VGTracker records the difference at every sync. That's how hours get split by day and year. More frequent syncs give more accurate dates.</p></div>
    <div class="card"><h2>Game details</h2>${rawgForm()}${sgdbForm()}
      <label class="check" style="margin-top:1.2rem"><input type="checkbox" id="set-hltb" ${s.hltb_enabled === '1' ? 'checked' : ''}> <span>Fetch HowLongToBeat times${s.hltb_available ? '' : ' <span class="err">(howlongtobeatpy isn\'t installed)</span>'}</span></label>
      <div class="row" style="margin-top:.8rem"><button class="btn sm" id="set-refetch">Re-fetch details for every game</button>${S.enrich_pending ? `<span class="small">${S.enrich_pending} games waiting for details</span>` : ''}</div></div>
    <div class="card"><h2>Import from Yamtrack</h2>${yamForm()}</div>
    <div class="card"><h2>Profile</h2><label>Your name (for greetings and the round-up)</label>
      <div class="row"><input id="set-name" value="${esc(s.display_name)}" style="max-width:260px"><button class="btn sm" id="set-name-go">Save</button></div></div>
    <div class="card"><h2>Password</h2>${passwordForm()}</div>
    <div class="card"><h2>Homepage widget</h2><p class="muted">A JSON summary for dashboards like Homepage (<code>customapi</code> widget) or Glance. The link contains a private token, so it works with a password set.</p>
      <code id="widget-url">${esc(location.origin + S.widget_url)}</code>
      <pre class="small" style="white-space:pre-wrap;background:var(--bg2);padding:.8rem;border-radius:10px;border:1px solid var(--line);margin-top:.8rem">- VGTracker:
    href: ${esc(location.origin)}
    widget:
      type: customapi
      url: ${esc(location.origin + S.widget_url)}
      mappings:
        - field: now_playing
          label: Playing
        - field: hours_this_year
          label: Hours
        - field: completed_this_year
          label: Completed</pre></div>
    <div class="card"><h2>Backup</h2><p class="muted">Download everything as a JSON file (API keys and tokens aren't included).</p>
      <div class="row"><a class="btn sm" href="/api/backup">Download backup</a><label class="btn sm" style="margin:0">Restore backup<input type="file" id="restore" accept=".json" hidden></label></div></div>
    <div class="card"><h2>Setup</h2><div class="row"><button class="btn sm" id="set-wizard">Run setup wizard again</button>${S.password_enabled && !S.password_from_env ? '<a class="btn sm" href="/logout">Sign out</a>' : ''}</div></div>
  </div>`;
}
function bindSettings() {
  bindAccountForms();
  const save = (d, msg = 'Saved') => guarded(async () => { await api('POST', '/api/settings', d); await refresh(); toast(msg); });
  $('#set-sync').onchange = e => save({ sync_hours: e.target.value });
  $('#set-sync-now').onclick = e => syncNow(e.target);
  $('#set-hltb').onchange = e => save({ hltb_enabled: e.target.checked ? '1' : '0' });
  $('#set-name-go').onclick = () => save({ display_name: $('#set-name').value });
  $('#set-refetch').onclick = () => guarded(async () => { await api('POST', '/api/refresh-all'); await load(); toast('Fetching details in the background'); });
  $('#set-wizard').onclick = () => guarded(async () => { await api('POST', '/api/settings', { setup_done: '0' }); wizStep = 0; await load(); });
  $('#restore').onchange = e => {
    const f = e.target.files[0]; if (!f || !confirm('Replace all games and playtime with this backup?')) return;
    const fd = new FormData(); fd.append('file', f);
    guarded(async () => { await api('POST', '/api/restore', fd); await load(); toast('Backup restored'); });
  };
}

/* ------------------------------------------------------------------ setup wizard */

const WIZ = [
  { title: 'Welcome to VGTracker', body: () => `<p class="muted">Track what you play across Steam, PlayStation and everything else, with your ratings, HowLongToBeat times, Metascores and a round-up at the end of every year.</p>
      <p class="muted">This takes a couple of minutes. You can skip any step and change it later in Settings.</p>
      <label>What should we call you?</label><input id="wiz-name" value="${esc(S.settings.display_name)}" placeholder="Your name (optional)">`,
    next: async () => api('POST', '/api/settings', { display_name: $('#wiz-name').value }) },
  { title: 'Connect Steam', body: () => `<p class="muted">VGTracker reads your Steam library and playtime. Steam Deck playtime counts too.</p>${steamForm()}` },
  { title: 'Connect PlayStation', body: () => `<p class="muted">Reads the PS4 and PS5 games you've played and how long for.</p>${psnForm()}` },
  { title: 'Connect Xbox', body: () => `<p class="muted">Reads the Xbox and Xbox app (PC) games you've played and how long for.</p>${xboxForm()}` },
  { title: 'Game details', body: () => `${rawgForm()}${sgdbForm()}<label class="check" style="margin-top:1.4rem"><input type="checkbox" id="wiz-hltb" ${S.settings.hltb_enabled === '1' ? 'checked' : ''}><span>Fetch HowLongToBeat times<br><span class="small">Looked up in the background for each game.</span></span></label>`,
    next: async () => api('POST', '/api/settings', { hltb_enabled: $('#wiz-hltb').checked ? '1' : '0' }) },
  { title: `What you've played in ${YEAR}`, body: () => `<p class="muted">From now on, every sync records how much you played and when, so hours are split by year exactly.</p>
      <p class="muted">For the time before today, Steam only gives a lifetime total and the date you last played. PlayStation also gives the date you first played.</p>
      <label class="check"><input type="checkbox" id="wiz-attr" ${S.settings.attribute_year === '1' ? 'checked' : ''}><span>Count the existing playtime of games last played in ${YEAR} towards ${YEAR}<br>
      <span class="small">Gives you a full ${YEAR} round-up straight away. Games you started in earlier years will over-count. PlayStation games first played in ${YEAR} are always counted, because their dates are known.</span></span></label>
      <div class="note">Either way, games last played in ${YEAR} appear in the ${YEAR} round-up. This only decides whether their hours do.</div>`,
    next: async () => api('POST', '/api/settings', { attribute_year: $('#wiz-attr').checked ? '1' : '0' }) },
  { title: 'Coming from Yamtrack?', body: () => `<p class="muted">Bring your games, ratings, statuses and notes across. Skip this if you're starting fresh.</p>${yamForm()}` },
  { title: 'Password (optional)', body: () => passwordForm() },
  { title: "You're all set", body: () => `<p class="muted">${hasAccounts() ? 'Hit finish to run the first sync. Covers, Metascores and HLTB times then fill in over the next few minutes in the background.' : 'No accounts connected, so add games by searching, or connect Steam or PlayStation later in Settings.'}</p>
      <ul class="list">${[['Steam', S.settings.has_steam_key], ['PlayStation', S.settings.has_psn], ['Xbox', S.settings.has_xbox], ['RAWG search & Metascores', S.settings.has_rawg_key], ['SteamGridDB box art', S.settings.has_sgdb], ['HowLongToBeat', S.settings.hltb_enabled === '1'], ['Password', S.password_enabled]]
        .map(([l, on]) => `<li><div class="grow">${l}</div>${on ? '<span class="ok">✓ On</span>' : '<span class="muted">Off</span>'}</li>`).join('')}</ul><div id="wiz-sync"></div>`,
    next: async () => {
      if (hasAccounts()) {
        $('#wiz-sync').innerHTML = '<p class="muted"><span class="spin">⟳</span> Running the first sync…</p>';
        const r = await api('POST', '/api/sync'); toast(r.message);
      }
      await api('POST', '/api/settings', { setup_done: '1' });
      view = 'dashboard'; location.hash = 'dashboard';
    } },
];
function renderWizard() {
  const st = WIZ[wizStep];
  return `<div class="wizard"><div class="wiz"><div class="dots">${WIZ.map((_, i) => `<i class="${i <= wizStep ? 'on' : ''}"></i>`).join('')}</div>
    <div class="small" style="margin-top:1rem">Step ${wizStep + 1} of ${WIZ.length}</div><h1>${st.title}</h1>${st.body()}
    <div class="foot">${wizStep ? '<button class="btn ghost" id="wiz-back">Back</button>' : '<span></span>'}
      <button class="btn primary" id="wiz-next">${wizStep === WIZ.length - 1 ? 'Finish' : 'Continue'}</button></div></div></div>`;
}
function bindWizard() {
  bindAccountForms(() => render());
  if ($('#wiz-back')) $('#wiz-back').onclick = () => { wizStep--; render(); };
  $('#wiz-next').onclick = async e => {
    const btn = e.target; btn.disabled = true;
    try { if (WIZ[wizStep].next) await WIZ[wizStep].next(); }
    catch (err) { toast(err.message); btn.disabled = false; return; }
    if (wizStep < WIZ.length - 1) { wizStep++; await refresh(); render(); window.scrollTo(0, 0); }
    else await load();
  };
}

load();
