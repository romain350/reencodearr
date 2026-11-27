'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmtSize = b => { b = b || 0; const u = ['o', 'Ko', 'Mo', 'Go', 'To']; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return (i > 2 ? b.toFixed(1) : Math.round(b)) + ' ' + u[i]; };
const fmtDur = s => { if (!s) return ''; const m = Math.round(s / 60); return m < 1 ? '< 1 min' : m < 60 ? m + ' min' : Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0'); };
// libelles des etats de job et des phases de l'agent
const STATUS = { queued: 'en attente', claimed: 'réservé', encoding: 'encodage', uploading: 'envoi', verifying: 'vérification', done: 'terminé', failed: 'échec', canceled: 'annulé', skipped: 'ignoré' };
const PHASE = { download: 'téléchargement', encode: 'encodage', check: 'contrôle', upload: 'envoi' };
const stLabel = st => STATUS[st] || st;
// confirmation dans la boite de dialogue de l'application (au lieu de confirm() du navigateur)
const ask = (title, text, ok = 'Confirmer', danger = false) => dialog(`<h3>${title}</h3><p>${text}</p>`,
  [{ label: 'Annuler', value: false }, { label: ok, value: true, cls: danger ? 'danger' : 'primary' }]);

// Les onglets Séries et Films partagent la même grille : S.kind dit laquelle est affichée, chacune garde
// sa liste, sa sélection, son tri et sa recherche.
const S = { kind: 'movie', libs: { series: newLib('series'), movie: newLib('movie') }, tab: 'movies', hash: {}, settings: null, paused: false, sections: null };
const L = () => S.libs[S.kind];
const MOVIE = () => S.kind === 'movie';
function newLib(kind) { return { list: [], sel: new Set(), sort: loadSort(kind), search: '', arr: false, dirOk: true }; }
const SORT_STORE = { series: 'rr_sort', movie: 'rr_sort_movie' };

function loadSort(kind) {
  try { const v = JSON.parse(localStorage.getItem(SORT_STORE[kind])); if (v && v.key) return v; } catch (e) {}
  return { key: 'name', dir: 1 };
}
const SORTS = {
  name: (a, b) => a.name.localeCompare(b.name, 'fr', { numeric: true, sensitivity: 'base' }),
  size: (a, b) => a.size - b.size,
  episodes: (a, b) => a.episodes - b.episodes,
  avg: (a, b) => (a.size / (a.episodes || 1)) - (b.size / (b.episodes || 1)),
  todo_size: (a, b) => a.todo_size - b.todo_size,
  todo: (a, b) => a.todo - b.todo,
  hevc_pct: (a, b) => (a.hevc / (a.episodes || 1)) - (b.hevc / (b.episodes || 1)),
  hevc: (a, b) => a.hevc - b.hevc,
  bad: (a, b) => a.bad - b.bad,
  height: (a, b) => (a.height || 0) - (b.height || 0),
  duration: (a, b) => (a.duration || 0) - (b.duration || 0),
  activity: (a, b) => (b.batch.running * 3 + b.batch.active + (b.scan ? 1 : 0)) - (a.batch.running * 3 + a.batch.active + (a.scan ? 1 : 0)),
  selected: (a, b) => (L().sel.has(b.name) ? 1 : 0) - (L().sel.has(a.name) ? 1 : 0),
};
// sens par defaut : alphabetique croissant, le reste du plus grand au plus petit
const DEFAULT_DIR = { name: 1, activity: 1, selected: 1 };
function sortList(list) {
  const so = L().sort, f = SORTS[so.key] || SORTS.name;
  const dirOf = d => (DEFAULT_DIR[so.key] ?? -1) * d;
  return [...list].sort((a, b) => dirOf(so.dir) * f(a, b) || SORTS.name(a, b));
}
function saveSort() { try { localStorage.setItem(SORT_STORE[S.kind], JSON.stringify(L().sort)); } catch (e) {} }

async function api(method, path, body) {
  const r = await fetch(path, { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'reencode', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('non connecté'); }
  let data = {}; try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.detail || ('erreur ' + r.status));
  return data;
}
function toast(msg, ms = 3500) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => t.hidden = true, ms); }

function dialog(html, buttons) {
  return new Promise(res => {
    $('#dlgBody').innerHTML = html;
    const box = $('#dlgActions'); box.innerHTML = '';
    for (const b of buttons) {
      const el = document.createElement('button'); el.className = 'btn ' + (b.cls || ''); el.textContent = b.label;
      el.onclick = () => { $('#dlg').close(); res(b.value); }; box.appendChild(el);
    }
    $('#dlg').onclose = () => res(null);
    $('#dlg').showModal();
  });
}

// ------------------------------------------------------------------ connexion
function showLogin() { $('#app').hidden = true; $('#setup').hidden = true; $('#login').hidden = false; $('#lUser').focus(); }
$('#loginForm').onsubmit = async e => {
  e.preventDefault(); $('#lErr').textContent = '';
  try { await api('POST', '/api/login', { username: $('#lUser').value, password: $('#lPass').value }); $('#lPass').value = ''; await boot(); }
  catch (err) { $('#lErr').textContent = err.message; }
};
$('#logout').onclick = async () => { await api('POST', '/api/logout'); showLogin(); };

async function boot() {
  let st = null;
  try { st = await (await fetch('/api/setup', { credentials: 'same-origin' })).json(); } catch (e) {}
  if (st && st.version) $('#ver').textContent = 'v' + st.version;
  if (st && st.admin_needed) return openSetup(st);
  try { const me = await api('GET', '/api/me'); $('#who').textContent = me.user; if (me.version) $('#ver').textContent = 'v' + me.version; } catch (e) { return; }
  if (st && !st.done) return openSetup(st);
  $('#login').hidden = true; $('#setup').hidden = true; $('#app').hidden = false;
  show(S.tab); poll();
}

// ------------------------------------------------------------------ export / import de la configuration
async function exportConfig(sections, secrets) {
  const qs = new URLSearchParams({ sections: sections.join(','), secrets: secrets ? '1' : '0' });
  const r = await fetch('/api/settings/export?' + qs, { credentials: 'same-origin' });
  if (r.status === 401) { showLogin(); throw new Error('non connecté'); }
  if (!r.ok) throw new Error('export impossible (erreur ' + r.status + ')');
  const name = (/filename="([^"]+)"/.exec(r.headers.get('content-disposition') || '') || [])[1] || 'reencodearr-config.json';
  const data = await r.json();
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return Object.keys(data.sections).length;
}
function pickFile() {
  return new Promise(res => {
    const i = document.createElement('input'); i.type = 'file'; i.accept = '.json,application/json';
    i.onchange = () => res(i.files[0] || null); i.click();
  });
}
// Lit le fichier, montre les categories trouvees, applique celles cochees. Renvoie null si annule.
async function importConfig(file) {
  if (!file) return null;
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { throw new Error("ce fichier n'est pas un JSON valide"); }
  const info = await api('POST', '/api/settings/import', { data, dry_run: true });
  const when = info.exported_at ? ` le ${esc(info.exported_at.slice(0, 16).replace('T', ' à '))}` : '';
  const go = await dialog(`<h3>Importer la configuration</h3>
    <p class="hint">Fichier exporté${when}${info.hub_version ? ' depuis la version ' + esc(info.hub_version) : ''}. Les catégories cochées remplaceront les réglages actuels.</p>
    <div class="checks">${info.found.map(x => `<label class="row"><input type="checkbox" class="im_sec" value="${esc(x.id)}" checked> ${esc(x.label)}${x.secrets ? ' <small class="muted">(avec clés API)</small>' : ''}</label>`).join('')}</div>
    ${info.secrets_included ? '' : `<p class="hint">Ce fichier ne contient pas les clés API : celles déjà enregistrées sont gardées.</p>`}`,
    [{ label: 'Annuler', value: false }, { label: 'Importer', value: true, cls: 'primary' }]);
  if (!go) return null;
  const chosen = $$('.im_sec:checked').map(x => x.value);   // la boite est fermee mais son contenu est encore la
  if (!chosen.length) return null;
  return api('POST', '/api/settings/import', { data, sections: chosen });
}

