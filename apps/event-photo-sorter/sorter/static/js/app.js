let currentFile = null;
let cooldownUntil = 0;
let bulkMode = false;
let bulkSelected = new Set();
const preloadedUrls = new Set();
let mySection = 1, mySections = 1;
let isHost = false;
let setupModalInstance = null;
let welcomeModalInstance = null;

// ---------- theme (light/dark, navy-blue brand) ----------
function applyTheme(theme) {
  document.documentElement.setAttribute('data-bs-theme', theme);
  document.getElementById('themeIcon').className = theme === 'light' ? 'bi bi-moon-stars' : 'bi bi-sun';
  localStorage.setItem('theme', theme);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-bs-theme') || 'light';
  applyTheme(cur === 'light' ? 'dark' : 'light');
}
applyTheme(document.documentElement.getAttribute('data-bs-theme') || 'light');

// ---------- multiplayer: server-assigned player numbers ----------
function getClientId() {
  let id = localStorage.getItem('clientId');
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() :
      'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
      }));
    localStorage.setItem('clientId', id);
  }
  return id;
}

async function joinRoom(requestedSections) {
  const body = {client_id: getClientId()};
  if (requestedSections !== undefined) body.requested_sections = requestedSections;
  const r = await api('/api/join', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  isHost = r.is_host;
  if (!r.room_full) {
    mySection = r.section; mySections = r.sections;
  }
  return r;
}

async function bootMultiplayer() {
  const r = await joinRoom();
  if (r.room_full) {
    new bootstrap.Modal(document.getElementById('roomFullModal')).show();
    return;
  }
  updateSectionBadge();
  document.getElementById('clearAllBtn').classList.toggle('d-none', !isHost);
  if (isHost) {
    document.getElementById('setupSections').value = mySections;
    setupModalInstance = new bootstrap.Modal(document.getElementById('setupModal'));
    setupModalInstance.show();
  } else if (mySections > 1) {
    document.getElementById('welcomePlayerLabel').textContent = `Player ${mySection} of ${mySections}`;
    welcomeModalInstance = new bootstrap.Modal(document.getElementById('welcomeModal'));
    welcomeModalInstance.show();
  } else {
    refreshState();
  }
}

function retryJoin() {
  const el = bootstrap.Modal.getInstance(document.getElementById('roomFullModal'));
  if (el) el.hide();
  bootMultiplayer();
}

function reopenSetup() {
  if (!isHost) {
    setStatus('Only the host machine (the PC running the server) can change the room size.');
    return;
  }
  const openSetup = () => {
    document.getElementById('setupSections').value = mySections;
    setupModalInstance = bootstrap.Modal.getOrCreateInstance(document.getElementById('setupModal'));
    setupModalInstance.show();
  };
  const settingsModalEl = document.getElementById('settingsModal');
  const settingsInstance = bootstrap.Modal.getInstance(settingsModalEl);
  if (settingsInstance && settingsModalEl.classList.contains('show')) {
    // wait for the fade-out + backdrop cleanup to finish before opening the
    // next modal -- opening immediately after hide() races Bootstrap's own
    // transition and can leave the new modal invisible behind a stale backdrop
    settingsModalEl.addEventListener('hidden.bs.modal', openSetup, {once: true});
    settingsInstance.hide();
  } else {
    openSetup();
  }
}

async function confirmSetup() {
  let sections = parseInt(document.getElementById('setupSections').value, 10) || 1;
  sections = Math.max(1, Math.min(16, sections));
  await joinRoom(sections);  // saves server-side, never errors -- reassigns anyone who no longer fits
  if (setupModalInstance) setupModalInstance.hide();
  updateSectionBadge();
  refreshState();
}

function confirmWelcome() {
  if (welcomeModalInstance) welcomeModalInstance.hide();
  refreshState();
}

function updateSectionBadge() {
  const el = document.getElementById('sectionBadge');
  if (mySections > 1) {
    el.textContent = `You: Player ${mySection}/${mySections}`;
    el.classList.remove('d-none');
  } else {
    el.classList.add('d-none');
    document.getElementById('myProgressBadge').classList.add('d-none');
  }
}

function updateMyProgress(reviewed, total) {
  const el = document.getElementById('myProgressBadge');
  if (mySections <= 1 || total === undefined) {
    el.classList.add('d-none');
    return;
  }
  const pct = total ? Math.round((reviewed / total) * 100) : 0;
  el.textContent = `Your section: ${reviewed}/${total} (${pct}%)`;
  el.classList.remove('d-none');
}

// ---------- resizable main preview (+/- buttons) ----------
function applyPhotoSize(vh) {
  vh = Math.max(30, Math.min(85, vh));
  document.documentElement.style.setProperty('--photo-h', vh + 'vh');
  localStorage.setItem('photoH', vh);
}
function changePhotoSize(delta) {
  const cur = parseInt(localStorage.getItem('photoH'), 10) || 58;
  applyPhotoSize(cur + delta);
}
applyPhotoSize(parseInt(localStorage.getItem('photoH'), 10) || 58);

// ---------- layout toggle: category strip above or below the preview ----------
const LAYOUT_MODES = ['normal', 'swapped', 'side'];  // category/top, preview/top, side-by-side
function applyLayout(mode) {
  if (!LAYOUT_MODES.includes(mode)) mode = 'normal';
  const el = document.getElementById('layoutFlex');
  el.classList.remove('layout-swapped', 'layout-side');
  if (mode === 'swapped') el.classList.add('layout-swapped');
  if (mode === 'side') el.classList.add('layout-side');
  localStorage.setItem('layoutMode', mode);

  // bulk mode's grid needs more room than the side-by-side split gives the
  // preview pane -- keep it off the table in that layout, and back out of
  // it automatically if you were already in bulk mode when you switched
  const bulkBtn = document.getElementById('bulkModeBtn');
  const sideActive = mode === 'side';
  bulkBtn.disabled = sideActive;
  bulkBtn.classList.toggle('d-none', sideActive);
  if (sideActive && typeof bulkMode !== 'undefined' && bulkMode) {
    toggleBulkMode();
  }
}
function toggleLayout() {
  const cur = localStorage.getItem('layoutMode') || 'normal';
  const next = LAYOUT_MODES[(LAYOUT_MODES.indexOf(cur) + 1) % LAYOUT_MODES.length];
  applyLayout(next);
  const labels = {normal: 'Category on top', swapped: 'Preview on top', side: 'Side by side (picture left, categories right)'};
  setStatus(`Layout: ${labels[next]}`);
}
applyLayout(localStorage.getItem('layoutMode') || 'normal');

// ---------- resizable bulk-mode thumbnails (+/- buttons) ----------
function applyBulkThumbSize(w) {
  w = Math.max(60, Math.min(260, w));
  const h = Math.round(w * 0.82);
  document.documentElement.style.setProperty('--bulk-thumb-w', w + 'px');
  document.documentElement.style.setProperty('--bulk-thumb-h', h + 'px');
  localStorage.setItem('bulkThumbW', w);
}
function changeBulkThumbSize(delta) {
  const cur = parseInt(localStorage.getItem('bulkThumbW'), 10) || 110;
  applyBulkThumbSize(cur + delta);
}
applyBulkThumbSize(parseInt(localStorage.getItem('bulkThumbW'), 10) || 110);

// ---------- resizable side-by-side split (drag the vertical handle left/right) ----------
function applySideSplit(pct) {
  pct = Math.max(25, Math.min(75, pct));
  document.documentElement.style.setProperty('--side-split', pct + '%');
  localStorage.setItem('sideSplit', pct);
}

function initSideResize() {
  applySideSplit(parseInt(localStorage.getItem('sideSplit'), 10) || 60);

  const handle = document.getElementById('sideResizeHandle');
  const flexEl = document.getElementById('layoutFlex');
  let dragging = false;

  function pointerX(e) { return e.touches ? e.touches[0].clientX : e.clientX; }

  function onStart(e) {
    dragging = true;
    handle.classList.add('dragging-active');
    e.preventDefault();
  }
  function onMove(e) {
    if (!dragging) return;
    const rect = flexEl.getBoundingClientRect();
    const pct = ((pointerX(e) - rect.left) / rect.width) * 100;
    applySideSplit(pct);
    e.preventDefault();
  }
  function onEnd() {
    dragging = false;
    handle.classList.remove('dragging-active');
  }

  handle.addEventListener('mousedown', onStart);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onEnd);
  handle.addEventListener('touchstart', onStart, {passive: false});
  window.addEventListener('touchmove', onMove, {passive: false});
  window.addEventListener('touchend', onEnd);
}

