// ============================================================
// DESKTOP-SPECIFIC: Tauri data layer
// Generated into desktop/dist/index.html by desktop/build.py
// ============================================================
const { invoke } = window.__TAURI__.core;

let pollTimer = null;
let lastProjectJson = null;
let cloudMode = false;

let state = {
  project: null, expanded: new Set(), lastUpdate: null, connected: false, error: null,
  activeTab: 'dashboard',
  showGraph: false, graphFilterDeps: true,
  retros: [], retroContent: null,
  documents: [], documentContent: null,
  hiddenStatuses: new Set(),
  hideEntryDone: new Set(),
  collapsedEntries: new Set(),
  sortAsc: true,
  searchQuery: '',
  showReleaseGraph: false,
  expandedDeployId: null,
  releasesSubTab: 'service',
  expandedPushId: null,
  cloudDiag: null,
  cloudProjectId: null,
  projectPath: null,
};

// ---- Data loading (Tauri invoke) ----

async function loadProject() {
  try {
    let json;
    if (cloudMode && state.cloudProjectId) {
      json = await invoke('cloud_load_project', { projectId: state.cloudProjectId });
    } else {
      json = await invoke('load_project_json');
    }
    if (!json) return;
    const data = JSON.parse(json);
    const newJson = JSON.stringify(data);
    if (newJson !== lastProjectJson) {
      lastProjectJson = newJson;
      state.project = data;
      state.lastUpdate = new Date();
      state.connected = true;
      state.error = null;
      renderOnDataChange();
    }
  } catch (e) {
    if (!state.error) {
      state.error = String(e);
      state.connected = false;
      stopPolling(); stopWatcher();
      renderProjectSelector();
    }
  }
}

async function loadDocuments() {
  try {
    if (cloudMode) {
      state.documents = JSON.parse(await invoke('cloud_list_documents'));
    } else {
      state.documents = JSON.parse(await invoke('get_documents', { scope: '' }));
    }
  } catch (e) { state.documents = []; }
}

async function loadRetros() {
  try {
    if (cloudMode) {
      state.retros = JSON.parse(await invoke('cloud_list_retros'));
    } else {
      state.retros = JSON.parse(await invoke('list_retros'));
    }
  } catch (e) { state.retros = []; }
}

async function loadRetroContent(week) {
  try {
    if (cloudMode) {
      state.retroContent = JSON.parse(await invoke('cloud_get_retro', { week }));
    } else {
      state.retroContent = JSON.parse(await invoke('get_retro_content', { week }));
    }
  } catch (e) { state.retroContent = { week, content: 'Failed to load.' }; }
  render();
}