// ------------------------------------------------------------------ assistant de premier lancement
// Affiche au premier acces (aucun compte) ou tant qu'il n'a pas ete termine ; l'etat est garde dans hub.db.
const W = { steps: [], i: 0, min: 0, s: null, restored: false };
const val = id => ($('#' + id) ? $('#' + id).value.trim() : '');
const testBtn = (svc, label) => `<div class="presets"><button class="btn small" type="button" data-test="${svc}">${label}</button></div><div class="test-res" id="res_${svc}"></div>`;
const STEPS = {
  admin: {
    label: 'Compte', skip: false,
    html: () => `<h2>Créer le compte administrateur</h2>
      <p class="hint">Bienvenue ! Ce compte protège l'interface. Il est enregistré dans <code>/data/hub.db</code> : l'assistant ne reviendra plus aux prochains démarrages.</p>
      <label>Utilisateur<input id="w_user" value="admin" autocomplete="username" required></label>
      <label>Mot de passe (8 caractères minimum)<input id="w_pw" type="password" autocomplete="new-password" required></label>
      <label>Confirmer le mot de passe<input id="w_pw2" type="password" autocomplete="new-password" required></label>`,
    async save() {
      if ($('#w_pw').value !== $('#w_pw2').value) throw new Error('les deux mots de passe ne correspondent pas');
      await api('POST', '/api/setup/admin', { username: val('w_user'), password: $('#w_pw').value });
      $('#who').textContent = val('w_user');
    },
  },
  restore: {
    label: 'Restaurer', skip: true,
    html: () => `<h2>Restaurer une configuration</h2>
      <p class="hint">Vous avez un fichier exporté depuis un autre hub Reencodearr (Paramètres → Sauvegarde de la configuration) ? Importez-le pour reprendre tous vos réglages d'un coup. Sinon, passez cette étape pour tout configurer à la main.</p>
      <div class="presets"><button class="btn small" type="button" id="w_restore">Choisir un fichier…</button></div><div class="test-res" id="res_restore"></div>`,
    async save() { return W.restored ? 'done' : null; },
  },
  sonarr: {
    label: 'Sonarr', skip: true,
    html: s => `<h2>Sonarr</h2>
      <p class="hint">Sert aux affiches des séries et, après « Supprimer les défectueux », à rescanner la série puis relancer la recherche. Clé API : Sonarr → Settings → General → Security.</p>
      <label>URL<input id="w_sn_url" placeholder="http://sonarr:8989" value="${esc(s.sonarr.url)}"></label>
      <label>Clé API<input id="w_sn_key" type="password" autocomplete="off" value="${esc(s.sonarr.api_key)}"></label>
      ${testBtn('sonarr', 'Tester la connexion')}`,
    body: () => ({ sonarr: { url: val('w_sn_url'), api_key: val('w_sn_key') } }),
    test: async () => { const r = await api('POST', '/api/settings/sonarr-test'); return `Sonarr ${r.version} : ${r.series} séries`; },
  },
  radarr: {
    label: 'Radarr', skip: true,
    html: s => `<h2>Radarr</h2>
      <p class="hint">Facultatif. Sert aux affiches des films (onglet Films) et, après « Supprimer les défectueux », à rescanner le film puis relancer sa recherche. Clé API : Radarr → Settings → General → Security.</p>
      <label>URL<input id="w_ra_url" placeholder="http://radarr:7878" value="${esc(s.radarr.url)}"></label>
      <label>Clé API<input id="w_ra_key" type="password" autocomplete="off" value="${esc(s.radarr.api_key)}"></label>
      ${testBtn('radarr', 'Tester la connexion')}`,
    body: () => ({ radarr: { url: val('w_ra_url'), api_key: val('w_ra_key') } }),
    test: async () => { const r = await api('POST', '/api/settings/radarr-test'); return `Radarr ${r.version} : ${r.movies} films`; },
  },
  posters: {
    label: 'Affiches', skip: true,
    html: s => `<h2>Affiches (TMDB / OMDb)</h2>
      <p class="hint">Utilisées quand Sonarr ou le dossier de la série n'ont pas d'affiche. Clé TMDB : themoviedb.org → Paramètres → API (clé v3 ou jeton de lecture v4).</p>
      <label>Clé TMDB<input id="w_tm_key" type="password" autocomplete="off" value="${esc(s.posters.tmdb_key)}"></label>
      <label>Clé OMDb (facultatif)<input id="w_om_key" type="password" autocomplete="off" value="${esc(s.posters.omdb_key)}" placeholder="omdbapi.com/apikey.aspx"></label>
      <label>Langue<input id="w_po_lang" value="${esc(s.posters.language)}" placeholder="fr-FR"></label>
      ${testBtn('posters', 'Tester les clés')}`,
    body: () => ({ posters: { tmdb_key: val('w_tm_key'), omdb_key: val('w_om_key'), language: val('w_po_lang') || 'fr-FR' } }),
    test: async () => { const r = await api('POST', '/api/settings/posters-test'); return Object.entries(r).map(([k, v]) => k.toUpperCase() + ' ' + v).join(' · '); },
  },
  network: {
    label: 'Réseau', skip: true,
    html: s => `<h2>Adresse du hub</h2>
      <p class="hint">Adresse que les agents d'encodage (GPU) utilisent pour joindre ce hub. Elle est proposée d'après l'adresse de cette page.</p>
      <label>URL publique<input id="w_pub" placeholder="http://adresse-du-hub:8095" value="${esc(s.public_url || location.origin)}"></label>`,
    body: () => ({ public_url: val('w_pub').replace(/\/+$/, '') }),
  },
  done: {
    label: 'Terminé', skip: false,
    html: s => {
      const line = (ok, txt) => `<li>${ok ? '✓' : '—'} ${txt}</li>`;
      return `<h2>C'est prêt</h2>
      <ul class="recap">
        ${line(s.sonarr.url && s.sonarr.api_key, 'Sonarr' + (s.sonarr.url ? ' : ' + esc(s.sonarr.url) : ' non configuré'))}
        ${line(s.radarr.url && s.radarr.api_key, 'Radarr' + (s.radarr.url ? ' : ' + esc(s.radarr.url) : ' non configuré'))}
        ${line(s.posters.tmdb_key || s.posters.omdb_key, 'Affiches : ' + ([s.posters.tmdb_key && 'TMDB', s.posters.omdb_key && 'OMDb'].filter(Boolean).join(' + ') || 'aucune clé'))}
        ${line(s.public_url, 'Adresse du hub : ' + esc(s.public_url || 'non renseignée'))}
      </ul>
      <p class="hint">Tout reste modifiable dans l'onglet Paramètres, qui permet aussi de relancer cet assistant. Prochaine étape : ajouter un host d'encodage dans l'onglet Hosts.</p>`;
    },
    async save() { await api('POST', '/api/setup/finish'); },
  },
};

async function openSetup(st) {
  W.steps = (st.admin_needed ? ['admin'] : []).concat(['restore', 'sonarr', 'radarr', 'posters', 'network', 'done']);
  W.i = 0; W.min = 0; W.restored = false;
  W.s = st.admin_needed ? null : (await api('GET', '/api/settings')).settings;
  $('#login').hidden = true; $('#app').hidden = true; $('#setup').hidden = false;
  renderStep();
}
function renderStep() {
  const key = W.steps[W.i], st = STEPS[key];
  $('#setupSteps').innerHTML = W.steps.map((k, n) => `<li class="${n === W.i ? 'on' : n < W.i ? 'ok' : ''}">${n + 1}. ${STEPS[k].label}</li>`).join('');
  $('#setupBody').innerHTML = st.html(W.s);
  $('#setupErr').textContent = '';
  $('#setupBack').hidden = W.i <= W.min;
  $('#setupSkip').hidden = !st.skip;
  $('#setupNext').textContent = key === 'done' ? 'Ouvrir Reencodearr' : key === 'admin' ? 'Créer le compte' : 'Enregistrer et continuer';
  const first = $('#setupBody input'); if (first) first.focus();
}
async function saveStep() {
  const st = STEPS[W.steps[W.i]];
  let jump = null;
  if (st.save) jump = await st.save();
  else if (st.body) await api('PUT', '/api/settings', st.body());
  W.s = (await api('GET', '/api/settings')).settings;
  return jump;
}
$('#setupForm').onsubmit = async e => {
  e.preventDefault(); $('#setupErr').textContent = '';
  const key = W.steps[W.i];
  try {
    const jump = await saveStep();
    if (key === 'done') { $('#setup').hidden = true; return boot(); }
    if (key === 'admin') W.min = W.i + 1;   // le compte existe : on ne revient plus sur cette etape
    W.i = jump ? W.steps.indexOf(jump) : W.i + 1; renderStep();
  } catch (err) { $('#setupErr').textContent = err.message; }
};
$('#setupSkip').onclick = () => { W.i++; renderStep(); };
$('#setupBack').onclick = () => { if (W.i > W.min) { W.i--; renderStep(); } };
$('#setupBody').onclick = async e => {
  if (e.target.id === 'w_restore') {
    const out = $('#res_restore'); out.className = 'test-res'; out.textContent = '';
    try {
      const r = await importConfig(await pickFile()); if (!r) return;
      W.restored = true; W.s = (await api('GET', '/api/settings')).settings;
      out.textContent = `Importé : ${r.found.filter(x => r.applied.includes(x.id)).map(x => x.label).join(', ')}. « Enregistrer et continuer » mène au récapitulatif.`;
      out.classList.add('ok');
    } catch (err) { out.textContent = err.message; out.classList.add('ko'); }
    return;
  }
  const svc = e.target.dataset.test; if (!svc) return;
  const out = $('#res_' + svc); out.className = 'test-res'; out.textContent = 'Test en cours…';
  try {
    await api('PUT', '/api/settings', STEPS[svc].body());
    out.textContent = await STEPS[svc].test(); out.classList.add('ok');
  } catch (err) { out.textContent = err.message; out.classList.add('ko'); }
};