// ---------- resizable category strip (drag the handle up/down) ----------
function applyStripSize(thumbH) {
  thumbH = Math.max(50, Math.min(240, thumbH));
  const cardW = Math.round(thumbH * 1.35 + 20);
  document.documentElement.style.setProperty('--cat-thumb-h', thumbH + 'px');
  document.documentElement.style.setProperty('--cat-card-w', cardW + 'px');
  localStorage.setItem('stripThumbH', thumbH);
}

function initStripResize() {
  const saved = parseInt(localStorage.getItem('stripThumbH'), 10);
  applyStripSize(Number.isFinite(saved) ? saved : 90);

  const handle = document.getElementById('stripResizeHandle');
  let dragging = false, startY = 0, startH = 90;

  function pointerY(e) { return e.touches ? e.touches[0].clientY : e.clientY; }

  function onStart(e) {
    dragging = true;
    startY = pointerY(e);
    startH = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--cat-thumb-h'), 10) || 90;
    e.preventDefault();
  }
  function onMove(e) {
    if (!dragging) return;
    const delta = pointerY(e) - startY;
    applyStripSize(startH + delta);
  }
  function onEnd() { dragging = false; }

  handle.addEventListener('mousedown', onStart);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onEnd);
  handle.addEventListener('touchstart', onStart, {passive: false});
  window.addEventListener('touchmove', onMove, {passive: false});
  window.addEventListener('touchend', onEnd);
}

