// ============================================================
// DESKTOP-SPECIFIC: Tauri data layer
// Generated into desktop/dist/index.html by desktop/build.py
// ============================================================
const { invoke } = window.__TAURI__.core;

let pollTimer = null;
let lastProjectJson = null;
let cloudMode = false;

// Cloud WS live-update state (e-738). Web UI has equivalent in index.html;
// Tauri previously left this as "別タスク" comment in doSelectCloudProject
// after 3cebae5 removed the 2s polling, leaving cloud Tauri permanently stale.
let cloudWs = null;
let cloudWsReconnectTimer = null;
let cloudWsPingInterval = null;
let cloudWsReconnectAttempts = 0;
const CLOUD_WS_RECONNECT_MAX_MS = 30000;

// ms-64 / e-1461: api_url is resolved at runtime via the Tauri command
// `cloud_get_api_url`, which honors BEACON_API_URL / cwd cloud.json /
// ~/.beacon/profiles/<name>/profile.json / default precedence. Cached after
// the first lookup to avoid a per-connect round-trip into Rust. Switching
// profile requires an app restart, same as switching projects.
let _cachedApiUrl = null;
async function getApiUrl() {
  if (_cachedApiUrl) return _cachedApiUrl;
  try {
    _cachedApiUrl = await invoke('cloud_get_api_url');
  } catch (_e) {
    _cachedApiUrl = 'https://beacon-ai.dev';
  }
  return _cachedApiUrl;
}