// ------------------------------------------------------------------ onglets
$$('#tabs button').forEach(b => b.onclick = () => show(b.dataset.tab));
const isLib = tab => tab === 'series' || tab === 'movies';
function show(tab) {
  S.tab = tab;
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  for (const t of ['library', 'queue', 'hosts', 'stats', 'logs', 'settings']) $('#tab-' + t).hidden = t !== (isLib(tab) ? 'library' : tab);
  if (isLib(tab)) setKind(tab === 'movies' ? 'movie' : 'series');
  if (tab === 'settings') loadSettings();
  if (tab === 'stats') loadStats();
  poll(true);
}
// Bascule la grille entre séries et films : textes de la barre d'outils, tri, recherche.
function setKind(kind) {
  if (S.kind !== kind) { L().search = $('#search').value; S.kind = kind; S.hash.library = ''; }
  const m = MOVIE(), arr = m ? 'Radarr' : 'Sonarr';
  $('#search').value = L().search;
  $('#search').placeholder = m ? 'Rechercher un film…' : 'Rechercher une série…';
  $$('#sortKey option').forEach(o => {
    o.hidden = !!o.dataset.kind && o.dataset.kind !== kind;
    if (o.dataset[kind]) o.textContent = o.dataset[kind];
  });
  if ($(`#sortKey option[value="${L().sort.key}"]`).hidden) L().sort = { key: 'name', dir: 1 };
  $('#sortKey').value = L().sort.key; $('#sortDir').textContent = L().sort.dir === 1 ? '↓' : '↑';
  $('[data-act="unmonitor"]').textContent = 'Arrêter le suivi ' + arr;
  $('[data-act="unmonitor"]').title = m ? 'Désactive le suivi Radarr des films entièrement encodés' : 'Désactive le suivi Sonarr des séries entièrement encodées';
  $('[data-act="delete_bad"]').title = `Retire les fichiers défectueux, puis ${arr} relance leur recherche`;
  renderGrid();
}
let timer = null;
function poll(now) {
  clearTimeout(timer);
  const run = async () => {
    if (document.hidden && !now) { timer = setTimeout(run, 4000); return; }
    try {
      if (isLib(S.tab)) await loadLibrary();
      if (S.tab === 'queue') await loadQueue();
      if (S.tab === 'hosts') await loadHosts();
      if (S.tab === 'stats') await loadStats();
      if (S.tab === 'logs' && $('#logLive').checked) await loadLogs();
      await loadBadge();
    } catch (e) {}
    timer = setTimeout(run, 3000);
  };
  run();
}
async function loadBadge() {
  if (!isLib(S.tab) && S.tab !== 'queue') { const d = await api('GET', '/api/status'); S.queued = d.queued; S.paused = d.paused; }
  $('#qCount').textContent = S.queued || '';
  $('#qCount').title = S.paused ? 'File en pause' : '';
}

// ------------------------------------------------------------------ séries et films
async function loadLibrary() {
  const kind = S.kind, m = kind === 'movie';
  const d = await api('GET', m ? '/api/movies' : '/api/series');
  if (kind !== S.kind) return;            // onglet change pendant la requete
  const lib = L(), list = m ? d.movies : d.series;
  S.paused = d.paused; S.queued = d.queued; lib.arr = m ? d.radarr : d.sonarr; lib.dirOk = m ? d.dir_ok : true;
  const h = kind + JSON.stringify(list);
  lib.list = list;
  if (S.hash.library !== h) { S.hash.library = h; renderGrid(); }
}
function tileState(s) {
  const b = s.batch;
  if (b.active > 0) return `<span class="spin"></span>${b.done}/${b.total}${b.failed ? ` <span class="warn">⚠${b.failed}</span>` : ''}`;
  if (s.scan) return `<span class="spin"></span>scan ${s.scan[0]}/${s.scan[1]}`;
  if (s.pending_probe) return `<span class="spin"></span>analyse…`;
  if (b.total > 0) return `<span class="${b.failed ? 'warn' : 'ok'}">${b.failed ? '⚠' : '✓'}</span> ${b.done}/${b.total}${b.failed ? ` (${b.failed} échec)` : ''}`;
  if (s.hevc === s.episodes && s.episodes) return `<span class="ok">✓ ${MOVIE() && s.episodes === 1 ? 'en HEVC' : 'tout en HEVC'}</span>`;
  return '';
}
const filtered = () => { const q = $('#search').value.trim().toLowerCase(); return L().list.filter(s => !q || s.name.toLowerCase().includes(q)); };
function emptyText() {
  if (!MOVIE()) return 'Aucune série trouvée dans le dossier de la bibliothèque.';
  return L().dirOk ? 'Aucun film trouvé dans le dossier des films (un sous-dossier ou un fichier vidéo par film).'
    : 'Le dossier des films n\'est pas monté : ajoutez le volume <code>/movies</code> au conteneur (variable MOVIES_DIR pour un autre chemin).';
}
// Infos d'une tuile : par épisodes pour une série, par fichier (codec, définition, durée) pour un film.
function tileInfo(s) {
  if (!MOVIE()) return `<div class="meta">${s.episodes} épisode${s.episodes > 1 ? 's' : ''} · ${fmtSize(s.size)}</div>
        <div class="bar ${s.batch.running ? 'run' : ''}"><i style="width:${s.episodes ? Math.round(100 * s.hevc / s.episodes) : 0}%"></i></div>
        <div class="meta">${s.hevc}/${s.episodes} en HEVC (H.265)${s.todo ? ` · reste ${fmtSize(s.todo_size)}` : ''}</div>
        ${s.bad ? `<div class="badge">⚠ ${s.bad} épisode${s.bad > 1 ? 's' : ''} défectueux</div>` : ''}
        <button class="more" data-more="${esc(s.name)}">Détails des épisodes</button>`;
  const hevc = s.episodes && s.hevc === s.episodes, tech = [s.codec ? esc(s.codec.toUpperCase()) : '', s.height ? s.height + 'p' : '', fmtDur(s.duration)].filter(Boolean).join(' · ');
  return `<div class="meta">${fmtSize(s.size)}${tech ? ' · ' + tech : ''}</div>
        <div class="bar ${s.batch.running ? 'run' : ''}"><i style="width:${s.episodes ? Math.round(100 * s.hevc / s.episodes) : 0}%"></i></div>
        <div class="meta">${s.pending_probe ? 'analyse en cours' : hevc ? 'En HEVC (H.265)' : s.todo ? `À encoder · ${fmtSize(s.todo_size)}` : 'Déjà dans un codec moderne'}${s.episodes > 1 ? ` · ${s.hevc}/${s.episodes} fichiers en HEVC` : ''}</div>
        ${s.bad ? `<div class="badge">⚠ ${s.episodes > 1 ? s.bad + ' fichier' + (s.bad > 1 ? 's' : '') + ' défectueux' : 'fichier défectueux'}</div>` : ''}
        <button class="more" data-more="${esc(s.name)}">Détails du ${s.episodes > 1 ? 'film (' + s.episodes + ' fichiers)' : 'fichier'}</button>`;
}
function renderGrid() {
  const lib = L(), list = sortList(filtered()), kq = MOVIE() ? '?kind=movie' : '';
  $('#empty').hidden = lib.list.length > 0; $('#empty').innerHTML = emptyText();
  const tot = lib.list.reduce((a, s) => (a.size += s.size, a.todo += s.todo_size, a), { size: 0, todo: 0 });
  $('#libInfo').textContent = lib.list.length ? `${lib.list.length} ${MOVIE() ? 'film' : 'série'}${lib.list.length > 1 ? 's' : ''} · ${fmtSize(tot.size)}${tot.todo ? ' · ' + fmtSize(tot.todo) + ' à encoder' : ''}` : '';
  if (!list.length && lib.list.length) { $('#grid').innerHTML = `<div class="empty wide">Aucun résultat pour « ${esc($('#search').value.trim())} ».</div>`; updateSel(); return; }
  $('#grid').innerHTML = list.map(s => `<div class="tile ${lib.sel.has(s.name) ? 'sel' : ''}" data-n="${esc(s.name)}">
      <div class="poster"><span>${esc(s.name.trim()[0] || '?').toUpperCase()}</span>
        <img loading="lazy" src="/api/poster/${encodeURIComponent(s.name)}${kq}" alt="">
        <div class="state"><span class="chk">${lib.sel.has(s.name) ? '✓' : ''}</span>${tileState(s)}</div></div>
      <div class="info"><div class="name">${esc(s.name)}</div>
        ${tileInfo(s)}</div></div>`).join('');
  updateSel();
}
$('#grid').onclick = e => {
  const more = e.target.closest('[data-more]');
  if (more) { e.stopPropagation(); return openEpisodes(more.dataset.more); }
  const t = e.target.closest('.tile'); if (!t) return;
  const sel = L().sel, n = t.dataset.n; sel.has(n) ? sel.delete(n) : sel.add(n);
  t.classList.toggle('sel'); $('.chk', t).textContent = sel.has(n) ? '✓' : ''; updateSel();
};
// affiche introuvable : l'image est retiree, la lettre reste (ecoute en capture : 'error' ne remonte pas)
$('#grid').addEventListener('error', e => { if (e.target.tagName === 'IMG') e.target.remove(); }, true);
$('#search').oninput = renderGrid;
$('#sortKey').onchange = () => { L().sort = { key: $('#sortKey').value, dir: 1 }; $('#sortDir').textContent = '↓'; saveSort(); renderGrid(); };
$('#sortDir').onclick = () => { L().sort.dir *= -1; $('#sortDir').textContent = L().sort.dir === 1 ? '↓' : '↑'; saveSort(); renderGrid(); };
$('#selAll').onclick = () => { filtered().forEach(s => L().sel.add(s.name)); renderGrid(); };
$('#selNone').onclick = $('#selNone2').onclick = () => { L().sel.clear(); renderGrid(); };
function updateSel() {
  const n = L().sel.size;
  const pl = n > 1 ? 's' : '';
  $('#selCount').textContent = MOVIE() ? `${n} film${pl} sélectionné${pl}` : `${n} série${pl} sélectionnée${pl}`;
  $('#selbar').hidden = !n;
}
$$('[data-act]').forEach(b => b.onclick = () => action(b.dataset.act));