// ---------- live category sync: pick up categories/people created by others ----------
function startCategoryAutoSync() {
  setInterval(async () => {
    if (document.hidden) return;
    const s = await api(`/api/state?${partitionParams()}`);
    const grandPct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
    document.getElementById('progressBadge').textContent = `${s.reviewed} / ${s.total} reviewed (${grandPct}%)`;
    updateProgressBar(s.reviewed, s.total);
    updateMyProgress(s.section_reviewed, s.section_total);
    renderStrip(s.categories);
  }, 3000);
}

// ---------- multiplayer progress modal (polls every 2s while open) ----------
const SECTION_COLORS = ['#38bdf8', '#f472b6', '#facc15', '#4ade80', '#a78bfa',
                         '#fb923c', '#f87171', '#2dd4bf', '#60a5fa', '#e879f9',
                         '#fbbf24', '#34d399', '#c084fc', '#fda4af', '#a3e635', '#7dd3fc'];
let mpInterval = null;
let serverLanUrl = null;

async function getServerLanUrl() {
  if (serverLanUrl) return serverLanUrl;
  const r = await api('/api/server_info');
  serverLanUrl = r.lan_url;
  return serverLanUrl;
}

async function copyMpLanUrl() {
  const el = document.getElementById('mpLanUrlInput');
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value);
}

async function saveRoomSizeFromModal() {
  let sections = parseInt(document.getElementById('mpSectionsInput').value, 10) || 1;
  sections = Math.max(1, Math.min(16, sections));
  await joinRoom(sections);
  updateSectionBadge();
  refreshMultiplayer();
}

document.getElementById('multiplayerModal').addEventListener('shown.bs.modal', async () => {
  const url = await getServerLanUrl();
  document.getElementById('mpLanUrlInput').value = url;
  const canvas = document.getElementById('mpQrCanvas');
  if (window.QRCode) {
    QRCode.toCanvas(canvas, url, {width: 140, margin: 1}, (err) => {
      if (err) console.error('QR code generation failed:', err);
    });
  } else {
    console.error('QRCode library did not load -- check network/CDN access');
  }
  refreshMultiplayer();
  mpInterval = setInterval(refreshMultiplayer, 2000);
});
document.getElementById('multiplayerModal').addEventListener('hidden.bs.modal', () => {
  if (mpInterval) clearInterval(mpInterval);
  mpInterval = null;
});