// Strip scheme to derive the WS host (wss://<host>/ws/...). Default profile
// `https://beacon-ai.dev` → `beacon-ai.dev`. Custom https profiles work too.
function apiUrlToWsHost(apiUrl) {
  return apiUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

let state = {
  project: null, expanded: new Set(), lastUpdate: null, connected: false, error: null,
  activeTab: 'dashboard',
  showGraph: false, graphFilterDeps: true,
  retros: [], retroContent: null,
  documents: [], documentContent: null, documentOverlay: null,
  hiddenStatuses: new Set(['cancelled']),
  projectVersion: null,
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
  // ms-46 e-745: Operations / Notes alignment with Web UI.
  // Operations は state.project.operations から read (追加 loader 不要)。
  // Notes はサーバー / CLI 経由で取得する必要があるため state に持つ。
  sessionNotes: [],
  // ms-57 e-1041: Session tab sub-view + session log list (mirror of Web state).
  sessionView: 'notes',
  sessionLogs: [],
  // SHARED state slots that Tauri also needs (Web initializes these in its
  // own SKIP block; Tauri must mirror them or render() throws on the first
  // expanded milestone — state.showCancelled.has() blows up if undefined,
  // which bounces the user back to the project selector. (ms-43 follow-up)
  showCancelled: new Set(),
  searchResults: null,
  searchFacets: { type: {}, status: {}, priority: {}, scope: {} },
  searchTotal: 0,
  searchLoading: false,
  searchToken: 0,
  searchTypeFilter: 'all',
  menuOpen: false,
  openEntryId: null,
  documentsScopeFilter: 'all',
  selectedDeployEnv: 'all',
  auth: {},
  projects: [],
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

// DataSource (ms-46 e-728): Tauri implementation (Tauri invoke).
// Web equivalent in server/static/index.html SKIP block. SHARED switchTab /
// openDocument / openRetro only see this interface.
const dataSource = {
  loadDocuments: async () => {
    state.documents = cloudMode
      ? JSON.parse(await invoke('cloud_list_documents'))
      : JSON.parse(await invoke('get_documents', { scope: '' }));
  },
  loadRetros: async () => {
    state.retros = cloudMode
      ? JSON.parse(await invoke('cloud_list_retros'))
      : JSON.parse(await invoke('list_retros'));
  },
  loadRetroContent: async (week) => {
    state.retroContent = cloudMode
      ? JSON.parse(await invoke('cloud_get_retro', { week }))
      : JSON.parse(await invoke('get_retro_content', { week }));
  },
  // ms-46 e-745: cloud mode のみ。Local mode は CLI 経由が未配線 (band-aid)。
  loadSessionNotes: async () => {
    state.sessionNotes = cloudMode
      ? JSON.parse(await invoke('cloud_list_notes'))
      : [];
  },
  // ms-57 e-1041: session log list. Cloud Rust binding (`cloud_list_session_logs`)
  // will be wired in a follow-up; until then we fail-soft to an empty list so
  // the Session Log sub-view simply shows "no entries" instead of crashing.
  loadSessionLogs: async () => {
    try {
      state.sessionLogs = cloudMode && typeof invoke === 'function'
        ? JSON.parse(await invoke('cloud_list_session_logs', { limit: 5 }))
        : [];
    } catch (_e) {
      state.sessionLogs = [];
    }
  },
  loadDocumentContent: async (docId) => {
    if (cloudMode) {
      state.documentContent = JSON.parse(await invoke('cloud_get_document', { docId }));
      return;
    }
    // Local mode: CLI returns raw markdown; parse frontmatter for scope/title.
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
  },
  // ms-70 e-1718 — DM approval history (Settings > Audit).
  // Tauri Rust binding (cloud_list_dm_approval_history) is not implemented
  // yet; mirror the "Web-only" pattern used by Member admin (= now wired
  // below via cloud_* commands). Audit history Rust binding is tracked as
  // a follow-up; throw stub keeps the SHARED region pure (= ms-46 e-728).
  loadDmApprovalHistory: async (_pid, _limit) => {
    throw new Error('DM approval history is Web-only in this build. Open the Web UI to view the audit trail.');
  },
  // ms-78 / e-1909 — Profile read / write. Cloud Rust bindings
  // (cloud_get_my_profile / cloud_update_my_profile) are a follow-up; the
  // Tauri build silently no-ops the load so the SHARED retroactive prompt
  // path stays inert and the Settings > Profile tab surfaces a one-line
  // "Web-only" message on save attempt.
  loadMyProfile: async () => {
    // No-op: leave state.myProfile null so _shouldShowDisplayNamePrompt
    // returns false (= guard against showing the banner inside Tauri until
    // the Rust binding lands).
    state.myProfile = null;
  },
  updateMyProfile: async (_displayName) => {
    throw new Error('Profile editing is Web-only in this build. Open the Web UI to set your display name.');
  },
  // ms-72 e-1779 — Member admin + invitation flow wired to Rust commands.
  // Was a "Web-only" throw stub before the Rust layer landed (= ms-72 prior).
  // SHARED Settings > Members tab now reaches the real backend through these
  // invoke() calls, restoring parity with the Web UI. JSON.parse mirrors the
  // pattern used by loadDocuments / loadRetros (= Rust returns raw response
  // text; the JS dataSource decodes).
  loadProjectMembers: async (pid) => JSON.parse(
    await invoke('cloud_list_members', { projectId: pid }),
  ),
  inviteMember: async (pid, email, role) => JSON.parse(
    await invoke('cloud_invite_member', { projectId: pid, args: { email, role } }),
  ),
  changeMemberRole: async (pid, email, role) => JSON.parse(
    await invoke('cloud_change_member_role', { projectId: pid, args: { email, role } }),
  ),
  removeMember: async (pid, email) => JSON.parse(
    await invoke('cloud_remove_member', { projectId: pid, args: { email } }),
  ),
  // ms-78 e-1803 (= token-based invitation) — Tauri parity for the
  // "Generate invite URL" flow. createInvitation returns { invitation, url,
  // expires_at } where `url` includes the plaintext token (= shown to the
  // owner once, never returned again by listInvitations).
  createInvitation: async (pid, email, role) => JSON.parse(
    await invoke('cloud_create_invitation', { projectId: pid, args: { email, role } }),
  ),
  listInvitations: async (pid) => JSON.parse(
    await invoke('cloud_list_invitations', { projectId: pid }),
  ),
  cancelInvitation: async (pid, invitationId) => JSON.parse(
    await invoke('cloud_cancel_invitation', { projectId: pid, args: { invitationId } }),
  ),
  // ms-78 e-1804 — Public landing endpoints. previewInvitation does NOT need
  // an auth token (= Rust drops the Authorization header); acceptInvitation
  // requires the caller to be signed in.
  previewInvitation: async (token) => JSON.parse(
    await invoke('cloud_preview_invitation', { args: { token } }),
  ),
  acceptInvitation: async (token, displayName) => JSON.parse(
    await invoke('cloud_accept_invitation', { args: { token, displayName: displayName || '' } }),
  ),
  // ms-72 e-1774 — Project archive / unarchive. Maps to existing Rust
  // commands (`cloud_archive_project` / `cloud_unarchive_project`) when
  // running cloud mode. Local Tauri mode is project-archive-free (= local
  // projects are managed via filesystem moves, not a flag).
  setProjectArchived: async (_pid, action) => {
    if (!cloudMode) {
      throw new Error('Local projects cannot be archived from the UI. Move the project folder instead.');
    }
    if (action === 'archive') {
      await invoke('cloud_archive_project');
    } else if (action === 'unarchive') {
      await invoke('cloud_unarchive_project');
    } else {
      throw new Error(`Unknown archive action: ${action}`);
    }
  },
};

function startPolling() { if (!pollTimer) pollTimer = setInterval(loadProject, 2000); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ---- Cloud WebSocket (live updates) ----
// e-738: Tauri cloud mode previously had no live updates after 3cebae5
// removed the 2s polling. This subscribes to the server's project broadcast,
// mirroring Web UI behaviour in server/static/index.html.

async function connectCloudWebSocket(projectId) {
  closeCloudWebSocket();
  if (!projectId) return;

  let token;
  try {
    token = await invoke('cloud_get_auth_token');
  } catch (e) {
    state.connected = false;
    state.error = String(e);
    render();
    return;
  }

  const apiUrl = await getApiUrl();
  const wsHost = apiUrlToWsHost(apiUrl);
  const url = `wss://${wsHost}/ws/projects/${encodeURIComponent(projectId)}?token=${encodeURIComponent(token)}`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    state.connected = false;
    state.error = `WS construct failed: ${e}`;
    render();
    return;
  }
  cloudWs = ws;

  ws.onopen = () => {
    cloudWsReconnectAttempts = 0;
    state.connected = true;
    state.error = null;
    // Targeted DOM update for the offline → live indicator. A full render()
    // here repaints the entire shell right after the initial dashboard
    // render, which the user sees as a 1-frame flash. The connection dot is
    // the only thing that actually changes on open, so update it directly
    // and leave the rest of the DOM untouched. (ms-43 follow-up)
    const dot = document.querySelector('.status-dot');
    if (dot) dot.classList.remove('offline');
    const label = document.querySelector('.connection-label');
    if (label) label.textContent = 'live';
    if (cloudWsPingInterval) clearInterval(cloudWsPingInterval);
    cloudWsPingInterval = setInterval(() => {
      if (cloudWs && cloudWs.readyState === WebSocket.OPEN) cloudWs.send('ping');
    }, 30000);
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    // ms-43 e-809: route document_change frames through the SHARED
    // applyDocumentChange (built into desktop/dist from server/static/index.html
    // by desktop/build.py) so Documents tab parity with Web UI is automatic.
    if (msg.type === 'document_change') {
      if (typeof applyDocumentChange === 'function') applyDocumentChange(msg.data);
      return;
    }
    if (msg.type !== 'project') return;
    const data = msg.data || {};
    const newJson = JSON.stringify(data);
    if (newJson === lastProjectJson) return;
    lastProjectJson = newJson;
    state.project = data;
    state.lastUpdate = new Date();
    state.connected = true;
    state.error = null;
    renderOnDataChange();
  };

  ws.onerror = () => {
    state.connected = false;
    if (state.project) render();
  };

  ws.onclose = async (ev) => {
    if (cloudWsPingInterval) { clearInterval(cloudWsPingInterval); cloudWsPingInterval = null; }
    cloudWs = null;
    state.connected = false;

    // Server-side close codes (server/app.py @ ws_project):
    //   4401: token missing — surface "sign in" message, do not retry.
    //   4403 reason="token_expired_or_invalid": refresh the token and
    //        reconnect once. If refresh fails, prompt re-sign-in.
    //   4403 reason="forbidden": user has no role on this project
    //        (e-1252). MUST NOT retry — reconnecting is futile and just
    //        loops against the auth gate.
    //   4404: the project doesn't exist (e-1252). Stop reconnecting and
    //        surface a clear "project not found" message.
    if (ev && ev.code === 4401) {
      state.error = 'Not signed in. Run `beacon auth login` to enable live updates.';
      if (state.project) render();
      return;
    }
    if (ev && ev.code === 4404) {
      state.error = 'This project does not exist or has been deleted.';
      if (state.project) render();
      return;
    }
    if (ev && ev.code === 4403) {
      if (ev.reason === 'forbidden') {
        state.error = 'You do not have access to this project. Ask the owner to invite you.';
        if (state.project) render();
        return;
      }
      // token_expired_or_invalid: try one silent refresh.
      try {
        await invoke('cloud_refresh_auth_token');
        // Reset attempts since refresh succeeded; reconnect immediately.
        cloudWsReconnectAttempts = 0;
        connectCloudWebSocket(projectId);
        return;
      } catch (e) {
        state.error = 'Session expired. Run `beacon auth login` to sign in again.';
        if (state.project) render();
        return;
      }
    }

    // Generic close (network blip, server restart): exponential backoff capped
    // at 30s, matching lib/store_api.py.
    if (!cloudMode || state.cloudProjectId !== projectId) return;
    cloudWsReconnectAttempts += 1;
    const delay = Math.min(1000 * (2 ** (cloudWsReconnectAttempts - 1)), CLOUD_WS_RECONNECT_MAX_MS);
    if (state.project) render();
    cloudWsReconnectTimer = setTimeout(() => {
      cloudWsReconnectTimer = null;
      if (cloudMode && state.cloudProjectId === projectId) {
        connectCloudWebSocket(projectId);
      }
    }, delay);
  };
}

function closeCloudWebSocket() {
  if (cloudWsReconnectTimer) { clearTimeout(cloudWsReconnectTimer); cloudWsReconnectTimer = null; }
  if (cloudWsPingInterval) { clearInterval(cloudWsPingInterval); cloudWsPingInterval = null; }
  if (cloudWs) {
    // Suppress reconnect: clear handler before close.
    cloudWs.onclose = null;
    cloudWs.onerror = null;
    try { cloudWs.close(); } catch (_) {}
    cloudWs = null;
  }
  cloudWsReconnectAttempts = 0;
}

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
      closeCloudWebSocket();
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
  closeCloudWebSocket();
  cloudMode = true;
  state.cloudProjectId = projectId;
  state.error = null; state.project = null; state.expanded.clear();
  state.documents = []; state.documentContent = null;
  state.retros = []; state.retroContent = null;
  state.activeTab = 'dashboard'; lastProjectJson = null;
  state.projectPath = 'cloud:' + projectId;
  // ms-43 e-1023: reset search state on project switch so a query typed in
  // project A doesn't silently filter project B.
  state.searchQuery = '';
  state.searchResults = null;
  state.searchFacets = { type: {}, status: {}, priority: {}, scope: {} };
  state.searchTotal = 0;
  state.searchLoading = false;
  state.searchTypeFilter = 'all';
  const _searchInput = document.getElementById('search-input');
  if (_searchInput) _searchInput.value = '';
  try {
    await loadProject();
    if (state.project) {
      for (const ms of state.project.milestones || []) {
        if (ms.status === 'in_progress') state.expanded.add(ms.id);
      }
      render();
    }
    // e-738: subscribe to live updates via WS. Polling was removed in 3cebae5
    // because the 2s setInterval blocked the main thread; WS pushes instead
    // arrive on data change only, so no constant CPU/JSON-parse cost.
    connectCloudWebSocket(projectId);
  } catch (e) { state.error = String(e); renderProjectSelector(); }
}

async function doSelectProject(path) {
  try {
    cloudMode = false;
    state.cloudProjectId = null;
    closeCloudWebSocket();
    stopPolling(); stopWatcher();
    await invoke('set_project_dir', { dir: path });
    state.error = null; state.project = null; state.expanded.clear();
    state.documents = []; state.documentContent = null;
    state.retros = []; state.retroContent = null;
    state.activeTab = 'dashboard'; lastProjectJson = null;
    state.projectPath = path;
    // ms-43 e-1023: reset search state on project switch so a query typed in
    // project A doesn't silently filter project B.
    state.searchQuery = '';
    state.searchResults = null;
    state.searchFacets = { type: {}, status: {}, priority: {}, scope: {} };
    state.searchTotal = 0;
    state.searchLoading = false;
    state.searchTypeFilter = 'all';
    const _searchInput = document.getElementById('search-input');
    if (_searchInput) _searchInput.value = '';
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

// Platform hooks for renderShell (ms-46 e-743). Web equivalent lives in
// server/static/index.html SKIP block. renderShell itself is shared.
const PLATFORM = {
  headerTag: 'beacon desktop',
  getConnectionTitle: () => cloudMode ? 'live = cloud API watcher' : 'live = local file watcher',
  versionBadgeHTML: () => {
    state.projectVersion = computeProjectVersion(state.project);
    return renderVersionBadge();
  },
  getFooterLeftHTML: () => `<div>${cloudMode ? 'cloud' : 'local'}</div>`,
  getArchiveButtonHTML: (p) => cloudMode
    ? `<button class="sort-toggle" data-action="archive-cloud-project" style="font-size:0.65rem;color:var(--text-dim);">Archive</button>`
    : '',
  footerStaggerClass: 'stagger-3',
  // ms-72 e-1779 — Account-menu items: Admin + Sign out, matching Web.
  // Was a CLI-hint stub while cloud_logout was unwired (= ms-72 e-1774). Now
  // that the Rust binding lands, the Tauri build can clear credentials.json
  // through the menu just like Web clears localStorage. Admin opens the Web
  // admin page in an external browser (Tauri has no `/admin` route of its
  // own) — handled in `case 'goto-admin'` below.
  accountMenuItemsHTML: () => `
    <button class="account-menu-item" data-action="goto-admin">Admin</button>
    <button class="account-menu-item danger" data-action="logout">Sign out</button>
  `,
};

function render() {
  const app = document.getElementById('app');
  if (!state.project) return;
  renderShell(app);
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

  // ms-72 e-1779 — Tauri-specific account-menu intercepts BEFORE the SHARED
  // dispatcher gets them. SHARED's `goto-admin` calls `gotoAdmin()` which
  // does `window.location.assign('/admin')`; inside the Tauri webview that
  // navigates AWAY from the app shell and breaks the session, so we open
  // the Web admin URL in an external browser via the system shell instead.
  // SHARED has no `logout` case at all (Web's `logout()` lives in SKIP), so
  // we own the Tauri sign-out path here: clear credentials.json via
  // cloud_logout and bounce back to the project selector. The case
  // statements below mirror the names so scripts/check-frontend-drift.py
  // sees handleAction "covers" both actions; the actual work is dispatched
  // into the switch via TAURI_PLATFORM_INTERCEPTS so the SHARED dispatcher
  // never sees them.
  const TAURI_PLATFORM_INTERCEPTS = new Set(['goto-admin', 'logout']);
  if (!TAURI_PLATFORM_INTERCEPTS.has(action)) {
    // First try SHARED common-action dispatcher (ms-46 e-726).
    // Returns true if handled — keeps platform handleAction focused on
    // data-source-specific cases.
    if (handleCommonAction(action, el)) return;
  }

  switch (action) {
    case 'goto-admin': {
      try { if (typeof closeAccountMenu === 'function') closeAccountMenu(); } catch (_) {}
      const apiUrl = await getApiUrl();
      const url = `${apiUrl}/admin`;
      try {
        // Prefer tauri-plugin-shell so the link opens in the user's default
        // browser. If the plugin isn't available (= older builds) fall back
        // to clipboard + alert so the user can still reach the page.
        if (window.__TAURI__?.shell?.open) {
          await window.__TAURI__.shell.open(url);
        } else {
          await navigator.clipboard.writeText(url);
          alert(`Admin URL copied to clipboard:\n${url}`);
        }
      } catch (err) {
        alert(`Failed to open Admin URL (${url}): ${err}`);
      }
      break;
    }
    case 'logout': {
      try { if (typeof closeAccountMenu === 'function') closeAccountMenu(); } catch (_) {}
      try {
        await invoke('cloud_logout');
      } catch (err) {
        alert(`Sign out failed: ${err}`);
        break;
      }
      // Clear app shell state so the next render lands on the project selector
      // instead of a stale dashboard. Mirrors Web's logout() which closes the
      // WS and re-renders the login screen.
      closeCloudWebSocket();
      stopPolling();
      stopWatcher();
      cloudMode = false;
      state.cloudProjectId = null;
      state.project = null;
      state.expanded.clear();
      lastProjectJson = null;
      state.projectPath = null;
      state.error = null;
      renderProjectSelector();
      break;
    }

    // ---- Project selection (path-based for Tauri local) ----
    case 'select-project': await doSelectProject(el.dataset.path); break;
    case 'select-cloud-project': await doSelectCloudProject(el.dataset.projectId); break;
    case 'menu-select-project': closeMenu(); await doSelectProject(el.dataset.path); break;
    case 'menu-select-cloud-project': closeMenu(); await doSelectCloudProject(el.dataset.projectId); break;

    // Note: switch-tab / open-document / open-retro are handled by
    // handleCommonAction (SHARED) via `dataSource`. See ms-46 e-728.

    // ---- Tauri-specific: clipboard URL format / commands ----
    case 'copy-doc-link': {
      // ms-64 / e-1461: profile-aware deep link to the Web UI.
      const apiUrl = await getApiUrl();
      const url = `${apiUrl}/?project=${state.cloudProjectId}#doc/${el.dataset.docId}`;
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
    // ms-46 e-755: cloud mode の focus 復帰では WS を主軸にする (Web と同じ
    // パターン)。loadProject() を呼ぶと REST が生データを返して state.project
    // (WS 経由で届いた enriched 版 — total_tasks/done_tasks 等を含む) を
    // 上書きしてしまう。WS が生きていれば何もしない。落ちていれば WS で
    // 再接続すると server が initial enriched data を改めて送り直してくれる。
    if (cloudWs && cloudWs.readyState === WebSocket.OPEN) return;
    connectCloudWebSocket(state.cloudProjectId);
  } else {
    // local mode は invoke('load_project_json') + file watcher 経由で、
    // server enrichment とは別経路 (CLI の count_task_status を後段で再現する
    // 必要はあるが本タスク範囲外)。
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