async function loadDocumentContent(docId) {
  try {
    if (cloudMode) {
      state.documentContent = JSON.parse(await invoke('cloud_get_document', { docId }));
    } else {
      const content = await invoke('get_document_content', { docId });
      let title = docId, scope = 'memo', body = content;
      if (content.startsWith('---')) {
        const parts = content.split('---', 3);
        if (parts.length >= 3) {
          for (const line of parts[1].trim().split('\n')) {
            if (line.startsWith('scope:')) scope = line.split(':', 2)[1].trim();
          }
          body = parts[2].trim();
        }
      }
      const m = body.match(/^# (.+)/m);
      if (m) title = m[1];
      state.documentContent = { doc_id: docId, title, scope, content: body };
    }
  } catch (e) {
    state.documentContent = { doc_id: docId, title: 'Error', scope: 'memo', content: String(e) };
  }
  render();
}

function startPolling() { if (!pollTimer) pollTimer = setInterval(loadProject, 2000); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

let unlistenWatcher = null;
async function startWatcher() {
  if (unlistenWatcher) return;
  if (window.__TAURI__?.event) {
    unlistenWatcher = await window.__TAURI__.event.listen('beacon-changed', () => loadProject());
  }
}
function stopWatcher() { if (unlistenWatcher) { unlistenWatcher(); unlistenWatcher = null; } }

// F27: project-changed event — fired by Rust single-instance plugin when a
// second `open -a Beacon --args /path` lands. Clear current state and reload
// from the new project dir.
let unlistenProjectSwitch = null;
async function startProjectSwitchListener() {
  if (unlistenProjectSwitch) return;
  if (window.__TAURI__?.event) {
    unlistenProjectSwitch = await window.__TAURI__.event.listen('project-changed', async () => {
      // Clear in-flight watcher / state from previous project
      stopWatcher();
      state.project = null;
      state.expanded.clear();
      lastProjectJson = '';
      state.cloudProjectId = null;
      cloudMode = false;
      state.connected = false;
      state.error = null;
      render();
      // Reload from the new project_dir set by Rust
      await loadProject();
      await startWatcher();
    });
  }
}

async function doSelectCloudProject(projectId) {
  cloudMode = true;
  state.cloudProjectId = projectId;
  state.error = null; state.project = null; state.expanded.clear();
  state.documents = []; state.documentContent = null;
  state.retros = []; state.retroContent = null;
  state.activeTab = 'dashboard'; lastProjectJson = null;
  state.projectPath = 'cloud:' + projectId;
  try {
    await loadProject();
    if (state.project) {
      for (const ms of state.project.milestones || []) {
        if (ms.status === 'in_progress') state.expanded.add(ms.id);
      }
      render();
      // Polling 撤去: 2s setInterval が main thread を JSON parse でブロックして
      // scroll を catch していた。Cloud の live 更新は Web UI 側 (WS) で見る。
      // Tauri cloud の live は別タスクで Tauri-side WS を入れる方針。
    }
  } catch (e) { state.error = String(e); renderProjectSelector(); }
}

async function doSelectProject(path) {
  try {
    cloudMode = false;
    state.cloudProjectId = null;
    stopPolling(); stopWatcher();
    await invoke('set_project_dir', { dir: path });
    state.error = null; state.project = null; state.expanded.clear();
    state.documents = []; state.documentContent = null;
    state.retros = []; state.retroContent = null;
    state.activeTab = 'dashboard'; lastProjectJson = null;
    state.projectPath = path;
    await loadProject();
    if (state.project) {
      for (const ms of state.project.milestones || []) {
        if (ms.status === 'in_progress') state.expanded.add(ms.id);
      }
      render(); startWatcher();
    }
  } catch (e) { state.error = String(e); renderProjectSelector(); }
}

// ---- Project selector ----

async function renderProjectSelector() {
  const app = document.getElementById('app');
  let localProjects = [], cloudProjects = [], authenticated = false;
  try { localProjects = await invoke('list_projects'); } catch (e) {}
  try { authenticated = await invoke('is_authenticated'); } catch (e) {}
  if (authenticated) {
    try {
      cloudProjects = JSON.parse(await invoke('cloud_list_projects'));
    } catch (e) { state.cloudError = String(e); }
  }
  const localOnly = localProjects.filter(p => p.mode === 'local');

  app.innerHTML = `
    <div class="open-prompt fade-in">
      <h1 style="font-family:var(--font-mono);font-size:2rem;font-weight:700;">Beacon</h1>
      <p style="font-size:0.8rem;color:var(--text-dim);font-style:italic;">Where humans and AI are bound together.</p>
      ${state.error ? `<p style="font-size:0.75rem;color:var(--cancelled);">${esc(state.error)}</p>` : ''}
      <div style="width:100%;max-width:480px;margin-top:24px;">
        ${localOnly.length > 0 ? `<div class="summary-label">Local Projects</div>
          ${localOnly.map(p => `<div class="project-item" data-action="select-project" data-path="${esc(p.path)}"><div class="project-item-name">${esc(p.name)}</div><div class="project-item-id">${esc(p.path)}</div></div>`).join('')}` : ''}
        ${cloudProjects.length > 0 ? `<div class="summary-label" style="margin-top:16px;">Cloud Projects</div>
          ${cloudProjects.filter(p => !p.archived).map(p => `<div class="project-item" data-action="select-cloud-project" data-project-id="${esc(p.project_id)}"><div class="project-item-name">${esc(p.name)} <span style="font-size:0.65rem;color:var(--info)">cloud</span></div><div class="project-item-id">${esc(p.project_id)}</div></div>`).join('')}` : ''}
        ${!authenticated && localOnly.length === 0 ? `<p style="color:var(--text-dim);font-size:0.8rem;margin-top:12px;">Run <code style="color:var(--accent)">beacon auth login</code> to access cloud projects,<br>or <code style="color:var(--accent)">beacon init</code> to create a local project.</p>` : ''}
        ${state.cloudError ? `<p style="color:var(--warning);font-size:0.75rem;margin-top:12px;">Cloud error: ${esc(state.cloudError)}<br><span style="color:var(--text-dim);">Run <code style="color:var(--accent)">beacon auth login</code> to refresh.</span></p>` : ''}
        ${state.cloudDiag ? `<pre style="color:var(--text-dim);font-size:0.65rem;margin-top:8px;white-space:pre-wrap;border:1px solid var(--border);padding:8px;border-radius:3px;">${esc(state.cloudDiag)}</pre>` : ''}
        <button data-action="cloud-diagnose" style="margin-top:12px;font-size:0.7rem;padding:3px 10px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--text-dim);cursor:pointer;font-family:inherit;">Diagnose cloud</button>
      </div>
    </div>`;
  bindEvents();
}

// ---- Render ----

function render() {
  const app = document.getElementById('app');
  if (!state.project) return;
  const scrollY = window.scrollY;
  const p = state.project;
  const sorted = sortMilestones(p.milestones || []);
  const filtered = filterMilestones(sorted);
  const activeCount = (p.milestones || []).filter(m => m.status === 'in_progress').length;
  const doneCount = (p.milestones || []).filter(m => m.status === 'done').length;
  const retro = nextRetroDate(p.retro_day);

  app.innerHTML = `
    <header class="header fade-in">
      <div class="header-top">
        <button class="hamburger-btn" data-action="open-menu" title="Menu"><span></span><span></span><span></span></button>
        <span class="project-name">${esc(p.name)}</span>
        <span class="header-tag">beacon desktop</span>
        <div class="connection-status" title="${cloudMode ? 'live = cloud API watcher' : 'live = local file watcher'}">
          <div class="status-dot ${state.connected ? '' : 'offline'}"></div>
          <span>${state.connected ? 'live' : 'offline'}</span>
        </div>
      </div>
    </header>

    ${(p.objective || retro) ? `
    <div class="project-info fade-in stagger-1">
      ${p.objective ? `<div class="project-info-item"><span class="project-info-label">Objective</span><span class="project-info-value">${esc(p.objective)}</span></div>` : ''}
      ${retro ? `<div class="project-info-item${retro.isToday ? ' review-due' : ''}"><span class="project-info-label">${retro.isToday ? '⚠ Review Due Today' : 'Next Review'}</span><span class="project-info-value">${retro.date} (${esc(p.retro_day || 'friday')})</span></div>` : ''}
    </div>` : ''}

    <div class="tab-bar fade-in stagger-1">
      <button class="tab-btn ${state.activeTab === 'dashboard' ? 'active' : ''}" data-action="switch-tab" data-tab="dashboard">Milestones</button>
      <button class="tab-btn ${state.activeTab === 'documents' ? 'active' : ''}" data-action="switch-tab" data-tab="documents">Documents</button>
      <button class="tab-btn ${state.activeTab === 'releases' ? 'active' : ''}" data-action="switch-tab" data-tab="releases">Releases</button>
    </div>

    ${state.activeTab === 'dashboard' ? (state.showGraph ? renderGraphSection() : `
      ${p.summary ? `<section class="summary-block fade-in"><div class="summary-label">Session Context</div><div class="summary-text">${esc(p.summary)}</div></section>` : ''}
      <section class="milestones fade-in">
        <div class="milestones-header">
          <span class="milestones-title">Milestones</span>
          <span class="milestones-count">${activeCount} active · ${doneCount}/${(p.milestones || []).length} done</span>
          <input class="ms-search-inline" id="search-input" type="search" placeholder="Search milestones, tasks, commits..." value="${esc(state.searchQuery)}" autocomplete="off">
          <button class="sort-toggle" data-action="show-graph">Graph →</button>
        </div>
        <div id="ms-list-container">${state.searchQuery.trim() ? renderSearchResults() : `
          <div class="filter-bar">
            <button class="filter-btn f-all ${state.hiddenStatuses.size === 0 ? 'on' : 'off'}" data-action="filter-all">all</button>
            ${FILTER_ORDER.map(s => `<button class="filter-btn ${!state.hiddenStatuses.has(s) ? 'on' : 'off'} ${FILTER_CSS_CLASS[s] || ''}" data-action="filter-status" data-status="${s}">${STATUS_LABELS[s] || s}</button>`).join('')}
            <button class="sort-toggle" data-action="toggle-sort">${state.sortAsc ? '↑ ms-1 first' : '↓ latest first'}</button>
          </div>
          ${filtered.map(ms => renderMilestoneCard(ms, 0)).join('')}
        `}</div>
      </section>`) : ''}

    ${state.activeTab === 'documents' ? renderDocumentsSection() : ''}
    ${state.activeTab === 'releases' ? renderReleasesSection() : ''}

    <footer class="footer fade-in stagger-3">
      <div class="footer-left">
        <div>${cloudMode ? 'cloud' : 'local'}</div>
        <div class="footer-tagline">Where humans and AI are bound together.</div>
      </div>
      <div class="footer-right" style="display:flex;align-items:center;gap:12px;">
        ${state.lastUpdate ? formatTime(state.lastUpdate) : ''}
        ${cloudMode ? `<button class="sort-toggle" data-action="archive-cloud-project" style="font-size:0.65rem;color:var(--text-dim);">Archive</button>` : ''}
        <button class="sort-toggle" data-action="export-json" style="font-size:0.65rem;">Export JSON</button>
      </div>
    </footer>
  `;
  bindEvents();
}

// ---- Menu ----

async function renderMenu() {
  let localProjects = [], cloudProjects = [];
  try { localProjects = await invoke('list_projects'); } catch (e) {}
  try {
    const auth = await invoke('is_authenticated');
    if (auth) cloudProjects = JSON.parse(await invoke('cloud_list_projects'));
  } catch (e) {}
  const localOnly = localProjects.filter(p => p.mode === 'local');
  const currentPath = state.projectPath || '';
  const root = document.getElementById('menu-root');
  root.innerHTML = `
    <div class="menu-overlay" data-action="close-menu"></div>
    <div class="menu-panel">
      <div class="menu-header"><span class="menu-title">Menu</span><button class="menu-close" data-action="close-menu">×</button></div>
      ${localOnly.length > 0 ? `<div class="menu-section">
        <div class="menu-section-title">Local Projects</div>
        ${localOnly.map(p => `<button class="menu-item ${p.path === currentPath ? 'current' : ''}" data-action="menu-select-project" data-path="${esc(p.path)}">${esc(p.name)}<div class="menu-item-sub">${esc(p.path)}</div></button>`).join('')}
      </div>` : ''}
      ${cloudProjects.length > 0 ? `<div class="menu-section">
        <div class="menu-section-title">Cloud Projects</div>
        ${cloudProjects.filter(p => !p.archived).map(p => `<button class="menu-item ${('cloud:' + p.project_id) === currentPath ? 'current' : ''}" data-action="menu-select-cloud-project" data-project-id="${esc(p.project_id)}">${esc(p.name)}<div class="menu-item-sub">${esc(p.project_id)}</div></button>`).join('')}
      </div>` : ''}
    </div>`;
  root.querySelectorAll('[data-action]').forEach(el => el.addEventListener('click', handleAction));
}

function closeMenu() { document.getElementById('menu-root').innerHTML = ''; }

// ---- Event delegation ----

function bindEvents() {
  document.querySelectorAll('[data-action]').forEach(el => {
    el.addEventListener('click', handleAction);
  });
}

async function handleAction(e) {
  const el = e.currentTarget;
  const action = el.dataset.action;
  if (action === 'toggle-entry' && e.target.closest('.entry-children')) return;
  e.stopPropagation();

  // First try SHARED common-action dispatcher (ms-46 e-726).
  // Returns true if handled — keeps platform handleAction focused on
  // data-source-specific cases.
  if (handleCommonAction(action, el)) return;

  switch (action) {
    // ---- Project selection (path-based for Tauri local) ----
    case 'select-project': await doSelectProject(el.dataset.path); break;
    case 'select-cloud-project': await doSelectCloudProject(el.dataset.projectId); break;
    case 'menu-select-project': closeMenu(); await doSelectProject(el.dataset.path); break;
    case 'menu-select-cloud-project': closeMenu(); await doSelectCloudProject(el.dataset.projectId); break;

    // ---- Data fetching (Tauri invoke; e-728 DataSource adapter で統一予定) ----
    case 'switch-tab': {
      state.activeTab = el.dataset.tab;
      state.showGraph = false;
      state.documentContent = null; state.retroContent = null;
      if (state.activeTab === 'documents') {
        const loads = [];
        if (state.documents.length === 0) loads.push(loadDocuments());
        if (state.retros.length === 0) loads.push(loadRetros());
        await Promise.all(loads);
      }
      render(); break;
    }
    case 'open-document': await loadDocumentContent(el.dataset.docId); break;
    case 'open-retro': await loadRetroContent(el.dataset.week); break;

    // ---- Tauri-specific: clipboard URL format / commands ----
    case 'copy-doc-link': {
      const url = `https://beacon-ai.dev/?project=${state.cloudProjectId}#doc/${el.dataset.docId}`;
      await navigator.clipboard.writeText(url);
      el.textContent = 'Copied!';
      setTimeout(() => { el.textContent = 'Copy web link'; }, 1500);
      break;
    }
    case 'open-menu': await renderMenu(); break;
    case 'cloud-diagnose': {
      state.cloudDiag = 'Running...';
      renderProjectSelector();
      state.cloudDiag = await invoke('cloud_diagnose').catch(e => String(e));
      renderProjectSelector();
      break;
    }
    case 'export-json': await exportProject(); break;
    case 'archive-cloud-project': await archiveCloudProject(); break;
  }
}

// ---- Project actions ----

async function archiveCloudProject() {
  if (!state.cloudProjectId) return;
  if (!confirm('Archive this project? It will be hidden from the project list.')) return;
  try {
    await invoke('cloud_archive_project');
    closeMenu();
    state.cloudProjectId = null; state.project = null;
    renderProjectSelector();
  } catch (e) { alert('Failed to archive: ' + e); }
}

async function exportProject() {
  try {
    let project, documents = [], retros = [];
    if (cloudMode && state.cloudProjectId) {
      project = JSON.parse(await invoke('cloud_load_project', { projectId: state.cloudProjectId }));
      try { documents = JSON.parse(await invoke('cloud_list_documents')); } catch (e) {}
      try { retros = JSON.parse(await invoke('cloud_list_retros')); } catch (e) {}
    } else {
      project = JSON.parse(await invoke('load_project_json'));
      try { documents = JSON.parse(await invoke('get_documents', { scope: '' })); } catch (e) {}
      try { retros = JSON.parse(await invoke('list_retros')); } catch (e) {}
    }
    const blob = new Blob([JSON.stringify({ ...project, documents, retros }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `${state.cloudProjectId || 'project'}.json`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  } catch (e) { alert('Export failed: ' + e); }
}

// ---- Reconnect (Tauri focus events) ----

function _tryReconnect() {
  if (!state.project) return;
  if (cloudMode) {
    // Polling 撤去 (上記 doSelectCloudProject 同様)。focus 復帰時に
    // 1 回だけ loadProject を呼んでスナップショットを取り直す。以降は静音。
    loadProject();
  } else {
    startWatcher();
    loadProject();
  }
}

if (window.__TAURI__?.event) {
  window.__TAURI__.event.listen('tauri://focus', _tryReconnect);
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') _tryReconnect();
});

// ---- Init ----

async function init() {
  // F27: always listen for project-changed even before project loads — covers
  // the cold-start case where Beacon was launched via `open -a Beacon --args ...`
  // but the running instance later receives another launch with a different path.
  startProjectSwitchListener();
  try {
    await loadProject();
    if (state.project) {
      for (const ms of state.project.milestones || []) {
        if (ms.status === 'in_progress') state.expanded.add(ms.id);
      }
      render(); startWatcher(); return;
    }
  } catch (e) {}
  renderProjectSelector();
}

if (window.__TAURI__) init();
else document.addEventListener('DOMContentLoaded', () => { if (window.__TAURI__) init(); });