async function refreshMultiplayer() {
  const hostControls = document.getElementById('mpHostControls');
  const sectionsInput = document.getElementById('mpSectionsInput');
  hostControls.classList.toggle('d-none', !isHost);
  if (isHost && document.activeElement !== sectionsInput) {
    sectionsInput.value = mySections;
  }

  const r = await api(`/api/sections_progress?sections=${mySections}`);
  const grandPct = r.grand_total ? Math.round((r.grand_reviewed / r.grand_total) * 100) : 0;
  document.getElementById('mpGrandLabel').textContent = `${r.grand_reviewed} / ${r.grand_total} (${grandPct}%)`;

  const segBar = document.getElementById('mpSegmentedBar');
  const cards = document.getElementById('mpCards');
  segBar.innerHTML = '';
  cards.innerHTML = '';

  for (const s of r.sections) {
    const color = SECTION_COLORS[(s.section - 1) % SECTION_COLORS.length];
    const pct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
    const shareOfGrand = r.grand_total ? (s.reviewed / r.grand_total) * 100 : 0;

    const seg = document.createElement('div');
    seg.className = 'seg';
    seg.style.width = shareOfGrand + '%';
    seg.style.background = color;
    seg.title = `Section ${s.section}: ${s.reviewed}/${s.total} (${pct}%)`;
    seg.textContent = shareOfGrand > 8 ? s.reviewed : '';
    segBar.appendChild(seg);

    const youTag = (s.section === mySection)
      ? ' <span class="badge text-bg-info" style="font-size:.6rem;">you</span>' : '';
    const card = document.createElement('div');
    card.className = 'mp-card';
    card.style.borderLeftColor = color;
    card.innerHTML = `
      <div class="fw-bold small">Section ${s.section}${youTag}</div>
      <div class="small text-muted">${s.reviewed} / ${s.total} photos &middot; ${pct}%</div>
      <div class="mp-bar-wrap"><div class="mp-bar-fill" style="width:${pct}%; background:${color};"></div></div>
      <div class="small text-muted mt-1 text-truncate">${s.current ? 'on: ' + s.current : 'all done!'}</div>
    `;
    cards.appendChild(card);
  }

  if (r.sections.length <= 1) {
    const hint = document.createElement('div');
    hint.className = 'text-muted small mt-2';
    hint.innerHTML = isHost
      ? 'Solo mode right now -- open <b>Settings</b> and set "how many people total" to add more players.'
      : 'Solo mode right now -- ask whoever is running the server (Settings on their end) to add more players.';
    cards.appendChild(hint);
  }
}

function partitionParams() {
  return `section=${mySection}&sections=${mySections}`;
}

// ---------- settings ----------
function getCooldownMs() {
  const v = parseInt(localStorage.getItem('cooldownMs'), 10);
  return Number.isFinite(v) ? v : 100;
}
function saveSettings() {
  const v = parseInt(document.getElementById('cooldownInput').value, 10);
  localStorage.setItem('cooldownMs', Number.isFinite(v) ? v : 100);
}
function copyLanUrl() {
  const el = document.getElementById('lanUrlInput');
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value);
}

// ---------- click-cooldown guard ----------
function canAct() {
  const now = performance.now();
  const cd = getCooldownMs();
  if (now < cooldownUntil) return false;
  cooldownUntil = now + cd;
  flashCooldown(cd);
  return true;
}
function flashCooldown(ms) {
  if (ms <= 0) return;
  document.querySelectorAll('.toolbar .btn, .cat-card').forEach(el => el.classList.add('is-cooling'));
  setTimeout(() => {
    document.querySelectorAll('.toolbar .btn, .cat-card').forEach(el => el.classList.remove('is-cooling'));
  }, ms);
}

let connectionFailures = 0;
let hostGoneModalInstance = null;

function onConnectionOk() {
  connectionFailures = 0;
  if (hostGoneModalInstance) hostGoneModalInstance.hide();
}

function onConnectionFail() {
  connectionFailures++;
  if (connectionFailures >= 2) {
    hostGoneModalInstance = bootstrap.Modal.getOrCreateInstance(document.getElementById('hostGoneModal'));
    hostGoneModalInstance.show();
  }
}

async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    onConnectionFail();
    throw err;
  }
  if (!res.ok && res.status >= 500) {
    onConnectionFail();
    throw new Error(`server error ${res.status}`);
  }
  onConnectionOk();
  return res.json();
}

function retryConnection() {
  refreshState().catch(() => {});
}
async function apiPost(path, body) {
  return api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...body, section: mySection, sections: mySections})
  });
}

function setStatus(text) { document.getElementById('statusBar').textContent = text; }

function updateProgressBar(reviewed, total) {
  const pct = total ? Math.round((reviewed / total) * 100) : 0;
  document.getElementById('progressBarFill').style.width = pct + '%';
  document.getElementById('progressBarPct').textContent = `${reviewed} / ${total} (${pct}%)`;
}

async function refreshState() {
  const s = await api(`/api/state?${partitionParams()}`);
  const grandPct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
  document.getElementById('progressBadge').textContent = `${s.reviewed} / ${s.total} reviewed (${grandPct}%)`;
  updateProgressBar(s.reviewed, s.total);
  updateSectionBadge();
  updateMyProgress(s.section_reviewed, s.section_total);
  currentFile = s.current;
  const img = document.getElementById('photo');
  const doneMsg = document.getElementById('doneMsg');
  if (s.current) {
    img.classList.remove('d-none');
    doneMsg.classList.add('d-none');
    img.src = `/api/photo/${encodeURIComponent(s.current)}?size=1600`;
    setStatus(`Now showing: ${s.current}`);
    loadPhotoDetails(s.current);
  } else {
    img.classList.add('d-none');
    doneMsg.classList.remove('d-none');
    setStatus('Done! Progress saved.');
    document.getElementById('photoDetails').innerHTML = '';
  }
  renderStrip(s.categories);
  preloadUpcoming(s.upcoming || []);
  if (bulkMode) loadBulkGrid();
}