async function action(act) {
  const names = [...L().sel]; if (!names.length) return;
  const m = MOVIE(), kind = S.kind, arr = m ? 'Radarr' : 'Sonarr', arrOk = L().arr;
  const what = m ? `${names.length} film(s)` : `${names.length} série(s)`, unit = m ? 'fichier(s)' : 'épisode(s)';
  const send = extra => api('POST', '/api/actions', { action: act, series: names, kind, ...extra });
  const list = `<p class="hint">${names.map(esc).slice(0, 12).join(', ')}${names.length > 12 ? '…' : ''}</p>`;
  try {
    if (act === 'scan') {
      const lv = await dialog(`<h3>Scanner ${what}</h3>${list}
        <p><b>Rapide</b> : relit entièrement chaque fichier sans le décoder. Détecte les zones à zéro, les conteneurs cassés et le contenu manquant. Peu de CPU.</p>
        <p><b>Complet</b> : décode aussi la vidéo. Beaucoup plus long.</p>`,
        [{ label: 'Annuler', value: null }, { label: 'Complet', value: 'full' }, { label: 'Rapide', value: 'quick', cls: 'primary' }]);
      if (!lv) return;
      const r = await send({ level: lv }); toast(r.queued + ` ${unit} en cours de scan`);
    } else if (act === 'encode') {
      const ok = await dialog(`<h3>Encoder ${what} en HEVC</h3>${list}
        <p>Les fichiers déjà en HEVC, défectueux ou déjà en file sont ignorés. Chaque ${m ? 'film' : 'épisode'} validé remplace l'original selon vos paramètres.</p>
        <label class="row"><input type="checkbox" id="rb"> Réessayer aussi les ${m ? 'films' : 'épisodes'} en échec</label>`,
        [{ label: 'Annuler', value: null }, { label: 'Mettre en file', value: true, cls: 'primary' }]);
      if (!ok) return;
      const r = await send({ retry_bad: $('#rb').checked });
      toast(`${r.enqueued} en file · ${r.already_hevc} déjà HEVC · ${r.already_modern} AV1/VP9 ignorés · ${r.skipped_compact} déjà compacts · ${r.skipped_bad} défectueux ignorés · ${r.already_queued} déjà en file`, 6000);
    } else if (act === 'cancel') {
      if (!await ask(`Annuler les encodages de ${what}`, `Les fichiers en file et en cours d'encodage pour ${m ? 'ces films' : 'ces séries'} sont retirés de la file.`, 'Annuler les encodages', true)) return;
      const r = await send({}); toast(r.canceled + ` ${unit} retiré(s) de la file`);
    } else if (act === 'unmonitor') {
      if (!arrOk) { toast(`${arr} n'est pas configuré (Paramètres)`); return; }
      const ok = await dialog(`<h3>Arrêter le suivi ${arr}</h3>${list}
        <p>Seul${m ? 's les films <b>entièrement encodés</b>' : 'es les séries <b>entièrement encodées</b>'} (plus rien à traiter, aucun fichier défectueux) sont désactivé${m ? 's' : 'es'} dans ${arr} : il ne téléchargera plus de remplacement ni de mise à niveau. Les autres sont laissé${m ? 's' : 'es'} tel${m ? 's' : 'les'} quel${m ? 's' : 'les'}.</p>`,
        [{ label: 'Annuler', value: null }, { label: 'Désactiver le suivi', value: true, cls: 'primary' }]);
      if (!ok) return;
      const r = await send({});
      const inc = Object.entries(r.incomplete), e = m ? '' : 'e';
      toast(`${r.done.length} désactivé${e}(s) · ${r.already.length} déjà non suivi${e}(s)` + (inc.length ? ` · ${inc.length} pas terminé${e}(s) (${inc.slice(0, 2).map(([n, w]) => n + ' : ' + w).join(' ; ')})` : '') + (r.missing.length ? ` · ${r.missing.length} absent${e}(s) de ${arr}` : '') + (Object.keys(r.errors).length ? ` · ${Object.keys(r.errors).length} erreur(s)` : ''), 9000);
    } else if (act === 'delete_bad') {
      const mode = (await api('GET', '/api/settings')).settings.bad_delete_mode;
      const ok = await dialog(`<h3>Retirer les fichiers défectueux</h3>${list}
        <p>${mode === 'delete' ? '<b>Les fichiers seront supprimés définitivement.</b>' : 'Les fichiers seront déplacés dans le dossier caché <code>.quarantaine</code> (non supprimés ; vous pouvez les vider dans Paramètres).'}</p>
        <p>${arrOk ? (m ? 'Radarr va ensuite rescanner le film puis relancer sa recherche.' : 'Sonarr va ensuite rescanner la série puis relancer la recherche des épisodes manquants.') : `<span class="muted">${arr} n'est pas configuré : aucune recherche ne sera relancée.</span>`}</p>`,
        [{ label: 'Annuler', value: null }, { label: 'Confirmer', value: true, cls: 'danger' }]);
      if (!ok) return;
      const r = await send({ confirm: true });
      const relaunch = r[m ? 'radarr' : 'sonarr'];
      toast(`${r.removed} fichier(s) retiré(s)` + (relaunch ? ' · ' + relaunch : ''), 6000);
    }
    loadLibrary();
  } catch (e) { toast('Erreur : ' + e.message, 6000); }
}