function formatBytes(n) {
  if (!n) return null;
  return n > 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
}

async function loadPhotoDetails(fname) {
  const el = document.getElementById('photoDetails');
  const info = await api(`/api/photo_info/${encodeURIComponent(fname)}`);
  const chips = [];
  if (info.date) chips.push(`<i class="bi bi-calendar3"></i> ${info.date}`);
  if (info.width && info.height) chips.push(`<i class="bi bi-aspect-ratio"></i> ${info.width}&times;${info.height}`);
  const sizeStr = formatBytes(info.size_bytes);
  if (sizeStr) chips.push(`<i class="bi bi-hdd"></i> ${sizeStr}`);
  if (info.camera) chips.push(`<i class="bi bi-camera2"></i> ${info.camera}`);
  if (info.iso) chips.push(`ISO ${info.iso}`);
  if (info.exposure) chips.push(info.exposure);
  if (info.fnumber) chips.push(info.fnumber);
  if (info.focal_length) chips.push(info.focal_length);
  el.innerHTML = chips.map(c => `<span>${c}</span>`).join('');
}

function preloadUpcoming(upcoming) {
  for (const fname of upcoming) {
    const url = `/api/photo/${encodeURIComponent(fname)}?size=1600`;
    if (preloadedUrls.has(url)) continue;
    preloadedUrls.add(url);
    const img = new Image();
    img.src = url;
  }
}

function categoryLabel(name) {
  if (name.startsWith('people_')) return '#' + name.split('_')[1];
  if (name === 'atmosphere') return 'Atmosphere';
  return name;
}

function buildCategoryCard(c) {
  const card = document.createElement('div');
  card.className = 'cat-card';
  card.onclick = (e) => {
    if (e.target.closest('.mini-btns')) return;
    if (bulkMode) bulkAssignToCategory(c.name);
    else assign(c.name);
  };
  card.addEventListener('mouseenter', (e) => showHoverPreview(c.thumb, e));
  card.addEventListener('mousemove', positionHoverPreview);
  card.addEventListener('mouseleave', hideHoverPreview);
  // touchscreens have no hover -- hold the card to preview it instead of tapping straight through
  let touchPreviewTimer = null, touchPreviewShown = false;
  card.addEventListener('touchstart', (e) => {
    touchPreviewShown = false;
    const t = e.touches[0];
    touchPreviewTimer = setTimeout(() => {
      touchPreviewShown = true;
      showHoverPreview(c.thumb, {clientX: t.clientX, clientY: t.clientY});
    }, 400);
  }, {passive: true});
  card.addEventListener('touchmove', () => clearTimeout(touchPreviewTimer));
  card.addEventListener('touchend', (e) => {
    clearTimeout(touchPreviewTimer);
    hideHoverPreview();
    if (touchPreviewShown) e.preventDefault();  // long-press previewed it, don't also fire the tap
  });
  card.addEventListener('dragover', (e) => { e.preventDefault(); card.classList.add('drag-over'); });
  card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
  card.addEventListener('drop', (e) => {
    e.preventDefault(); card.classList.remove('drag-over');
    if (e.dataTransfer.getData('text/plain') === 'current-photo') assign(c.name);
  });

  const thumbHtml = c.thumb
    ? `<img src="/api/photo/${encodeURIComponent(c.thumb)}?size=200" loading="lazy">`
    : `<div class="noimg">?</div>`;

  card.innerHTML = `
    ${thumbHtml}
    <div class="cat-label"></div>
    <div class="mini-btns">
      <button class="btn btn-info" onclick="openFolder('${escapeJs(c.name)}')" title="Open in Explorer"><i class="bi bi-folder2-open"></i></button>
      <button class="btn btn-primary" onclick="renameCategory('${escapeJs(c.name)}')" title="Rename"><i class="bi bi-pencil"></i></button>
      <button class="btn btn-danger" onclick="deleteCategory('${escapeJs(c.name)}')" title="Delete whole category (back to unsorted)"><i class="bi bi-trash"></i></button>
    </div>`;

  const labelEl = card.querySelector('.cat-label');
  labelEl.textContent = `${categoryLabel(c.name)} (${c.count})`;

  return {el: card, labelEl, thumb: c.thumb, count: c.count};
}

let stripState = {};       // category name -> {el, labelEl, thumb, count}
let stripOrderCache = [];  // category names, in display order