async function openEpisodes(name) {
  const m = MOVIE();
  const d = await api('GET', m ? '/api/movies/' + encodeURIComponent(name) + '/files' : '/api/series/' + encodeURIComponent(name) + '/episodes');
  const rows = (m ? d.files : d.episodes).map(e => `<tr><td>${esc(e.file)}</td><td>${fmtSize(e.size)}</td><td>${esc(e.codec || '—')}${e.height ? ' ' + e.height + 'p' : ''}</td><td>${fmtDur(e.duration)}</td>
    <td>${e.job ? `<span class="st ${esc(e.job.status)}">${esc(stLabel(e.job.status))}${e.job.status === 'encoding' ? ' ' + Math.round(e.job.progress) + '%' : ''}</span>` : ''}</td>
    <td>${e.issues.map(i => `<div class="ep-issue">⚠ ${esc(i)}</div>`).join('')}</td></tr>`).join('');
  await dialog(`<h3>${esc(name)}</h3><table class="tbl"><tr><th>Fichier</th><th>Taille</th><th>Codec</th><th>Durée</th><th>Encodage</th><th>Problèmes</th></tr>${rows}</table>`, [{ label: 'Fermer', value: true }]);
}

// ------------------------------------------------------------------ file d'attente
async function loadQueue() {
  const d = await api('GET', '/api/queue');
  S.paused = d.paused; S.queued = d.queued;
  $('#pauseBtn').textContent = S.paused ? 'Reprendre la file' : 'Mettre en pause';
  const act = d.jobs.filter(j => ['queued', 'claimed', 'encoding', 'uploading', 'verifying'].includes(j.status)).length;
  $('#queueInfo').textContent = S.paused ? 'File en pause : aucun nouveau fichier n\'est distribué.' : act + ' fichier(s) en cours ou en attente';
  const h = JSON.stringify(d.jobs); if (S.hash.queue === h) return; S.hash.queue = h;
  $('#queueTbl').innerHTML = '<tr><th>Série / film</th><th>Fichier</th><th>État</th><th>Progression</th><th>Host / GPU</th><th>Taille</th><th></th></tr>' +
    (d.jobs.length ? '' : '<tr class="empty-row"><td colspan="7">La file est vide. Sélectionnez des séries ou des films, puis « Encoder en HEVC ».</td></tr>') +
    d.jobs.map(j => `<tr><td>${j.kind === 'movie' ? '<span class="kind" title="Film">🎬</span> ' : ''}${esc(j.series)}</td><td>${esc(j.file)}</td>
      <td><span class="st ${esc(j.status)}">${esc(stLabel(j.status))}</span>${j.phase && ['claimed', 'encoding', 'uploading'].includes(j.status) ? ` <small class="muted">${PHASE[j.phase] !== stLabel(j.status) ? esc(PHASE[j.phase] || j.phase) + ' ' : ''}${esc(j.speed)}</small>` : ''}${j.error ? `<div class="ep-issue">${esc(j.error)}</div>` : ''}</td>
      <td>${['encoding', 'uploading', 'claimed', 'verifying'].includes(j.status) ? `<div class="bar run" title="${Math.round(j.progress)} %"><i style="width:${j.progress}%"></i></div>` : ''}</td>
      <td>${j.host ? esc(j.host) + ' · ' + (j.gpu === 'cpu' ? 'CPU' : 'GPU ' + esc(j.gpu)) : ''}</td>
      <td>${fmtSize(j.in_size)}${j.out_size ? ' → ' + fmtSize(j.out_size) : ''}</td>
      <td><div class="row-acts">${j.status === 'queued' ? `<button class="btn small" data-prio="${j.id}" title="Passe ce fichier en tête de la file d'attente">Prioriser</button><button class="btn small" data-force="${j.id}" title="Le lance tout de suite : si aucun GPU n'est libre, l'encodage le moins avancé est interrompu et remis en file juste derrière">Forcer</button>` : ''}${['queued', 'claimed', 'encoding', 'uploading'].includes(j.status) ? `<button class="btn small" data-cancel="${j.id}">Annuler</button>` : ''}${['failed', 'canceled', 'skipped'].includes(j.status) ? `<button class="btn small" data-retry="${j.id}">Relancer</button>` : ''}</div></td></tr>`).join('');
}
$('#queueTbl').onclick = async e => {
  const { cancel: c, retry: r, prio: p, force: f } = e.target.dataset;
  try {
    if (c) await api('POST', `/api/jobs/${c}/cancel`);
    if (r) await api('POST', `/api/jobs/${r}/retry`);
    if (p || f) {
      const res = await api('POST', `/api/jobs/${p || f}/prioritize`, { force: !!f });
      toast(res.paused ? 'En tête de file, mais la file est en pause : reprenez-la pour le lancer.'
        : res.preempted ? 'Lancement forcé : un encodage en cours est interrompu et remis en file juste derrière.'
        : f ? 'Lancement forcé : il sera pris par le prochain GPU libre.' : 'Mis en tête de la file d\'attente.', 5000);
    }
    S.hash.queue = ''; loadQueue();
  } catch (err) { toast(err.message); }
};
$('#pauseBtn').onclick = async () => { await api('PUT', '/api/settings', { paused: !S.paused }); S.hash.queue = ''; loadQueue(); };

// ------------------------------------------------------------------ hosts
async function loadHosts() {
  const d = await api('GET', '/api/hosts'); const h = JSON.stringify(d.hosts);
  if (S.hash.hosts === h) return; S.hash.hosts = h;
  if (document.activeElement && document.activeElement.closest('#hostList') && document.activeElement.type === 'checkbox') return;
  $('#hostList').innerHTML = d.hosts.length ? d.hosts.map(hs => `<div class="host" data-id="${esc(hs.id)}">
    <h3 class="host-head"><span class="dot ${hs.online ? 'on' : ''}" title="${hs.online ? 'en ligne' : 'hors ligne'}"></span>${esc(hs.name)}<span class="grow"></span>
      <label class="sort small-row"><input type="checkbox" data-enabled ${hs.enabled ? 'checked' : ''}> actif</label></h3>
    <div class="muted">${hs.online ? 'en ligne' : 'hors ligne'} · ${esc(hs.platform || '')} · agent ${esc(hs.agent_version || '?')} ${hs.reusable_key ? ' · machine non persistante' : ''} · NVENC ${hs.nvenc ? 'oui' : '<span class="warn-txt">ne fonctionne pas</span>'}</div>${!hs.nvenc && hs.nvenc_error ? `<div class="ep-issue">⚠ ${esc(hs.nvenc_error)} — les GPU de ce host ne reçoivent aucun épisode.</div>` : ''}
    ${hs.gpus.map(g => `<label class="gpu"><input type="checkbox" data-gpu="${esc(g.id)}" ${hs.gpus_enabled.includes(String(g.id)) ? 'checked' : ''}>
      <span><b>${g.id === 'cpu' ? 'CPU' : 'GPU ' + esc(g.id)}</b> ${esc(g.name)}${g.mem_total ? `<small>${esc(g.mem_used)}/${esc(g.mem_total)} Mo de VRAM utilisés</small>` : '<small>encodage logiciel : lent, à éviter sauf besoin</small>'}
      ${hs.running.filter(r => String(r.gpu) === String(g.id)).map(r => `<small>▶ ${esc(r.series)} — ${esc(r.file)} (${Math.round(r.progress)} %, ${esc(PHASE[r.phase] || r.phase)})</small>`).join('')}</span></label>`).join('')}
    ${hostUpdate(hs)}
    <div class="host-acts"><button class="btn small danger" data-del>Retirer ce host</button>${hs.update.outdated && !hs.update.updating ? `<button class="btn small" data-upd ${hs.online ? '' : 'disabled title="host hors ligne"'}>Mettre à jour l'agent (${esc(hs.agent_version)} → ${esc(hs.update.latest)})</button>` : ''}</div></div>`).join('') : '<div class="empty">Aucun host. Cliquez sur « Ajouter un host ».</div>';
}
function hostUpdate(hs) {
  const u = hs.update || {};
  if (u.updating) return `<div class="muted">⟳ Mise à jour de l'agent vers ${esc(u.latest || '?')} en cours : le host finit ses envois puis redémarre, ses encodages en cours repartent en file.</div>`;
  if (u.error) return `<div class="ep-issue">⚠ Mise à jour : ${esc(u.error)}</div>${u.command ? `<code class="cmd">${esc(u.command)}</code>` : ''}`;
  return '';
}
$('#hostList').onchange = async e => {
  const card = e.target.closest('.host'); if (!card) return; const id = card.dataset.id;
  try {
    if (e.target.dataset.enabled !== undefined) await api('PATCH', '/api/hosts/' + id, { enabled: e.target.checked });
    if (e.target.dataset.gpu !== undefined) await api('PATCH', '/api/hosts/' + id, { gpus_enabled: $$('[data-gpu]:checked', card).map(x => x.dataset.gpu) });
    S.hash.hosts = ''; loadHosts();
  } catch (err) { toast(err.message); }
};
$('#hostList').onclick = async e => {
  const card = e.target.closest('.host');
  if (e.target.dataset.upd !== undefined) {
    if (!await ask('Mettre à jour l\'agent', 'Ses encodages en cours sont interrompus et remis en file (sans tentative perdue) ; un envoi déjà commencé va à son terme.', 'Mettre à jour')) return;
    try { await api('POST', '/api/hosts/' + card.dataset.id + '/update'); toast('Mise à jour demandée'); } catch (err) { toast(err.message); }
    S.hash.hosts = ''; return loadHosts();
  }
  if (e.target.dataset.del === undefined) return;
  if (!await ask('Retirer ce host', 'Ses encodages en cours sont remis en file. Pour le rajouter, il faudra relancer une commande d\'installation.', 'Retirer', true)) return;
  await api('DELETE', '/api/hosts/' + card.dataset.id); S.hash.hosts = ''; loadHosts();
};
$('#addHost').onclick = async () => {
  const kind = await dialog(`<h3>Ajouter un host</h3>
    <p>Sur quelle machine installer l'agent ? Il faut un GPU NVIDIA avec son pilote.</p>
    <p class="hint">« Windows non persistant » : VM ou serveur remis à zéro à chaque démarrage. La commande sert à chaque démarrage et retrouve le même host dans cet onglet.</p>`,
    [{ label: 'Linux', value: 'linux', cls: 'primary' }, { label: 'Windows', value: 'windows' },
     { label: 'Windows non persistant', value: 'windows-np' }, { label: 'Annuler', value: null }]);
  if (!kind) return;
  try {
    const r = await api('POST', '/api/hosts/enroll', { os: kind === 'linux' ? 'linux' : 'windows', reusable: kind === 'windows-np' });
    const where = r.os === 'windows'
      ? 'Sur la machine Windows, dans un PowerShell ouvert <b>en administrateur</b>, exécutez cette commande. Elle installe Python (version embarquée), ffmpeg et l\'agent dans <code>C:\\ProgramData\\Reencodearr-agent</code>, puis une tâche planifiée qui le lance au démarrage (compte SYSTEM).'
      : 'Sur la machine Linux, exécutez cette commande. Elle installe l\'agent et un service systemd.';
    const once = r.reusable
      ? `<p>Cette commande est <b>réutilisable</b> : la tâche planifiée la relit sur le hub à chaque démarrage (réinstallation, nouvel enregistrement, même host). Si l'image de la VM ne garde pas la tâche, mettez la commande dans son script de démarrage. Première utilisation sous ${Math.round(r.expires_in / 86400)} jours ; ensuite, seul « Retirer ce host » la révoque. Une commande = une machine : n'en partagez pas une entre plusieurs VM.</p>`
      : `<p>Elle ne marche qu'une seule fois pendant ${Math.round(r.expires_in / 60)} minutes.</p>`;
    await dialog(`<h3>Ajouter un host ${r.os === 'windows' ? 'Windows' : 'Linux'}</h3>
      <p>${where}</p>${once}
      <code class="cmd">${esc(r.command)}</code>
      <p class="hint">L'agent se connecte à ${esc(r.url)} (il n'ouvre aucun port). Si cette adresse n'est pas joignable depuis la machine, renseignez « URL publique du hub » dans Paramètres puis regénérez la commande.</p>`,
      [{ label: 'Copier', value: 'copy', cls: 'primary' }, { label: 'Fermer', value: null }]).then(v => { if (v === 'copy') navigator.clipboard?.writeText(r.command).then(() => toast('Commande copiée')); });
  } catch (e) { toast(e.message); }
};

// ------------------------------------------------------------------ journal
const fmtTime = ts => { const d = new Date(ts * 1000); const p = n => String(n).padStart(2, '0'); return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
function logQuery(limit) {
  const q = new URLSearchParams({ limit });
  if ($('#logLevel').value) q.set('level', $('#logLevel').value);
  if ($('#logSource').value) q.set('source', $('#logSource').value);
  if ($('#logSearch').value.trim()) q.set('q', $('#logSearch').value.trim());
  return q.toString();
}
async function loadLogs() {
  const d = await api('GET', '/api/logs?' + logQuery(400));
  const cur = $('#logSource').value;
  const opts = '<option value="">Toutes les sources</option>' + d.sources.map(s => `<option ${s === cur ? 'selected' : ''}>${esc(s)}</option>`).join('');
  if ($('#logSource').innerHTML !== opts) $('#logSource').innerHTML = opts;
  const h = JSON.stringify(d.logs); if (S.hash.logs === h) return; S.hash.logs = h;
  $('#logTbl').innerHTML = '<tr><th>Heure</th><th>Niveau</th><th>Source</th><th>Message</th></tr>' +
    d.logs.map(l => `<tr class="lvl-${esc(l.level)}"><td>${fmtTime(l.ts)}</td><td><span class="lv ${esc(l.level)}">${esc(l.level)}</span></td><td>${esc(l.source)}</td><td>${esc(l.message)}</td></tr>`).join('');
  $('#logInfo').textContent = d.logs.length + ' ligne(s) affichée(s), les plus récentes en premier. Le journal garde les 5 000 dernières lignes.';
}
['#logLevel', '#logSource'].forEach(s => $(s).onchange = () => { S.hash.logs = ''; loadLogs(); });
$('#logSearch').oninput = () => { clearTimeout(loadLogs.t); loadLogs.t = setTimeout(() => { S.hash.logs = ''; loadLogs(); }, 300); };
$('#logExport').onclick = async () => {
  const d = await api('GET', '/api/logs?' + logQuery(2000));
  const txt = d.logs.slice().reverse().map(l => `${new Date(l.ts * 1000).toISOString()} ${l.level.toUpperCase().padEnd(5)} ${l.source.padEnd(8)} ${l.message}`).join(String.fromCharCode(10));
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([txt], { type: 'text/plain' })); a.download = 'reencodearr-journal.txt'; a.click();
};
$('#logClear').onclick = async () => {
  if (!await ask('Vider le journal', 'Toutes les lignes du journal sont supprimées.', 'Vider', true)) return;
  const r = await api('DELETE', '/api/logs?confirm=yes'); toast(r.removed + ' ligne(s) supprimée(s)'); S.hash.logs = ''; loadLogs();
};

// ------------------------------------------------------------------ paramètres
const PRESETS = { 'Haute qualité (CQ 24)': 24, 'Équilibré (CQ 28)': 28, 'Compact (CQ 32)': 32 };
function statsCard(st) {
  if (!st || !st.done) return `<div class="card wide"><h3>Espace gagné</h3>
    <p class="hint">Aucun encodage terminé pour l'instant. Dès qu'un épisode ou un film est encodé et validé, le gain apparaît ici.</p></div>`;
  const days = st.daily; let cum = 0; const pts = days.map(d => (cum += d.saved, { day: d.day, v: cum }));
  const W = 560, H = 140, P = 6, max = Math.max(1, cum);
  const x = i => P + (pts.length < 2 ? (W - 2 * P) / 2 : i * (W - 2 * P) / (pts.length - 1));
  const y = v => H - P - v * (H - 2 * P) / max;
  const line = pts.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.v).toFixed(1)).join('');
  const area = pts.length ? line + `L${x(pts.length - 1).toFixed(1)} ${H - P}L${x(0).toFixed(1)} ${H - P}Z` : '';
  const fd = t => new Date(t * 1000).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
  const chart = pts.length > 1
    ? `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Espace gagné cumulé">
        <path d="${area}" fill="var(--green)" opacity=".18"/><path d="${line}" fill="none" stroke="var(--green)" stroke-width="2.2" vector-effect="non-scaling-stroke"/></svg>
       <div class="chartx"><span>${fd(pts[0].day)}</span><span>cumul : ${fmtSize(cum)}</span><span>${fd(pts[pts.length - 1].day)}</span></div>`
    : `<p class="hint">Le graphique apparaîtra dès que des encodages auront été validés sur au moins deux jours.</p>`;
  const top = st.series.map(r => `<div class="srow"><span class="sname" title="${esc(r.name)}">${r.movie ? '🎬 ' : ''}${esc(r.name)}</span>
      <div class="bar"><i style="width:${Math.max(2, 100 * r.saved / Math.max(1, st.series[0].saved))}%"></i></div>
      <span class="sval">−${fmtSize(r.saved)} <small>(${r.n} fich. · ${Math.round(100 * (1 - r.after / r.before))} %)</small></span></div>`).join('');
  const td = st.todo, hrs = st.busy_s / 3600;
  return `<div class="card wide"><h3>Espace gagné</h3>
    <div class="kpis">
      <div class="kpi"><b class="big">${fmtSize(st.saved)}</b><span>gagnés</span></div>
      <div class="kpi"><b>−${st.percent} %</b><span>${fmtSize(st.before)} → ${fmtSize(st.after)}</span></div>
      <div class="kpi"><b>${st.done}</b><span>fichier${st.done > 1 ? 's' : ''} encodé${st.done > 1 ? 's' : ''}</span></div>
      <div class="kpi"><b>${hrs >= 1 ? hrs.toFixed(1) + ' h' : Math.round(st.busy_s / 60) + ' min'}</b><span>d’encodage cumulées</span></div>
    </div>
    ${chart}
    ${top ? `<h4>Plus gros gains par série ou film</h4>${top}` : ''}
    ${st.hosts.length > 1 ? `<h4>Par machine</h4><p class="hint">${st.hosts.map(h => `${esc(h.name)} : ${h.n} fich. (−${fmtSize(h.saved)})`).join(' · ')}</p>` : ''}
    ${td.files ? `<div class="todo">Il reste <b>${td.files}</b> fichier${td.files > 1 ? 's' : ''} à encoder (${fmtSize(td.size)}). Au rythme mesuré (−${Math.round(100 * (1 - td.ratio_used))} %${td.measured ? '' : ', estimation par défaut'}), cela libérerait environ <b>${fmtSize(td.estimated_saving)}</b> de plus.</div>` : ''}
    <p class="hint">Calculé sur les encodages validés par Reencodearr (taille avant / après). Les anciens encodages faits hors de l'application ne sont pas comptés.</p></div>`;
}
async function loadStats() {
  const st = await api('GET', '/api/stats').catch(() => null);
  const h = JSON.stringify(st); if (S.hash.stats === h) return; S.hash.stats = h;
  $('#statsBody').innerHTML = statsCard(st);
}
async function loadSettings() {
  const d = await api('GET', '/api/settings');
  if (!S.sections) S.sections = (await api('GET', '/api/settings/sections').catch(() => ({ sections: [] }))).sections;
  const s = d.settings; S.settings = s; const q = s.quality;
  const opt = (v, cur, label) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${label || v}</option>`;
  $('#settingsForm').innerHTML = `
  <div class="card"><h3>Qualité d'encodage</h3>
    <div class="presets">${Object.entries(PRESETS).map(([l, v]) => `<button class="btn small" data-preset="${v}">${l}</button>`).join('')}</div>
    <label>CQ (plus bas = meilleure qualité, fichier plus gros) <input id="q_cq" type="number" min="0" max="51" value="${q.cq}"></label>
    <label>Preset NVENC <select id="q_preset">${['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'].map(p => opt(p, q.preset, p + (p === 'p1' ? ' (rapide)' : p === 'p7' ? ' (le plus lent / meilleur)' : ''))).join('')}</select></label>
    <label>Réglage NVENC (tune) <select id="q_tune">${opt('hq', q.tune, 'hq : haute qualité (recommandé)')}${opt('ll', q.tune, 'll : faible latence')}${opt('ull', q.tune, 'ull : latence minimale')}</select></label>
    <label>Lookahead (images) <input id="q_look" type="number" min="0" max="64" value="${q.lookahead}"></label>
    <label>Images B <input id="q_bf" type="number" min="0" max="4" value="${q.bframes}"></label>
    <label>Plafond de débit (% du débit de la source, 0 = aucun) <input id="q_cap" type="number" min="0" max="300" value="${q.maxrate_pct}"></label>
    <label>CRF si encodage logiciel (CPU) <input id="q_crf" type="number" min="0" max="51" value="${q.cpu_crf}"></label>
    <label>Preset x265 si encodage logiciel (CPU) <select id="q_cpu_preset">${['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'].map(p => opt(p, q.cpu_preset)).join('')}</select></label>
    <p class="hint">Audio, sous-titres, chapitres et pièces jointes sont toujours copiés tels quels. Mesure sur des sources WEB : CQ 28 ≈ −55 %, Blu-ray ≈ −40 %.</p></div>
  <div class="card"><h3>Remplacement</h3>
    <label>Après un encodage validé <select id="replace_mode">${opt('replace', s.replace_mode, "Remplacer l'original (le supprimer)")}${opt('quarantine', s.replace_mode, "Garder l'original en quarantaine (.quarantaine/originaux)")}</select></label>
    <label class="row"><input type="checkbox" id="rn_en" ${s.rename.enabled ? 'checked' : ''}> Renommer H264/x264 → HEVC</label>
    <label class="row"><input type="checkbox" id="rn_tag" ${s.rename.strip_tag ? 'checked' : ''}> Retirer le tag de release final (-TG, -SUPPLY…)</label>
    <label class="row"><input type="checkbox" id="rn_pad" ${s.rename.pad3 ? 'checked' : ''}> Normaliser E010 → E10</label>
    <label>Tolérance de durée (secondes) <input id="tol" type="number" step="0.5" min="0" value="${s.tolerance_s}"></label>
    <label>Gain minimal exigé (%) <input id="gain" type="number" min="0" max="90" value="${s.min_gain_pct}"></label>
    <label class="row"><input type="checkbox" id="av1_en" ${s.av1.reencode ? 'checked' : ''}> Réencoder aussi les sources AV1 en HEVC (compatibilité)</label>
    <label>AV1 → HEVC : hausse de taille acceptée (%) <input id="av1_grow" type="number" min="0" max="1000" value="${s.av1.max_growth_pct}"></label>
    <p class="hint">Le HEVC est moins efficace que l'AV1 : le fichier encodé peut être plus gros que l'original. Il est accepté s'il grossit d'au plus ce pourcentage (0 = il ne doit pas grossir) ; le gain minimal ne s'applique pas à ces fichiers. Sinon, l'original est conservé.</p>
    <label>Tentatives par fichier <input id="att" type="number" min="1" max="10" value="${s.max_attempts}"></label></div>
  <div class="card"><h3>Scan et fichiers défectueux</h3>
    <label>Niveau de scan par défaut <select id="scan_level">${opt('quick', s.scan.level, 'Rapide (relecture du conteneur)')}${opt('full', s.scan.level, 'Complet (décodage vidéo)')}</select></label>
    <label>Retrait des fichiers défectueux <select id="bad_mode">${opt('quarantine', s.bad_delete_mode, 'Quarantaine (déplacés, récupérables)')}${opt('delete', s.bad_delete_mode, 'Suppression définitive')}</select></label>
    <label>Fichiers scannés en parallèle <input id="scan_conc" type="number" min="1" max="16" value="${s.scan.concurrency}"></label>
    <div class="hint">Quarantaine : ${d.quarantine.files} fichier(s), ${fmtSize(d.quarantine.size)}.</div>
    <div><button class="btn small danger" id="purge" ${d.quarantine.files ? '' : 'disabled'}>Vider la quarantaine (définitif)</button></div></div>
  <div class="card"><h3>Sonarr</h3>
    <label>URL <input id="sn_url" placeholder="http://sonarr:8989" value="${esc(s.sonarr.url)}"></label>
    <label>Clé API <input id="sn_key" type="password" value="${esc(s.sonarr.api_key)}" autocomplete="off"></label>
    <label class="row"><input type="checkbox" id="sn_unmon" ${s.sonarr.unmonitor_done ? 'checked' : ''}> Désactiver le suivi d'une série dès qu'elle est entièrement encodée</label>
    <div><button class="btn small" id="sn_test">Tester la connexion</button></div>
    <p class="hint">Sert aux affiches et, après « Supprimer les défectueux », à rescanner la série puis relancer la recherche.</p></div>
  <div class="card"><h3>Radarr</h3>
    <label>URL <input id="ra_url" placeholder="http://radarr:7878" value="${esc(s.radarr.url)}"></label>
    <label>Clé API <input id="ra_key" type="password" value="${esc(s.radarr.api_key)}" autocomplete="off"></label>
    <label class="row"><input type="checkbox" id="ra_unmon" ${s.radarr.unmonitor_done ? 'checked' : ''}> Désactiver le suivi d'un film dès qu'il est entièrement encodé</label>
    <div><button class="btn small" id="ra_test">Tester la connexion</button></div>
    <p class="hint">Sert aux affiches des films et, après « Supprimer les défectueux », à rescanner le film puis relancer sa recherche.</p></div>
  <div class="card"><h3>Affiches (séries et films)</h3>
    <label>Clé TMDB (clé API v3 ou jeton de lecture v4) <input id="tm_key" type="password" value="${esc(s.posters.tmdb_key)}" autocomplete="off" placeholder="themoviedb.org → Paramètres → API"></label>
    <label>Clé OMDb (affiches IMDb, optionnel) <input id="om_key" type="password" value="${esc(s.posters.omdb_key)}" autocomplete="off" placeholder="omdbapi.com/apikey.aspx"></label>
    <label>Langue des affiches <input id="po_lang" value="${esc(s.posters.language)}" placeholder="fr-FR"></label>
    <div class="presets"><button class="btn small" id="po_test">Tester les clés</button><button class="btn small" id="po_clear">Vider le cache des affiches</button></div>
    <p class="hint">Ordre : cache disque (30 jours, échecs 1 jour) → <code>poster.jpg</code> du dossier → Sonarr (séries) ou Radarr (films) → TMDB → OMDb. Les affiches sont téléchargées une seule fois, le titre vient du nom du dossier (« Titre (2013) » aide à choisir le bon film).</p></div>
  <div class="card"><h3>Réseau et agents</h3>
    <label>URL publique du hub (jointe par les agents) <input id="pub" placeholder="http://adresse-du-hub:8095" value="${esc(s.public_url)}"></label>
    <label>Bail d'un agent sans nouvelles (secondes) <input id="lease" type="number" min="60" max="86400" value="${s.lease_s}"></label>
    <p class="hint">Si un agent ne donne plus de nouvelles pendant ce délai, son fichier repart en file pour un autre GPU. Les agents récents envoient un signe de vie toutes les 15 s, même pendant un long contrôle ; 300 s suffit en général.</p></div>
  <div class="card"><h3>Mot de passe</h3>
    <label>Ancien <input id="pw_old" type="password" autocomplete="current-password"></label>
    <label>Nouveau (8 caractères minimum) <input id="pw_new" type="password" autocomplete="new-password"></label>
    <div><button class="btn small" id="pw_btn">Changer le mot de passe</button></div></div>
  <div class="card"><h3>Sauvegarde de la configuration</h3>
    <p class="hint">Exporte les réglages choisis dans un fichier JSON, à réimporter ici ou dans l'assistant d'une nouvelle installation. Les comptes, les hosts et l'historique des encodages n'en font pas partie.</p>
    <div class="checks">${S.sections.map(x => `<label class="row"><input type="checkbox" class="bk_sec" value="${x.id}" checked> ${esc(x.label)}</label>`).join('')}</div>
    <label class="row"><input type="checkbox" id="bk_secrets"> Inclure les clés API (Sonarr, Radarr, TMDB, OMDb)</label>
    <p class="hint">Sans les clés, le fichier peut être partagé sans risque ; à l'import, les clés déjà enregistrées sont gardées. Avec les clés, conservez le fichier en lieu sûr : il donne accès à Sonarr et Radarr.</p>
    <div class="presets"><button class="btn small" id="bk_export">Exporter (.json)</button><button class="btn small" id="bk_import">Importer un fichier…</button></div></div>
  <div class="card"><h3>Assistant de configuration</h3>
    <p class="hint">Reprend pas à pas Sonarr, Radarr, les affiches et l'adresse du hub. Le compte administrateur n'est pas touché.</p>
    <div><button class="btn small" id="setup_again">Relancer l'assistant</button></div></div>
  <div class="savebar"><span class="hint">Les réglages d'encodage s'appliquent aux prochains fichiers mis en file.</span><button class="btn primary" id="saveAll">Enregistrer les paramètres</button></div>`;
}
$('#settingsForm').onclick = async e => {
  const id = e.target.id;
  try {
    if (e.target.dataset.preset) $('#q_cq').value = e.target.dataset.preset;
    if (id === 'saveAll') {
      await api('PUT', '/api/settings', {
        quality: { cq: +$('#q_cq').value, preset: $('#q_preset').value, tune: $('#q_tune').value, lookahead: +$('#q_look').value, bframes: +$('#q_bf').value, cpu_crf: +$('#q_crf').value, cpu_preset: $('#q_cpu_preset').value, maxrate_pct: +$('#q_cap').value },
        replace_mode: $('#replace_mode').value, bad_delete_mode: $('#bad_mode').value,
        rename: { enabled: $('#rn_en').checked, strip_tag: $('#rn_tag').checked, pad3: $('#rn_pad').checked },
        tolerance_s: +$('#tol').value, min_gain_pct: +$('#gain').value, max_attempts: +$('#att').value,
        av1: { reencode: $('#av1_en').checked, max_growth_pct: +$('#av1_grow').value },
        scan: { level: $('#scan_level').value, concurrency: +$('#scan_conc').value }, lease_s: +$('#lease').value, sonarr: { url: $('#sn_url').value, api_key: $('#sn_key').value, unmonitor_done: $('#sn_unmon').checked }, radarr: { url: $('#ra_url').value, api_key: $('#ra_key').value, unmonitor_done: $('#ra_unmon').checked }, posters: { tmdb_key: $('#tm_key').value, omdb_key: $('#om_key').value, language: $('#po_lang').value }, public_url: $('#pub').value.trim()
      });
      toast('Paramètres enregistrés.'); loadSettings();
    }
    if (id === 'sn_test') {
      await api('PUT', '/api/settings', { sonarr: { url: $('#sn_url').value, api_key: $('#sn_key').value, unmonitor_done: $('#sn_unmon').checked } });
      const r = await api('POST', '/api/settings/sonarr-test'); toast(`Sonarr ${r.version} : ${r.series} séries`);
    }
    if (id === 'ra_test') {
      await api('PUT', '/api/settings', { radarr: { url: $('#ra_url').value, api_key: $('#ra_key').value, unmonitor_done: $('#ra_unmon').checked } });
      const r = await api('POST', '/api/settings/radarr-test'); toast(`Radarr ${r.version} : ${r.movies} films`);
    }
    if (id === 'bk_export') {
      const secs = $$('.bk_sec:checked').map(x => x.value);
      if (!secs.length) return toast('Cochez au moins une catégorie');
      const n = await exportConfig(secs, $('#bk_secrets').checked);
      toast(`Configuration exportée (${n} catégorie${n > 1 ? 's' : ''})`);
    }
    if (id === 'bk_import') {
      const r = await importConfig(await pickFile());
      if (r) { toast(`Configuration importée : ${r.applied.length} catégorie${r.applied.length > 1 ? 's' : ''}`); S.hash.library = ''; loadSettings(); }
    }
    if (id === 'setup_again') {
      await api('POST', '/api/setup/restart'); clearTimeout(timer); return boot();
    }
    if (id === 'po_test') {
      await api('PUT', '/api/settings', { posters: { tmdb_key: $('#tm_key').value, omdb_key: $('#om_key').value, language: $('#po_lang').value } });
      const r = await api('POST', '/api/settings/posters-test'); toast('Affiches : ' + Object.entries(r).map(([k, v]) => k.toUpperCase() + ' ' + v).join(' · '), 6000);
    }
    if (id === 'po_clear') { const r = await api('DELETE', '/api/posters/cache'); toast(r.removed + ' fichier(s) du cache supprimé(s)'); S.hash.library = ''; }
    if (id === 'pw_btn') { await api('POST', '/api/password', { old: $('#pw_old').value, new: $('#pw_new').value }); toast('Mot de passe changé, reconnexion…'); setTimeout(showLogin, 1200); }
    if (id === 'purge') {
      if (!await ask('Vider la quarantaine', '<b>Tout le contenu de la quarantaine est supprimé définitivement.</b>', 'Supprimer définitivement', true)) return;
      const r = await api('DELETE', '/api/quarantine?confirm=yes'); toast(r.files + ' fichier(s) supprimé(s)'); loadSettings();
    }
  } catch (err) { toast('Erreur : ' + err.message, 6000); }
};

boot().catch(showLogin);