function renderStrip(categories) {
  const outer = document.getElementById('stripOuter');
  const select = document.getElementById('bulkCategorySelect');

  select.innerHTML = '';
  for (const c of categories) {
    const opt = document.createElement('option');
    opt.value = c.name; opt.textContent = categoryLabel(c.name);
    select.appendChild(opt);
  }

  if (!categories.length) {
    if (stripOrderCache.length !== 0) {
      outer.innerHTML = '<div class="text-muted small p-2">No categories yet — click a button below to start</div>';
    }
    stripOrderCache = [];
    stripState = {};
    return;
  }

  const newOrder = categories.map(c => c.name);
  const sameShape = newOrder.length === stripOrderCache.length &&
                     newOrder.every((n, i) => n === stripOrderCache[i]);

  if (sameShape) {
    // nothing was added/removed/reordered -- patch only what changed,
    // in place, so nothing flickers
    for (const c of categories) {
      const prev = stripState[c.name];
      if (!prev) continue;
      if (prev.count !== c.count) {
        prev.labelEl.textContent = `${categoryLabel(c.name)} (${c.count})`;
        prev.count = c.count;
      }
      if (prev.thumb !== c.thumb && c.thumb) {
        const imgEl = prev.el.querySelector('img');
        const url = `/api/photo/${encodeURIComponent(c.thumb)}?size=200`;
        if (imgEl) {
          imgEl.src = url;
        } else {
          const noimg = prev.el.querySelector('.noimg');
          if (noimg) noimg.outerHTML = `<img src="${url}" loading="lazy">`;
        }
        prev.thumb = c.thumb;
      }
    }
    return;
  }

  // categories were added/removed/reordered -- rebuild (rare, so any
  // flicker here is acceptable; routine picks stay on the fast path above)
  const scrollPos = outer.scrollLeft;
  outer.innerHTML = '';
  stripState = {};
  stripOrderCache = newOrder;
  for (const c of categories) {
    const card = buildCategoryCard(c);
    outer.appendChild(card.el);
    stripState[c.name] = card;
  }
  outer.scrollLeft = scrollPos;
}

function escapeJs(s) { return s.replace(/'/g, "\\'"); }

// ---------- hover preview ----------
function showHoverPreview(thumbFile, e) {
  if (!thumbFile) return;
  const box = document.getElementById('hoverPreview');
  document.getElementById('hoverPreviewImg').src = `/api/photo/${encodeURIComponent(thumbFile)}?size=500`;
  box.style.display = 'block';
  positionHoverPreview(e);
}
function positionHoverPreview(e) {
  const box = document.getElementById('hoverPreview');
  box.style.left = (e.clientX + 20) + 'px';
  box.style.top = (e.clientY + 20) + 'px';
}
function hideHoverPreview() {
  document.getElementById('hoverPreview').style.display = 'none';
}

// ---------- lightbox ----------
function openLightbox() {
  if (!currentFile) return;
  document.getElementById('lightboxImg').src = `/api/photo/${encodeURIComponent(currentFile)}?size=2400`;
  new bootstrap.Modal(document.getElementById('lightboxModal')).show();
}

// ---------- drag and drop of the current photo ----------
document.getElementById('photo').addEventListener('dragstart', (e) => {
  e.dataTransfer.setData('text/plain', 'current-photo');
});

// ---------- actions ----------
async function assign(category) {
  if (!currentFile || !canAct()) return;
  await apiPost('/api/assign', {category});
  await refreshState();
}

async function customCategory() {
  const name = prompt('Category / folder name:');
  if (!name) return;
  if (bulkMode) {
    // bulkAssignToCategory does its own cooldown check -- don't also
    // consume it here, or the second check fails right after the first
    await bulkAssignToCategory(name);
    return;
  }
  if (!canAct()) return;
  await apiPost('/api/assign', {category: name});
  await refreshState();
}

async function skip() {
  if (!canAct()) return;
  await apiPost('/api/skip', {});
  await refreshState();
}

async function undo() {
  if (!canAct()) return;
  await apiPost('/api/undo', {});
  await refreshState();
}

async function renameCategory(oldName) {
  const newName = prompt('Edit the name and press OK:', oldName);
  if (!newName || newName === oldName) return;
  await api('/api/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old: oldName, new: newName})
  });
  await refreshState();
}

async function deleteCategory(category) {
  if (!confirm(`Delete the whole "${category}" category? Every photo in it goes back to unsorted.`)) return;
  await api('/api/delete_category', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category})
  });
  await refreshState();
}

async function openFolder(category) {
  await api('/api/open_folder', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category})
  });
}

async function syncFromDisk() {
  const r = await api('/api/sync', {method: 'POST'});
  setStatus(r.changed ? 'Synced -- some photos went back into the queue.' : 'Sync: nothing to reconcile, all good.');
  await refreshState();
}

async function clearAll() {
  if (!canAct()) return;
  if (!confirm('Clear EVERYTHING? This wipes every sorted folder and all progress for the whole event -- for everyone, on every device. Only the copies get deleted, your original photos are safe. This cannot be undone.')) return;
  const r = await api('/api/clear_all', {method: 'POST'});
  if (r.ok === false) {
    setStatus(r.error || 'Clear All is only allowed from the host machine.');
    return;
  }
  await refreshState();
}

// ---------- bulk mode ----------
function toggleBulkMode() {
  bulkMode = !bulkMode;
  document.getElementById('singleView').classList.toggle('d-none', bulkMode);
  document.getElementById('bulkView').classList.toggle('d-none', !bulkMode);
  if (bulkMode) loadBulkGrid();
}

async function loadBulkGrid() {
  const r = await api(`/api/undecided_list?limit=200&${partitionParams()}`);
  const grid = document.getElementById('bulkGrid');
  grid.innerHTML = '';
  bulkSelected.clear();
  updateBulkSelectedCount();
  for (const fname of r.files) {
    const wrap = document.createElement('div');
    wrap.className = 'bulk-thumb';
    wrap.dataset.fname = fname;
    wrap.innerHTML = `<img src="/api/photo/${encodeURIComponent(fname)}?size=220" loading="lazy" onclick="toggleBulkSelect('${escapeJs(fname)}')">`;
    grid.appendChild(wrap);
  }
  if (!r.files.length) {
    grid.innerHTML = '<div class="text-muted small p-2">Nothing left to sort in your section right now.</div>';
  }
}

function toggleBulkSelect(fname) {
  const el = document.querySelector(`.bulk-thumb[data-fname="${CSS.escape(fname)}"]`);
  if (bulkSelected.has(fname)) {
    bulkSelected.delete(fname);
    el && el.classList.remove('selected');
  } else {
    bulkSelected.add(fname);
    el && el.classList.add('selected');
  }
  updateBulkSelectedCount();
}

function updateBulkSelectedCount() {
  document.getElementById('bulkSelectedCount').textContent = `${bulkSelected.size} selected`;
}

function deselectAllBulk() {
  for (const fname of bulkSelected) {
    const el = document.querySelector(`.bulk-thumb[data-fname="${CSS.escape(fname)}"]`);
    el && el.classList.remove('selected');
  }
  bulkSelected.clear();
  updateBulkSelectedCount();
}

// ---------- press-and-drag multi-select (touch and mouse, like Photos/Gallery) ----------
function setupBulkDragSelect() {
  const grid = document.getElementById('bulkGrid');
  const box = document.getElementById('bulkSelectionBox');
  const LONG_PRESS_MS = 220;
  let pressTimer = null;
  let dragging = false;
  let dragMode = 'add';
  let startX = 0, startY = 0;
  let dragStartSelection = new Set();  // snapshot of what was selected before this drag

  function updateBox(curX, curY) {
    const left = Math.min(startX, curX), top = Math.min(startY, curY);
    const right = Math.max(startX, curX), bottom = Math.max(startY, curY);
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.style.width = (right - left) + 'px';
    box.style.height = (bottom - top) + 'px';

    // true marquee semantics: whatever the rectangle currently overlaps is
    // selected (or deselected, in remove mode) on top of whatever was
    // already selected before the drag started -- shrinking the box lets
    // go of items it no longer covers, same as Photos/Gallery apps
    document.querySelectorAll('.bulk-thumb').forEach((el) => {
      const r = el.getBoundingClientRect();
      const overlaps = !(r.right < left || r.left > right || r.bottom < top || r.top > bottom);
      const fname = el.dataset.fname;
      const wasSelected = dragStartSelection.has(fname);
      const shouldSelect = dragMode === 'add' ? (wasSelected || overlaps) : (wasSelected && !overlaps);
      if (shouldSelect && !bulkSelected.has(fname)) {
        bulkSelected.add(fname);
        el.classList.add('selected');
      } else if (!shouldSelect && bulkSelected.has(fname)) {
        bulkSelected.delete(fname);
        el.classList.remove('selected');
      }
    });
    updateBulkSelectedCount();
  }

  let activePointerId = null;

  grid.addEventListener('pointerdown', (e) => {
    const thumb = e.target.closest('.bulk-thumb');
    if (!thumb) return;
    startX = e.clientX; startY = e.clientY;
    activePointerId = e.pointerId;
    // keep every event for this finger/cursor routed to the grid even once
    // it strays outside the grid's box -- without this, dragging near the
    // edge hands control back to the browser and it starts scrolling again
    try { grid.setPointerCapture(e.pointerId); } catch (err) {}
    pressTimer = setTimeout(() => {
      dragging = true;
      dragMode = bulkSelected.has(thumb.dataset.fname) ? 'remove' : 'add';
      dragStartSelection = new Set(bulkSelected);
      grid.classList.add('dragging');
      box.style.display = 'block';
      updateBox(startX, startY);
      if (navigator.vibrate) navigator.vibrate(15);  // small haptic tick, phones only
    }, LONG_PRESS_MS);
  });

  grid.addEventListener('pointermove', (e) => {
    if (dragging) {
      e.preventDefault();
      updateBox(e.clientX, e.clientY);
      return;
    }
    if (!pressTimer) return;
    // still inside the long-press window -- block native scroll from
    // hijacking the gesture before our timer gets a chance to fire
    e.preventDefault();
    // moved too far before the long-press fired -- treat as a scroll/flick, cancel
    if (Math.abs(e.clientX - startX) > 8 || Math.abs(e.clientY - startY) > 8) {
      clearTimeout(pressTimer);
      pressTimer = null;
    }
  });

  function endPress() {
    clearTimeout(pressTimer);
    pressTimer = null;
    dragging = false;
    grid.classList.remove('dragging');
    box.style.display = 'none';
    if (activePointerId !== null) {
      try { grid.releasePointerCapture(activePointerId); } catch (err) {}
      activePointerId = null;
    }
  }
  grid.addEventListener('pointerup', endPress);
  grid.addEventListener('pointercancel', endPress);
  grid.addEventListener('pointerleave', () => { if (!dragging) clearTimeout(pressTimer); });
}

async function bulkAssignToCategory(category) {
  if (!canAct()) return;
  if (bulkSelected.size === 0) {
    setStatus('Select at least one photo first, then click a category to file them all.');
    return;
  }
  const r = await api('/api/bulk_assign', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category, files: Array.from(bulkSelected)})
  });
  setStatus(`Filed ${r.count} photos into ${category}.`);
  await refreshState();
}

async function bulkAssignSelected() {
  const category = document.getElementById('bulkCategorySelect').value;
  if (!category) { setStatus('Pick a category first.'); return; }
  await bulkAssignToCategory(category);
}

// ---------- session timer ----------
function startSessionTimer() {
  const start = Date.now();
  const el = document.getElementById('sessionTimer');
  setInterval(() => {
    const secs = Math.floor((Date.now() - start) / 1000);
    const h = String(Math.floor(secs / 3600)).padStart(2, '0');
    const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0');
    const s = String(secs % 60).padStart(2, '0');
    el.innerHTML = `<i class="bi bi-stopwatch"></i> ${h}:${m}:${s}`;
  }, 1000);
}

// ---------- pick-folder gate: nothing else starts until a folder is chosen ----------
let pickFolderPoll = null;

async function bootApp() {
  const info = await api('/api/current_folder');
  if (info.folder) {
    bootMultiplayer();
    return;
  }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('pickFolderModal')).show();
  pickFolderPoll = setInterval(async () => {
    const info2 = await api('/api/current_folder');
    if (info2.folder) {
      clearInterval(pickFolderPoll);
      pickFolderPoll = null;
      const m = bootstrap.Modal.getInstance(document.getElementById('pickFolderModal'));
      if (m) m.hide();
      bootMultiplayer();
    }
  }, 2000);
}

async function choosePhotoFolder() {
  const statusEl = document.getElementById('pickFolderStatus');
  statusEl.textContent = 'Opening the folder picker on the host computer...';
  const r = await api('/api/pick_folder', {method: 'POST'});
  if (r.ok) {
    statusEl.textContent = '';
    if (pickFolderPoll) { clearInterval(pickFolderPoll); pickFolderPoll = null; }
    const m = bootstrap.Modal.getInstance(document.getElementById('pickFolderModal'));
    if (m) m.hide();
    bootMultiplayer();
  } else if (r.error === 'cancelled') {
    statusEl.textContent = '';
  } else {
    statusEl.textContent = r.error || 'Could not pick a folder.';
  }
}

// ---------- boot ----------
document.getElementById('cooldownInput').value = getCooldownMs();
document.getElementById('lanUrlInput').value = window.location.origin;
initStripResize();
initSideResize();
setupBulkDragSelect();
bootApp();
startCategoryAutoSync();
startSessionTimer();
