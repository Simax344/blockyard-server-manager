const NAVIGATION_STATE_KEY = 'blockyard.navigation-state';
const savedNavigation = (() => {
  try {
    const saved = JSON.parse(sessionStorage.getItem(NAVIGATION_STATE_KEY) || '{}');
    return {
      view: ['servers', 'discover', 'settings'].includes(saved.view) ? saved.view : 'servers',
      tab: ['overview', 'console', 'properties', 'network', 'files', 'compatibility'].includes(saved.tab) ? saved.tab : 'overview',
      currentId: typeof saved.currentId === 'string' ? saved.currentId : null
    };
  } catch {
    return {view:'servers', tab:'overview', currentId:null};
  }
})();

const state = {
  view: savedNavigation.view, tab: savedNavigation.tab, servers: [], currentId: savedNavigation.currentId, config: null,
  query: '', results: [], searchBusy: false, searchError: '',
  curseforgeQuery: '', curseforgeResults: [], curseforgeSearchBusy: false, curseforgeSearchError: '',
  curseforgeFiles: [], curseforgeFilesBusy: false, curseforgeSelectedPack: null, curseforgeFileId: null, curseforgeSelectedFile: null, curseforgeInstallBusy: false,
  importBusy: false, importProgress: 0, importFileChosen: false, importFileName: '', importFileSize: 0,
  createBusy: false, installerMode: 'catalog', chosenPack: null,
  files: [], filesBusy: false, selectedFileId: null, selectedFile: null,
  serverFiles: [], filePath: '', fileServerId: null, fileLoading: false, fileError: '',
  fileOpenPath: '', fileEditorContent: '', fileReadBusy: false, fileSaving: false,
  fileNewFolderOpen: false, fileUploadBusy: false,
  consoleClearThrough: {},
  clientManifestPath: '', clientManifestText: '', clientManifestSavedText: '', clientManifestLoaded: false,
  clientManifestLoading: false, clientManifestSaving: false, clientManifestDirty: false,
  clientManifestLoadError: '',
  searchTimer: null, curseforgeSearchTimer: null, searchRequestId: 0, curseforgeSearchRequestId: 0, versionsRequestId: 0, curseforgeFilesRequestId: 0,
  fileListingRequestId: 0, fileEditorRequestId: 0, pollTimer: null, lastSeq: 0, pollBusy: false
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = value => new Intl.NumberFormat().format(Number(value || 0));
const currentServer = () => state.servers.find(server => server.id === state.currentId) || null;
const isActive = server => server?.status === 'running';
const setupBusy = server => server?.setup_status === 'installing';

function applyThemeColor(value) {
  const color = /^#[0-9a-f]{6}$/i.test(String(value || '')) ? String(value).toLowerCase() : '#54a9ff';
  const red = parseInt(color.slice(1, 3), 16);
  const green = parseInt(color.slice(3, 5), 16);
  const blue = parseInt(color.slice(5, 7), 16);
  const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;
  const contrast = luminance > 0.58 ? '#081321' : '#f7fbff';
  const root = document.documentElement;
  root.style.setProperty('--theme-accent', color);
  root.style.setProperty('--theme-accent-strong', `rgb(${Math.round(red * .78)}, ${Math.round(green * .78)}, ${Math.round(blue * .78)})`);
  root.style.setProperty('--theme-accent-dim', `rgba(${red}, ${green}, ${blue}, .15)`);
  root.style.setProperty('--theme-accent-rgb', `${red}, ${green}, ${blue}`);
  root.style.setProperty('--theme-accent-contrast', contrast);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'GET',
    headers: options.body ? {'Content-Type':'application/json'} : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: 'no-store'
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function loadServerFiles(path = state.filePath) {
  const server = currentServer();
  if (!server) return;
  const requestId = ++state.fileListingRequestId;
  state.fileLoading = true;
  state.fileError = '';
  state.filePath = path;
  render();
  try {
    const data = await api(`/api/servers/${encodeURIComponent(server.id)}/files?path=${encodeURIComponent(path)}`);
    if (requestId !== state.fileListingRequestId || server.id !== state.currentId) return;
    state.fileServerId = server.id;
    state.filePath = data.path || '';
    state.serverFiles = data.entries || [];
  } catch (err) {
    if (requestId === state.fileListingRequestId && server.id === state.currentId) {
      state.fileError = err.message;
      state.serverFiles = [];
    }
  } finally {
    if (requestId === state.fileListingRequestId) {
      state.fileLoading = false;
      render();
    }
  }
}

async function openServerFile(path) {
  const server = currentServer();
  if (!server) return;
  const requestId = ++state.fileEditorRequestId;
  state.fileOpenPath = path;
  state.fileEditorContent = '';
  state.fileReadBusy = true;
  render();
  try {
    const data = await api(`/api/servers/${encodeURIComponent(server.id)}/files/read?path=${encodeURIComponent(path)}`);
    if (requestId !== state.fileEditorRequestId || server.id !== state.currentId) return;
    state.fileEditorContent = data.content || '';
  } catch (err) {
    if (requestId === state.fileEditorRequestId && server.id === state.currentId) {
      state.fileOpenPath = '';
      toast(err.message, 'error');
    }
  } finally {
    if (requestId === state.fileEditorRequestId) {
      state.fileReadBusy = false;
      render();
    }
  }
}

async function uploadServerFile(file) {
  const server = currentServer();
  if (!server || !file) return;
  const uploadPath = state.filePath;
  if (!file.size || file.size > 512 * 1024 * 1024) {
    toast('Choose a non-empty file no larger than 512 MB.', 'error');
    return;
  }
  state.fileUploadBusy = true;
  render();
  try {
    const params = new URLSearchParams({path:uploadPath, filename:file.name});
    const response = await fetch(`/api/servers/${encodeURIComponent(server.id)}/files/upload?${params}`, {
      method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file, cache:'no-store'
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Upload failed (${response.status})`);
    toast(`Uploaded ${file.name}.`);
    if (currentServer()?.id === server.id && state.tab === 'files') await loadServerFiles(uploadPath);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    state.fileUploadBusy = false;
    render();
  }
}

function uploadServerPack(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', path);
    request.setRequestHeader('Content-Type', 'application/zip');
    request.upload.addEventListener('progress', event => onProgress(event.loaded, event.total || file.size));
    request.addEventListener('load', () => {
      let data = {};
      try { data = JSON.parse(request.responseText || '{}'); }
      catch { reject(new Error('The server returned an unreadable response after the upload.')); return; }
      if (request.status < 200 || request.status >= 300) reject(new Error(data.error || `Upload failed (${request.status})`));
      else resolve(data);
    });
    request.addEventListener('error', () => reject(new Error('The modpack upload was interrupted.')));
    request.send(file);
  });
}

function toast(message, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'error' ? 'error' : ''}`;
  el.textContent = message;
  $('#toast-region').append(el);
  setTimeout(() => el.remove(), 4200);
}

function statusLabel(server) {
  if (setupBusy(server)) return 'Installing';
  if (server?.setup_status === 'error') return 'Setup error';
  return isActive(server) ? 'Online' : 'Offline';
}

function statusClass(server) {
  if (setupBusy(server)) return 'installing';
  if (server?.setup_status === 'error') return 'error';
  return isActive(server) ? 'running' : '';
}

function updateSidebar() {
  $('#server-count').textContent = state.servers.length;
  $$('.nav-item[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === state.view));
  const list = $('#server-list');
  list.innerHTML = state.servers.map(server => `
    <button class="server-nav ${server.id === state.currentId ? 'active' : ''}" data-server="${esc(server.id)}" aria-label="${esc(server.name)} · ${esc(statusLabel(server))}" title="Open ${esc(server.name)}">
      <span class="server-nav-icon">▦</span>
      <span class="server-nav-copy"><span class="server-nav-name">${esc(server.name)}</span>
      <span class="server-nav-meta"><i class="tiny-status ${statusClass(server)}"></i>${esc(statusLabel(server))}</span></span>
    </button>`).join('');
  const sections = $('#server-sections');
  const server = currentServer();
  const tabs = [['overview','Overview','▦'],['console','Console','›_'],['files','Files','▤'],['properties','Properties','⚙'],['network','Network','◎'],['compatibility','Mod compatibility','◈']];
  sections.innerHTML = state.view === 'servers' && server ? `
    <div class="server-sections-heading side-label">MANAGE SERVER</div>
    <nav class="server-section-nav" aria-label="${esc(server.name)} management">
      ${tabs.map(([id,label,icon]) => `<button class="server-section-link ${state.tab === id ? 'active' : ''}" type="button" data-tab="${id}" ${state.tab === id ? 'aria-current="page"' : ''}><span class="server-section-icon">${icon}</span><span>${label}</span>${id === 'compatibility' && pendingClientModRemovals(server).length ? `<span class="server-section-count">${pendingClientModRemovals(server).length}</span>` : ''}</button>`).join('')}
    </nav>` : '';
}

function saveNavigationState() {
  try {
    sessionStorage.setItem(NAVIGATION_STATE_KEY, JSON.stringify({view:state.view, tab:state.tab, currentId:state.currentId}));
  } catch { /* Session storage may be unavailable in restricted browser contexts. */ }
}

function updateBreadcrumb() {
  const server = currentServer();
  const labels = {servers: server ? server.name.toUpperCase() : 'OVERVIEW', discover: 'DISCOVER PACKS', settings: 'SETTINGS'};
  $('#breadcrumb-current').textContent = state.view === 'servers' && server ? `${server.name.toUpperCase()} · ${state.tab.toUpperCase()}` : labels[state.view] || 'OVERVIEW';
}

function setView(view) {
  state.view = view;
  if (view === 'servers') state.tab = 'overview';
  render();
  if (view === 'discover' && !state.results.length) doSearch('');
}

function iconFallback(text = '▦', className = 'pack-icon-fallback') {
  return `<span class="${className}">${esc(text)}</span>`;
}

function imageOrFallback(src, alt, className) {
  if (!src) return iconFallback('▦', className);
  return `<img class="${className}" src="${esc(src)}" alt="${esc(alt)}" loading="lazy" onerror="this.outerHTML='<span class=\'${className} pack-fallback\'>▦</span>'">`;
}

function emptyServers() {
  return `
    <div class="page-head"><div class="page-head-left"><div class="eyebrow">YOUR WORKSPACE</div><h1>Server control</h1><p class="subheading">Your worlds, ready when you are.</p></div></div>
    <section class="hero-empty">
      <div class="hero-copy"><div class="eyebrow">A BETTER WAY TO PLAY TOGETHER</div><h2>Your next world starts here.</h2>
        <p>Find a modpack on Modrinth or import a dedicated server-pack ZIP, then let Blockyard prepare the files. Start, stop, and configure your server from one place.</p>
        <div class="hero-actions"><button class="btn btn-primary" data-view="discover"><span class="btn-icon">⌕</span> Browse modpacks</button><button class="btn btn-quiet" type="button" data-action="detect-server-folders">◎ Detect existing servers</button><button class="btn btn-quiet" data-view="settings">Server settings <span>↗</span></button></div>
      </div>
    </section>
    <div class="steps-row">
      <div class="step-card"><span class="step-number">01</span><div><h3>Find your pack</h3><p>Search Modrinth or import a server-pack ZIP from its author.</p></div></div>
      <div class="step-card"><span class="step-number">02</span><div><h3>Install automatically</h3><p>Blockyard downloads the files and prepares the server.</p></div></div>
      <div class="step-card"><span class="step-number">03</span><div><h3>Invite your crew</h3><p>Adjust settings, accept the EULA, then press Start.</p></div></div>
    </div>`;
}

function renderHeader(server) {
  const ready = server.setup_status === 'ready';
  const active = isActive(server);
  const busy = setupBusy(server);
  const pendingClientMods = pendingClientModRemovals(server).length;
  const needsEula = ready && !server.settings?.eula_accepted;
  const startTitle = needsEula
    ? 'Accept the Minecraft EULA in Properties before starting.'
    : pendingClientMods ? 'Review Mod compatibility and confirm or dismiss the client-only mod candidates.'
    : busy ? 'Wait for server setup to finish.'
    : active ? 'This server is already running.'
    : !ready ? 'Finish preparing this server before starting it.'
    : 'Start this server.';
  const stopTitle = active ? 'Gracefully stop the server and save the world.' : 'Start the server before stopping it.';
  const restartTitle = active ? 'Gracefully restart the server.' : 'Start the server before restarting it.';
  const killTitle = active ? 'Force-terminate the process. Unsaved world progress may be lost.' : 'Start the server before force-stopping it.';
  return `
    <div class="server-title-row">
      <div class="page-head-left"><div class="eyebrow">SERVER CONTROL</div>
        <div class="server-name-line"><h1>${esc(server.name)}</h1><span class="status-pill ${statusClass(server)}">${esc(statusLabel(server))}</span></div>
        <div class="server-subline"><span>${esc(server.modpack?.name || 'Minecraft modpack')}</span><i class="dot-sep"></i><span>${esc(server.minecraft_version || 'Resolving version')}</span><i class="dot-sep"></i><span>${esc(server.loader || 'Resolving loader')}</span></div>
      </div>
      <div class="control-bar" aria-label="Server controls">
        <button class="btn btn-primary" type="button" data-action="start" title="${esc(startTitle)}" ${!ready || active || busy || needsEula || pendingClientMods ? 'disabled' : ''}><span class="btn-icon">▶</span>Start</button>
        <button class="btn" type="button" data-action="restart" title="${esc(restartTitle)}" ${!ready || !active ? 'disabled' : ''}><span class="btn-icon">↻</span>Restart</button>
        <button class="btn" type="button" data-action="stop" title="${esc(stopTitle)}" ${!ready || !active ? 'disabled' : ''}><span class="btn-icon">■</span>Stop</button>
        <button class="btn btn-danger" type="button" data-action="kill" title="${esc(killTitle)}" ${!ready || !active ? 'disabled' : ''}><span class="btn-icon">✕</span>Force stop</button>
      </div>
    </div>
    <div class="tabs" role="tablist">
      ${[['overview','Overview'],['console','Console'],['properties','Properties'],['network','Network'],['files','Files'],['compatibility','Mod compatibility']].map(([id,label]) => `<button class="tab ${state.tab === id ? 'active' : ''}" type="button" role="tab" aria-selected="${state.tab === id}" data-tab="${id}">${label}</button>`).join('')}
    </div>`;
}

function logLines(logs) {
  const server = currentServer();
  const clearThrough = Number(state.consoleClearThrough[server?.id] || 0);
  const visibleLogs = (logs || []).filter(line => Number(line.seq || 0) > clearThrough);
  if (!visibleLogs.length) {
    const message = clearThrough
      ? 'Console view cleared. New output will appear here.'
      : setupBusy(server) ? 'Preparing your server… setup messages will appear here.' : 'No console output yet. Start the server to see live logs.';
    return `<div class="console-empty">${message}</div>`;
  }
  return visibleLogs.map(line => `<div class="console-line ${esc(line.level || '')}"><span class="console-time">${esc(line.time || '')}</span><span>${esc(line.text || '')}</span></div>`).join('');
}

function visibleConsoleText(server = currentServer()) {
  if (!server) return '';
  const clearThrough = Number(state.consoleClearThrough[server.id] || 0);
  return (server.logs || [])
    .filter(line => Number(line.seq || 0) > clearThrough)
    .map(line => `${line.time ? `[${line.time}] ` : ''}${line.text || ''}`)
    .join('\n');
}

function overviewPanel(server) {
  const settings = server.settings || {};
  const packIcon = server.modpack?.icon;
  return `
    <div class="overview-grid">
      <div class="panel">
        <div class="panel-head"><h2>Server overview</h2><span class="source-chip">${esc((server.source || server.modpack?.source || 'modrinth').toUpperCase())}</span></div>
        <div class="panel-body">
          <div class="pack-card">${imageOrFallback(packIcon, server.modpack?.name || '', 'pack-icon')}<div class="pack-copy"><strong>${esc(server.modpack?.name || 'Minecraft modpack')}</strong><span>${esc(server.pack_version || 'Latest selected release')} · ${esc(server.minecraft_version || 'Minecraft version pending')}</span></div></div>
          <div class="stat-grid">
            <div class="stat-box"><div class="stat-label">Minecraft</div><div class="stat-value">${esc(server.minecraft_version || '—')}</div></div>
            <div class="stat-box"><div class="stat-label">Loader</div><div class="stat-value">${esc(server.loader || '—')}</div></div>
            <div class="stat-box"><div class="stat-label">Memory limit</div><div class="stat-value">${number(settings.ram_mb || 4096)} MB</div></div>
            <div class="stat-box"><div class="stat-label">Process</div><div class="stat-value small">${server.pid ? `RUNNING · PID ${esc(server.pid)}` : 'STOPPED'}</div></div>
          </div>
        </div>
        <div class="panel-head"><h3>Live console</h3><button class="btn btn-small btn-quiet" data-tab="console">Open console ↗</button></div>
        <div class="panel-body"><div class="mini-console"><div class="mini-console-top"><span><span class="console-lights"><span></span><span></span><span></span></span> &nbsp; SERVER OUTPUT</span><span>${isActive(server) ? 'LIVE' : 'IDLE'}</span></div><div class="mini-console-body" id="mini-console">${logLines(server.logs)}</div></div></div>
      </div>
      <div class="side-stack">
        <div class="panel connection-panel"><div class="panel-head"><h3>Server address</h3><span class="status-pill ${isActive(server) ? 'running' : ''}">${isActive(server) ? 'Online' : 'Offline'}</span></div><div class="panel-body">
          <div class="address-box"><span>localhost:${esc(settings.port || 25565)}</span><button class="copy-button" title="Copy address" data-copy="localhost:${esc(settings.port || 25565)}">▢</button></div>
          <p class="connection-note">This address works on this computer. Friends outside your home network may need your public IP and router port forwarding.</p>
          <button class="btn btn-small btn-quiet" data-tab="network" style="margin-top:12px">Network settings ↗</button>
        </div></div>
        <div class="panel"><div class="panel-head"><h3>Quick settings</h3><button class="btn btn-small btn-quiet" data-tab="properties">Edit ↗</button></div><div class="panel-body"><div class="info-list quick-settings-list">
          <div class="info-row"><span>Message</span><span>${esc(settings.motd || server.name)}</span></div>
          <div class="info-row"><span>Max players</span><span>${esc(settings.max_players || 20)}</span></div>
          <div class="info-row"><span>Difficulty</span><span>${esc(settings.difficulty || 'normal')}</span></div>
          <div class="info-row"><span>View distance</span><span>${esc(settings.view_distance || 10)} chunks</span></div>
        </div></div></div>
        <div class="panel"><div class="panel-head"><h3>Install details</h3></div><div class="panel-body"><div class="info-list">
          <div class="info-row"><span>Folder</span><span class="mono">${esc(server.path)}</span></div>
          <div class="info-row"><span>Pack release</span><span>${esc(server.pack_version || 'Resolving…')}</span></div>
          <div class="info-row"><span>Created</span><span>${esc(new Date(server.created_at).toLocaleDateString())}</span></div>
        </div></div></div>
      </div>
    </div>`;
}

function consolePanel(server) {
  return `<div class="section-heading"><div><h2>Server console</h2><p class="subheading">Live output and in-game server commands.</p></div><div class="console-heading-actions"><button class="btn btn-small btn-quiet" data-action="copy-console" type="button" title="Copy the visible console output to the clipboard">Copy console</button><button class="btn btn-small btn-quiet" id="clear-console" data-action="clear-console" type="button" title="Clear the visible log history; new output will still appear">Clear view</button></div></div>
    <div class="console-panel"><div class="console-toolbar"><div class="console-toolbar-title"><span class="tiny-status ${isActive(server) ? 'running' : ''}"></span> ${esc(server.name)} <span style="color:#59665a">/</span> console</div><span class="mono" style="font-size:10px;color:#718071">${isActive(server) ? 'CONNECTED' : 'DISCONNECTED'}</span></div>
    <div class="console-viewport" id="full-console">${logLines(server.logs)}</div>
    <form class="console-input-row" id="console-form"><span class="console-prompt">›</span><input id="console-input" autocomplete="off" placeholder="${isActive(server) ? 'Enter a command, e.g. list or say Hello' : 'Start the server to send commands'}" ${isActive(server) ? '' : 'disabled'}><button class="btn btn-primary" type="submit" title="${isActive(server) ? 'Send this command to the Minecraft server.' : 'Start the server before sending commands.'}" ${isActive(server) ? '' : 'disabled'}>Send</button></form></div>`;
}

function switchMarkup(name, title, help, checked) {
  return `<div class="toggle-row"><div class="toggle-copy"><strong>${esc(title)}</strong><span>${esc(help)}</span></div><input class="toggle" type="checkbox" name="${esc(name)}" ${checked ? 'checked' : ''}></div>`;
}

function javaVersionMajor(version) {
  const match = String(version || '').trim().match(/^(\d+)(?:\.(\d+))?/);
  if (!match) return null;
  const major = Number(match[1]);
  return major === 1 && match[2] ? Number(match[2]) : major;
}

function javaRequirementForMinecraft(version) {
  const match = String(version || '').trim().match(/^(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!match) return null;
  const [gameMajor, gameMinor, gamePatch] = match.slice(1).map(value => Number(value || 0));
  if (gameMajor === 26 && gameMinor >= 1) return {major:25, minimum:true};
  if (gameMajor !== 1) return null;
  if (gameMinor === 21 || (gameMinor === 20 && gamePatch >= 5)) return {major:21, minimum:false};
  if (gameMinor >= 18 && gameMinor <= 20) return {major:17, minimum:false};
  if (gameMinor === 17) return {major:16, minimum:false};
  if (gameMinor >= 1 && gameMinor <= 16) return {major:8, minimum:false};
  return null;
}

function javaMeetsRequirement(runtime, requirement) {
  const major = javaVersionMajor(runtime?.version);
  return major !== null && (requirement.minimum ? major >= requirement.major : major === requirement.major);
}

function recommendedJavaRuntime(runtimes, requirement) {
  return requirement ? runtimes.find(runtime => javaMeetsRequirement(runtime, requirement)) || null : null;
}

function updateJavaRecommendationCard(server, selectedRuntime, javaPath = '') {
  const card = $('#java-recommendation-card');
  const status = $('#java-recommendation-status');
  if (!card || !status) return;
  const requirement = javaRequirementForMinecraft(server?.minecraft_version);
  if (!requirement) return;
  const runtimes = state.config?.java_runtimes || [];
  const available = recommendedJavaRuntime(runtimes, requirement);
  const selectedMajor = javaVersionMajor(selectedRuntime?.version);
  const matches = javaMeetsRequirement(selectedRuntime, requirement);
  const label = `Java ${requirement.major}${requirement.minimum ? ' or newer' : ''}`;
  card.classList.toggle('ready', matches);
  card.classList.toggle('attention', !matches);
  if (matches) {
    status.textContent = `Java ${selectedRuntime.version} is selected and matches Minecraft ${server.minecraft_version}.`;
  } else if (selectedMajor !== null) {
    status.textContent = `This server is set to Java ${selectedMajor}; ${label} is recommended for Minecraft ${server.minecraft_version}.`;
  } else if (javaPath) {
    status.textContent = `Blockyard could not verify the selected Java path. ${label} is recommended for Minecraft ${server.minecraft_version}.`;
  } else {
    status.textContent = `${label} is recommended for Minecraft ${server.minecraft_version}.`;
  }
  if (available && !matches) status.textContent += ` A compatible runtime is detected; select it below.`;
  else if (!available) status.textContent += ` No compatible runtime was detected. Install ${label}, then click Find Java.`;
  const useButton = $('#use-recommended-java');
  if (useButton) {
    useButton.hidden = !available || matches;
    if (available) useButton.textContent = `Use Java ${javaVersionMajor(available.version)}`;
  }
  const downloadLink = $('#download-recommended-java');
  if (downloadLink) downloadLink.hidden = !!available;
}

function javaRecommendationMarkup(server, runtimes, selectedRuntime, javaInputValue) {
  const requirement = javaRequirementForMinecraft(server.minecraft_version);
  if (!requirement) {
    return `<div class="field full"><div class="java-recommendation unknown"><div><strong>Java version recommendation unavailable</strong><p>Blockyard could not match this Minecraft version to a Java recommendation. Check the modpack or loader documentation.</p></div></div></div>`;
  }
  const available = recommendedJavaRuntime(runtimes, requirement);
  const selectedMatches = javaMeetsRequirement(selectedRuntime, requirement);
  const label = `Java ${requirement.major}${requirement.minimum ? ' or newer' : ''}`;
  const downloadVersion = requirement.major;
  const downloadUrl = `https://adoptium.net/temurin/releases/?version=${downloadVersion}`;
  let status;
  const selectedMajor = javaVersionMajor(selectedRuntime?.version);
  if (selectedMatches) status = `Java ${selectedRuntime.version} is selected and matches Minecraft ${server.minecraft_version}.`;
  else if (selectedMajor !== null) status = `This server is set to Java ${selectedMajor}; ${label} is recommended for Minecraft ${server.minecraft_version}.`;
  else if (javaInputValue) status = `Blockyard could not verify the selected Java path. ${label} is recommended for Minecraft ${server.minecraft_version}.`;
  else status = `${label} is recommended for Minecraft ${server.minecraft_version}.`;
  if (available && !selectedMatches) status += ` A compatible runtime is detected; select it below.`;
  else if (!available) status += ` No compatible runtime was detected. Install ${label}, then click Find Java.`;
  return `<div class="field full"><div class="java-recommendation ${selectedMatches ? 'ready' : 'attention'}" id="java-recommendation-card" role="status" aria-live="polite"><div class="java-recommendation-copy"><strong>Recommended runtime: ${esc(label)}</strong><p id="java-recommendation-status">${esc(status)}</p></div><div class="java-recommendation-actions"><button class="btn btn-small btn-primary" type="button" id="use-recommended-java" data-action="use-recommended-java" ${available && !selectedMatches ? '' : 'hidden'}>Use Java ${esc(available ? javaVersionMajor(available.version) : requirement.major)}</button><a class="btn btn-small btn-quiet" id="download-recommended-java" href="${esc(downloadUrl)}" target="_blank" rel="noreferrer" ${available ? 'hidden' : ''}>Get Java ${esc(requirement.major)} ↗</a></div></div><span class="field-hint">This is the usual Minecraft runtime recommendation. A pack author or loader may specify a different version.</span></div>`;
}

function propertiesPanel(server) {
  const s = server.settings || {};
  const detectedJava = state.config?.java_runtime || {};
  const javaRuntimes = state.config?.java_runtimes || [];
  const savedJava = String(s.java_path || 'java');
  const javaInputValue = savedJava.toLowerCase() === 'java' && detectedJava.path ? detectedJava.path : savedJava;
  const selectedRuntime = javaRuntimes.find(item => String(item.path).toLowerCase() === javaInputValue.toLowerCase());
  const runtimeOptions = javaRuntimes.map(item => `<option value="${esc(item.path)}" ${selectedRuntime?.path === item.path ? 'selected' : ''}>Java ${esc(item.version)} · ${esc(item.source)} · ${esc(item.path)}</option>`).join('');
  const javaDetectionText = detectedJava.path
    ? `Found Java ${detectedJava.version} in ${detectedJava.source}: ${detectedJava.path}`
    : 'No Java installation was found in the usual host locations. Install Java or enter its executable path.';
  const javaRecommendation = javaRecommendationMarkup(server, javaRuntimes, selectedRuntime, javaInputValue);
  const javaControl = `<div class="field"><label for="java-runtime-select">Detected Java version</label><select id="java-runtime-select" class="select"><option value="" ${selectedRuntime ? '' : 'selected'}>${javaRuntimes.length ? 'Custom path or PATH lookup' : 'No installed Java detected'}</option>${runtimeOptions}</select><span class="field-hint">Select an installed runtime, or leave this on custom and enter a path below.</span></div><div class="field full"><label for="java_path">Java executable</label><div style="display:flex;gap:8px;align-items:center"><input id="java_path" class="input" name="java_path" value="${esc(javaInputValue)}" placeholder="java or full path to java.exe" style="min-width:0;flex:1"><button class="btn btn-small btn-quiet" type="button" data-action="find-java">Find Java</button></div><span class="field-hint" id="java-detection-status">${esc(javaDetectionText)}</span><span class="field-hint">The detector checks this computer’s PATH, Java install folders, Windows registry, and Minecraft Launcher runtimes. <a href="https://adoptium.net/temurin/releases/" target="_blank" rel="noreferrer">Download Temurin ↗</a></span></div>`;
  return `<div class="section-heading"><div><h2>Server properties</h2><p class="subheading">Tune gameplay, player limits, and the runtime used to launch this world.</p></div><button class="btn btn-primary" type="submit" form="properties-form">Save changes</button></div>
    <form id="properties-form" class="panel"><div class="settings-section"><div class="settings-section-head"><h2>Gameplay</h2><p>These settings are written to the server’s server.properties file.</p></div>
      <div class="field-grid">
        <div class="field full"><label for="motd">Server message (MOTD)</label><input id="motd" class="input" name="motd" value="${esc(s.motd || server.name)}" maxlength="200"><span class="field-hint">Shown in the multiplayer server list.</span></div>
        <div class="field"><label for="max_players">Maximum players</label><input id="max_players" class="input" name="max_players" type="number" min="1" max="1000" value="${esc(s.max_players || 20)}"></div>
        <div class="field"><label for="difficulty">Difficulty</label><select id="difficulty" class="select" name="difficulty">${['peaceful','easy','normal','hard'].map(x => `<option ${s.difficulty === x ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
        <div class="field"><label for="gamemode">Default game mode</label><select id="gamemode" class="select" name="gamemode">${['survival','creative','adventure','spectator'].map(x => `<option ${s.gamemode === x ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
        <div class="field"><label for="view_distance">View distance <span class="field-hint">chunks</span></label><input id="view_distance" class="input" name="view_distance" type="number" min="2" max="32" value="${esc(s.view_distance || 10)}"></div>
        <div class="field"><label for="simulation_distance">Simulation distance <span class="field-hint">chunks</span></label><input id="simulation_distance" class="input" name="simulation_distance" type="number" min="2" max="32" value="${esc(s.simulation_distance || 10)}"></div>
      </div>
    </div>
        <div class="settings-section"><div class="settings-section-head"><h2>Gameplay rules</h2><p>Choose how players can interact with the world.</p></div><div class="field-grid">
      ${switchMarkup('pvp','Player versus player','Allow players to damage one another.',s.pvp !== false)}
      ${switchMarkup('allow_flight','Allow flight','Prevent kick for flying when using allowed mods.',s.allow_flight)}
    </div></div>
    <div class="settings-section"><div class="settings-section-head"><h2>Java runtime</h2><p>Use a Java version compatible with the selected Minecraft release and mod loader.</p></div>
      <div class="field-grid">
        <div class="field"><label for="ram_mb">Maximum memory <span class="field-hint">MB</span></label><input id="ram_mb" class="input" name="ram_mb" type="number" min="1024" max="131072" step="512" value="${esc(s.ram_mb || 4096)}"></div>
        ${javaRecommendation}
        ${javaControl}
      </div>
    </div>
    <div class="settings-section"><div class="settings-section-head"><h2>Minecraft EULA</h2><p>The server will not start until you confirm this setting.</p></div>
      <div class="warning-card"><strong>By enabling this, you confirm that you have read and agree to the Minecraft EULA.</strong> This writes eula=true into the server folder. Review the terms on <a href="https://www.minecraft.net/eula" target="_blank" rel="noreferrer">Minecraft.net</a>.</div>
      <div style="margin-top:12px">${switchMarkup('eula_accepted','I agree to the Minecraft EULA','Required before this server can start.',s.eula_accepted)}</div>
    </div></form>
    <section class="panel danger-zone"><div><h2>Delete server</h2><p>Remove this server from Blockyard. You can keep the server folder or permanently delete its files.</p></div><button class="btn btn-danger" type="button" data-action="delete-server">Delete server…</button></section>`;
}

function pendingClientModRemovals(server) {
  return (server?.mod_compatibility?.mods || []).filter(mod => mod.status === 'Client only' && mod.installed && !mod.quarantined);
}

function compatibilityPanel(server) {
  const report = server.mod_compatibility;
  if (!report) return `<div class="section-heading"><div><h2>Mod compatibility</h2><p class="subheading">Server-side check for the installed modpack.</p></div></div><section class="panel compatibility-empty"><strong>No compatibility report yet</strong><span>${setupBusy(server) ? 'The report will appear after the Modrinth pack check completes.' : 'This server pack did not include Modrinth environment metadata to classify its mods.'}</span></section>`;
  const counts = report.counts || {};
  const pending = pendingClientModRemovals(server);
  const active = isActive(server);
  const curatedListNote = report.client_mod_list_version
    ? `Curated rules ${esc(report.client_mod_list_version)} from <a href="${esc(report.client_mod_list_source || 'https://github.com/LianJordaan/client-side-mods-list')}" target="_blank" rel="noreferrer">the evidence-backed client-side mod list</a> were included when this report was checked.`
    : 'The curated client-side mod list is not available yet; Blockyard is using mod metadata and its local learned manifest.';
  const rows = (report.mods || []).map(mod => {
    const alternative = mod.alternative ? `<a class="btn btn-small btn-quiet" href="${esc(mod.alternative.url)}" target="_blank" rel="noreferrer" title="${esc(mod.alternative.filename)}">Server release ${esc(mod.alternative.version)} ↗</a>` : '';
    const confirm = mod.status === 'Client only' && mod.installed && !mod.quarantined
      ? `<button class="btn btn-small btn-danger" type="button" data-remove-client-mod="${esc(mod.path)}" ${active ? 'disabled title="Stop the server before moving this mod."' : 'title="Confirm and move this mod to the recovery folder."'}>Confirm removal</button>`
      : '';
    const restore = ['Client only', 'Protected library'].includes(mod.status) && mod.quarantined && !mod.installed
      ? `<button class="btn btn-small btn-quiet" type="button" data-restore-client-mod="${esc(mod.path)}" ${active ? 'disabled title="Stop the server before restoring this mod."' : 'title="Restore this mod from the recovery folder."'}>Undo remove</button>`
      : '';
    const actions = alternative || confirm || restore ? `${alternative}${confirm}${restore}` : '<span class="compat-no-alt">—</span>';
    const status = mod.status || 'Unverified';
    return `<div class="compat-row"><div class="compat-mod"><strong>${esc(mod.name)}</strong><span class="mono">${esc(mod.path)}</span></div><span class="compat-status ${status === 'Server compatible' ? 'good' : status === 'Protected library' ? 'protected' : status === 'Client only' || status.startsWith('Skipped') ? 'warn' : ''}" title="${esc(mod.reason || '')}">${esc(status)}</span><span class="compat-install" title="${mod.quarantined ? `Recoverable copy: ${esc(mod.backup_path)}` : ''}">${mod.quarantined ? 'Moved aside' : mod.installed ? 'Included' : status.startsWith('Skipped') ? 'Skipped' : 'Not installed'}</span><span class="compat-actions">${actions}</span></div>`;
  }).join('');
  return `<div class="section-heading"><div><h2>Mod compatibility</h2><p class="subheading">Checked for Minecraft ${esc(report.minecraft || server.minecraft_version)} · ${esc(report.loader || server.loader)}. Confirm client-only candidates to move them out of the server mods folder.</p></div>${pending.length ? `<button class="btn btn-danger" type="button" data-action="confirm-all-client-mods" ${active ? 'disabled title="Stop the server before moving these mods."' : ''}>Confirm all ${pending.length}</button>` : ''}</div>
    <div class="compat-summary"><div><strong>${number(counts.total)}</strong><span>mods found</span></div><div><strong>${number(counts.server_compatible)}</strong><span>server compatible</span></div><div><strong>${number(counts.client_only)}</strong><span>client only</span></div><div><strong>${number(counts.alternatives_found)}</strong><span>server releases found</span></div></div>
    <div class="compat-note">Candidates come from mod metadata, the Blockyard client-only manifest, startup crash evidence, and high-confidence rules in the curated list. ${curatedListNote} Protected libraries are never suggested for removal. Each candidate stays in place until you confirm its removal. Confirmed JARs move to <span class="mono">blockyard-client-only-mods/</span> so you can restore them. Blockyard checked ${number(report.alternatives_checked || 0)} Modrinth projects for possible server releases; alternatives are never substituted automatically.</div>
    <section class="panel compatibility-list"><div class="compat-head"><span>Mod</span><span>Compatibility</span><span>Server files</span><span>Alternative / action</span></div>${rows || '<div class="compatibility-empty">No mods were listed in this pack.</div>'}</section>`;
}

function networkPanel(server) {
  const s = server.settings || {};
  return `<div class="section-heading"><div><h2>Network &amp; access</h2><p class="subheading">Choose the port players use to connect to this server.</p></div><button class="btn btn-primary" type="submit" form="network-form">Save changes</button></div>
    <form id="network-form" class="panel"><div class="settings-section"><div class="settings-section-head"><h2>Connection details</h2><p>Blockyard updates this port in server.properties.</p></div><div class="field-grid">
      <div class="field"><label for="port">Server port</label><input id="port" class="input" name="port" type="number" min="1" max="65535" value="${esc(s.port || 25565)}"><span class="field-hint">Default Minecraft Java Edition port is 25565.</span></div>
      <div class="field"><label>Local address</label><input class="input" value="localhost:${esc(s.port || 25565)}" readonly><span class="field-hint">For players on this computer. LAN players use this device’s local IP.</span></div>
    </div></div><div class="settings-section"><div class="settings-section-head"><h2>Share with friends</h2><p>Your router and firewall are outside the app’s control.</p></div>
      <div class="notice-card"><strong style="color:#dce5d8">To let friends join over the internet:</strong><br>Forward TCP port <span class="mono">${esc(s.port || 25565)}</span> on your router to this computer, allow Java through your firewall, then share your public IP with the port. Only share it with people you trust.</div>
    </div><div class="settings-section"><div class="settings-section-head"><h2>Access controls</h2><p>Keep player verification and whitelist settings easy to find.</p></div>
      <div class="field-grid">${switchMarkup('online_mode','Online mode','Verify player accounts with Minecraft services.',s.online_mode !== false)}${switchMarkup('whitelist','Whitelist','Only allow players listed in whitelist.json.',s.whitelist)}</div>
    </div></form>`;
}

function formatFileSize(bytes) {
  const size = Number(bytes || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fileBreadcrumbs() {
  const parts = state.filePath ? state.filePath.split('/').filter(Boolean) : [];
  let path = '';
  const crumbs = [`<button type="button" class="file-crumb" data-file-dir="">Server root</button>`];
  parts.forEach((part, index) => {
    path = path ? `${path}/${part}` : part;
    crumbs.push(`<span class="file-crumb-sep">/</span><button type="button" class="file-crumb" data-file-dir="${esc(path)}" ${index === parts.length - 1 ? 'aria-current="page"' : ''}>${esc(part)}</button>`);
  });
  return crumbs.join('');
}

function filesPanel(server) {
  const rows = state.serverFiles.map(entry => entry.type === 'directory'
    ? `<div class="file-row"><button type="button" class="file-name-button" data-file-dir="${esc(entry.path)}"><span class="file-icon folder">▰</span><span class="file-name-copy"><strong>${esc(entry.name)}</strong><small>Folder</small></span></button><span class="file-modified">${esc(entry.modified || '')}</span><span class="file-size">—</span><span class="file-row-actions"></span></div>`
    : `<div class="file-row"><button type="button" class="file-name-button" data-file-open="${esc(entry.path)}"><span class="file-icon">▤</span><span class="file-name-copy"><strong>${esc(entry.name)}</strong><small>File</small></span></button><span class="file-modified">${esc(entry.modified || '')}</span><span class="file-size">${formatFileSize(entry.size)}</span><span class="file-row-actions"><button class="btn btn-small btn-quiet" type="button" data-file-download="${esc(entry.path)}" title="Download ${esc(entry.name)}">Download</button><button class="btn btn-small btn-quiet file-delete-button" type="button" data-file-delete="${esc(entry.path)}" title="Delete ${esc(entry.name)}">Delete</button></span></div>`
  ).join('');
  const folderForm = state.fileNewFolderOpen ? `<form id="new-folder-form" class="new-folder-form"><label for="new-folder-name">New folder name</label><input class="input" id="new-folder-name" name="name" maxlength="255" required autofocus><button class="btn btn-primary btn-small" type="submit">Create folder</button><button class="btn btn-quiet btn-small" type="button" data-action="cancel-new-folder">Cancel</button></form>` : '';
  const editor = state.fileOpenPath ? `<form id="file-editor-form" class="file-editor"><div class="file-editor-head"><div><strong>${esc(state.fileOpenPath.split('/').pop())}</strong><span class="mono">${esc(state.fileOpenPath)}</span></div><button class="btn btn-small btn-quiet" type="button" data-action="close-file-editor">Close</button></div><textarea class="input file-editor-text" id="file-editor-content" spellcheck="false" ${state.fileReadBusy ? 'disabled' : ''}>${esc(state.fileEditorContent)}</textarea><div class="file-editor-foot"><span class="field-hint">Text editor · up to 2 MB. Restart the server if it needs to reload this file.</span><button class="btn btn-primary btn-small" type="submit" ${state.fileReadBusy || state.fileSaving ? 'disabled' : ''}>${state.fileSaving ? 'Saving…' : 'Save file'}</button></div></form>` : '';
  return `<div class="section-heading file-manager-heading"><div><h2>Server files</h2><p class="subheading">Browse, edit text files, and manage files in this server folder.</p></div><div class="file-toolbar-actions"><button class="btn btn-small btn-quiet" type="button" data-action="refresh-server-files" ${state.fileLoading ? 'disabled' : ''}>↻ Refresh</button><button class="btn btn-small" type="button" data-action="new-server-folder" ${state.fileLoading ? 'disabled' : ''}>＋ New folder</button><button class="btn btn-small btn-primary" type="button" data-action="upload-server-file" ${state.fileUploadBusy ? 'disabled' : ''}>${state.fileUploadBusy ? 'Uploading…' : '↑ Upload file'}</button><input type="file" id="server-file-upload" class="visually-hidden" aria-label="Upload a server file" ${state.fileUploadBusy ? 'disabled' : ''}></div></div>
    <div class="file-manager-note"><span>📁</span><span>Files stay inside <strong>${esc(server.path)}</strong>. Avoid editing or deleting world files while the server is running.</span></div>
    ${folderForm}
    <div class="file-manager-layout ${editor ? 'has-editor' : ''}"><section class="panel file-browser"><div class="file-browser-head"><div class="file-breadcrumbs">${fileBreadcrumbs()}</div><span class="mono file-count">${state.fileLoading ? 'LOADING…' : `${state.serverFiles.length} ITEMS`}</span></div>
      ${state.fileError ? `<div class="file-error">${esc(state.fileError)} <button class="btn btn-small btn-quiet" type="button" data-action="refresh-server-files">Try again</button></div>` : state.fileLoading && !state.serverFiles.length ? `<div class="file-empty"><span class="loading-dots"><i></i><i></i><i></i></span>Loading server files…</div>` : state.serverFiles.length ? `<div class="file-list"><div class="file-list-labels"><span>Name</span><span>Modified</span><span>Size</span><span></span></div>${rows}</div>` : `<div class="file-empty"><span class="file-empty-icon">▤</span><strong>This folder is empty</strong><span>Upload a file or create a folder to get started.</span></div>`}</section>${editor}</div>`;
}

function serverContent(server) {
  let inner = '';
  if (state.tab === 'overview') inner = overviewPanel(server);
  if (state.tab === 'console') inner = consolePanel(server);
  if (state.tab === 'properties') inner = propertiesPanel(server);
  if (state.tab === 'network') inner = networkPanel(server);
  if (state.tab === 'files') inner = filesPanel(server);
  if (state.tab === 'compatibility') inner = compatibilityPanel(server);
  let banner = '';
  if (setupBusy(server)) banner = `<div class="setup-banner"><span class="loading-dots"><i></i><i></i><i></i></span><span class="banner-copy"><span class="banner-label">Setting up your server</span>Downloading the pack, applying files, and preparing the loader. Progress appears in the console.</span></div>`;
  else if (server.setup_status === 'error') banner = `<div class="setup-banner error"><span>!</span><span class="banner-copy"><span class="banner-label">Setup needs attention</span>${esc(server.setup_error || 'The server could not be prepared. Review the console output for details.')}</span><button class="btn btn-small btn-quiet" data-tab="console">Open console</button></div>`;
  else if (pendingClientModRemovals(server).length) banner = `<div class="setup-banner error"><span>!</span><span class="banner-copy"><span class="banner-label">Review client-only mod candidates</span>${pendingClientModRemovals(server).length} flagged mod${pendingClientModRemovals(server).length === 1 ? '' : 's'} need your confirmation before this server can start.</span><button class="btn btn-small btn-quiet" type="button" data-tab="compatibility">Review mods</button></div>`;
  else if (server.setup_status === 'ready' && !server.settings?.eula_accepted) banner = `<div class="setup-banner"><span>!</span><span class="banner-copy"><span class="banner-label">Accept the Minecraft EULA to start</span>Review the EULA in Properties. Start becomes available after you save your choice.</span><button class="btn btn-small btn-quiet" type="button" data-tab="properties">Review EULA</button></div>`;
  return `${renderHeader(server)}${banner}${inner}`;
}

function installerStepStates(mode) {
  if (mode === 'import') {
    if (state.importBusy) return ['done', 'done', 'current'];
    if (state.importFileChosen) return ['done', 'current', 'upcoming'];
    return ['current', 'upcoming', 'upcoming'];
  }
  const curseforge = mode === 'curseforge';
  const pack = curseforge ? state.curseforgeSelectedPack : state.chosenPack;
  const fileId = curseforge ? state.curseforgeFileId : state.selectedFileId;
  const filesBusy = curseforge ? state.curseforgeFilesBusy : state.filesBusy;
  const releaseReady = !!pack && !!fileId && !filesBusy;
  if (releaseReady) return ['done', 'done', 'current'];
  if (pack) return ['done', 'current', 'upcoming'];
  return ['current', 'upcoming', 'upcoming'];
}

function renderInstallerSteps(mode) {
  const importing = mode === 'import';
  const curseforge = mode === 'curseforge';
  const states = installerStepStates(mode);
  const selectedPack = curseforge ? state.curseforgeSelectedPack : state.chosenPack;
  const selectedFile = curseforge ? state.curseforgeSelectedFile : state.selectedFile;
  const pack = selectedPack?.name;
  const version = selectedFile?.game_versions?.[0];
  const loaders = selectedFile?.loaders || [];
  const release = selectedFile ? `${selectedFile.name} · Minecraft ${version || 'version from pack metadata'}${loaders.length ? ` · ${loaders.join('/')}` : ''}` : '';
  const archive = state.importFileName;
  const steps = importing ? [
    ['Choose a pack archive', archive || 'Select a ZIP or .mrpack file'],
    ['Review server details', 'Set name and location; Minecraft version and loader come from pack metadata'],
    ['Convert and prepare', state.importBusy ? 'Uploading your archive…' : 'Resolve server-side mods and keep removed client-only mods recoverable']
  ] : [
    ['Choose a modpack', pack || `Search ${curseforge ? 'CurseForge' : 'Modrinth'} and select a pack`],
    ['Choose a release', (curseforge ? state.curseforgeFilesBusy : state.filesBusy) ? 'Loading compatible releases…' : release || 'Select a Minecraft release and enter server details'],
    ['Prepare the server', (curseforge ? state.curseforgeInstallBusy : state.createBusy) ? 'Adding the server…' : curseforge ? 'Fetch profile files and convert for a dedicated server' : 'Download files and resolve server-side requirements']
  ];
  return `<ol class="installer-steps" aria-label="Server setup steps">${steps.map(([title, detail], index) => {
    const status = states[index];
    const label = status === 'done' ? 'Complete' : status === 'current' ? 'Current step' : 'Upcoming';
    return `<li class="installer-step is-${status}" data-installer-step="${index + 1}" ${status === 'current' ? 'aria-current="step"' : ''}><span class="installer-step-number" aria-hidden="true">${status === 'done' ? '✓' : index + 1}</span><span class="installer-step-copy"><strong>${title}</strong><small>${esc(detail)}</small></span><span class="visually-hidden">${label}</span></li>`;
  }).join('')}</ol>`;
}

function syncInstallerSteps() {
  const states = installerStepStates(state.installerMode);
  const curseforge = state.installerMode === 'curseforge';
  const selectedRelease = curseforge ? state.curseforgeSelectedFile : state.selectedFile;
  const selectedPack = curseforge ? state.curseforgeSelectedPack : state.chosenPack;
  const filesBusy = curseforge ? state.curseforgeFilesBusy : state.filesBusy;
  const releaseDetail = selectedRelease
    ? `${selectedRelease.name} · Minecraft ${selectedRelease.game_versions?.[0] || 'version from pack metadata'}${selectedRelease.loaders?.length ? ` · ${selectedRelease.loaders.join('/')}` : ''}`
    : 'Select a Minecraft release and enter server details';
  const details = state.installerMode === 'import'
    ? [state.importFileName ? `${state.importFileName} · ${formatFileSize(state.importFileSize)}` : 'Select a ZIP or .mrpack file', 'Set name and location; Minecraft version and loader come from pack metadata', state.importBusy ? 'Uploading your archive…' : 'Resolve server-side mods and keep removed client-only mods recoverable']
    : [selectedPack?.name || `Search ${curseforge ? 'CurseForge' : 'Modrinth'} and select a pack`, filesBusy ? 'Loading compatible releases…' : releaseDetail, (curseforge ? state.curseforgeInstallBusy : state.createBusy) ? 'Adding the server…' : curseforge ? 'Fetch profile files and convert for a dedicated server' : 'Use this release version and look for server-side mod versions'];
  $$('.installer-step').forEach((step, index) => {
    const status = states[index];
    step.classList.remove('is-done', 'is-current', 'is-upcoming');
    step.classList.add(`is-${status}`);
    if (status === 'current') step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
    const number = $('.installer-step-number', step);
    if (number) number.textContent = status === 'done' ? '✓' : String(index + 1);
    const detail = $('.installer-step-copy small', step);
    if (detail) detail.textContent = details[index];
  });
}

function renderDiscover() {
  const options = state.files.map(file => {
    const date = file.date ? new Date(file.date).toLocaleDateString() : '';
    const releaseTags = ` · ${esc((file.game_versions || []).slice(0, 2).join(', '))}${file.loaders?.length ? ` · ${esc(file.loaders.join('/'))}` : ''}`;
    return `<option value="${esc(file.id)}" ${String(state.selectedFileId) === String(file.id) ? 'selected' : ''}>${esc(file.name)}${releaseTags}${date ? ` · ${esc(date)}` : ''}</option>`;
  }).join('');
  const searchArea = state.searchBusy ? `<div class="empty-results"><span class="loading-dots"><i></i><i></i><i></i></span>Searching Modrinth…</div>` : state.searchError ? `<div class="empty-results"><strong>Couldn’t load packs</strong>${esc(state.searchError)}<br><button class="btn btn-small" data-action="search-retry" style="margin-top:12px">Try again</button></div>` : state.results.length ? state.results.map(pack => `
      <article class="result-card ${state.chosenPack?.id === pack.id ? 'selected' : ''}">
        ${imageOrFallback(pack.icon, pack.name, 'result-icon')}
        <div class="result-info"><div class="result-title">${esc(pack.name)}</div><div class="result-desc">${esc(pack.summary || 'Modrinth modpack')}</div><div class="result-foot"><span>${number(pack.downloads)} downloads</span><span>Modrinth</span></div></div>
        <button class="btn btn-small ${state.chosenPack?.id === pack.id ? 'btn-primary' : ''}" data-select-pack="${esc(pack.id)}" data-provider="modrinth">${state.chosenPack?.id === pack.id ? 'Selected ✓' : 'Select pack'}</button>
      </article>`).join('') : `<div class="empty-results"><strong>${state.query ? 'No modpacks found' : 'Search Modrinth modpacks'}</strong>${state.query ? 'Try another title or keyword.' : 'Use the search field to find a pack for your next world.'}</div>`;
  const createPanel = `<div class="panel create-panel"><div class="panel-head"><h3>Server details</h3><span class="source-chip">MODRINTH</span></div><div class="panel-body">
      ${state.chosenPack ? `<div class="selected-pack"><strong>${esc(state.chosenPack.name)}</strong><span>${esc(state.chosenPack.summary || 'Selected Modrinth modpack')}</span></div>` : `<div class="selected-pack"><strong>Choose a modpack</strong><span>Select one from the search results to continue.</span></div>`}
      <form id="create-form" class="form-stack">
        <div class="field"><label for="server_name">Server name</label><input class="input" id="server_name" name="name" placeholder="My new world" maxlength="70" required ${state.chosenPack ? '' : 'disabled'}></div>
          <div class="field"><label for="pack_file">Pack release</label><select class="select" id="pack_file" name="file_id" ${state.chosenPack && !state.filesBusy && state.files.length ? '' : 'disabled'}>${state.filesBusy ? '<option>Loading releases…</option>' : options || '<option>Select a pack first</option>'}</select><span class="field-hint">Blockyard targets this Minecraft version and loader, then looks for server-ready files.</span></div>
        <div class="field"><label for="install_root">Install location</label><input class="input" id="install_root" name="root" value="${esc(state.config?.default_root || '')}" placeholder="C:\Minecraft Servers" ${state.chosenPack ? '' : 'disabled'}><span class="field-hint">Blockyard creates a folder named after your server here and remembers this location for the next install.</span></div>
        <button class="btn btn-primary create-install" type="submit" title="${state.chosenPack ? state.filesBusy ? 'Wait for releases to load.' : state.files.length ? 'Download this release and prepare the server files.' : 'This pack has no available releases.' : 'Select a modpack first.'}" ${state.chosenPack && state.files.length && !state.filesBusy && !state.createBusy ? '' : 'disabled'}><span>＋</span> ${state.createBusy ? 'Adding server…' : 'Install &amp; prepare server'}</button>
      </form>
      <p class="field-hint" style="margin:12px 1px 0">Java must be installed and available on PATH. Server downloads can be several gigabytes.</p>
    </div></div>`;
  const importPercent = Math.min(100, Math.round(state.importProgress || 0));
  const importPanel = `<section class="local-import-card">
      <div class="local-import-copy"><span class="import-icon">⇧</span><div><div class="eyebrow">STEP 1 · CHOOSE A PACK</div><h2>Convert a client pack or import a server pack</h2><p>Blockyard reads pack metadata, converts full CurseForge installs and supported profile exports, and keeps removed client-only mods recoverable. If version or loader details are missing, you can enter them below.</p></div></div>
      <form id="local-import-form" class="import-form">
        <div class="import-fields">
          <div class="field import-file-field"><label for="import_pack_file">Modpack archive</label><input class="input file-input" id="import_pack_file" name="pack_file" type="file" accept=".zip,.mrpack,application/zip" required ${state.importBusy ? 'disabled' : ''}><span class="field-hint" id="import-file-status">${state.importFileChosen ? `${esc(state.importFileName)} · ${formatFileSize(state.importFileSize)}` : 'ZIP or .mrpack, up to 2 GB. Choose an exported profile, full CurseForge install, or server pack.'}</span></div>
          <div class="field"><label for="import_server_name">Server name</label><input class="input" id="import_server_name" name="name" maxlength="70" placeholder="My imported server" required ${state.importBusy ? 'disabled' : ''}></div>
          <div class="field"><label for="import_root">Install location</label><input class="input" id="import_root" name="root" value="${esc(state.config?.default_root || '')}" placeholder="C:\Minecraft Servers" ${state.importBusy ? 'disabled' : ''}><span class="field-hint">A new folder will be created here. Blockyard remembers this location for the next install.</span></div>
          <div class="field"><label for="import_minecraft_version">Minecraft version <span class="field-optional">optional</span></label><input class="input" id="import_minecraft_version" name="minecraft_version" maxlength="40" placeholder="Auto detect · e.g. 1.21.1" ${state.importBusy ? 'disabled' : ''}></div>
          <div class="field"><label for="import_loader_type">Server loader <span class="field-optional">optional</span></label><select class="select" id="import_loader_type" name="loader_type" ${state.importBusy ? 'disabled' : ''}><option value="auto">Auto detect</option><option value="vanilla">Vanilla</option><option value="fabric">Fabric</option><option value="quilt">Quilt</option><option value="forge">Forge</option><option value="neoforge">NeoForge</option></select></div>
          <div class="field"><label for="import_loader_version">Loader version <span class="field-optional">optional</span></label><input class="input" id="import_loader_version" name="loader_version" maxlength="80" placeholder="e.g. 21.1.249" ${state.importBusy ? 'disabled' : ''}></div>
          <p class="import-metadata-hint">Use these only when the archive does not include the details. Forge and NeoForge need an exact loader version; Fabric and Quilt can use the latest compatible version if left blank.</p>
          <button class="btn btn-primary import-submit" type="submit" ${state.importBusy || !state.importFileChosen ? 'disabled' : ''}>${state.importBusy ? 'Uploading…' : 'Convert &amp; prepare server'}</button>
        </div>
        ${state.importBusy ? `<div class="import-progress-wrap"><div class="import-progress-label"><span>Uploading modpack</span><span id="import-progress-label">${importPercent}%</span></div><progress class="import-progress" id="import-progress" max="100" value="${importPercent}"></progress></div>` : ''}
      </form>
    </section>`;
  const curseforgeOptions = state.curseforgeFiles.map(file => {
    const tags = (file.game_versions || []).slice(0, 2).join(', ');
    const kind = file.is_server_pack ? ' · Server pack' : file.server_pack_file_id ? ' · Server pack available' : ' · Client profile';
    return `<option value="${esc(file.id)}" ${String(state.curseforgeFileId) === String(file.id) ? 'selected' : ''}>${esc(file.name)}${tags ? ` · ${esc(tags)}` : ''}${kind}</option>`;
  }).join('');
  const curseforgeKeyReady = !!state.config?.curseforge_api_key_set;
  const curseforgeSearchArea = !curseforgeKeyReady
    ? `<div class="empty-results"><strong>CurseForge API key needed</strong>Add your key in Settings to search and install CurseForge modpacks.<br><button class="btn btn-small" type="button" data-view="settings" style="margin-top:12px">Open Settings</button></div>`
    : state.curseforgeSearchBusy
      ? `<div class="empty-results"><span class="loading-dots"><i></i><i></i><i></i></span>Searching CurseForge…</div>`
      : state.curseforgeSearchError
        ? `<div class="empty-results"><strong>Couldn’t load CurseForge packs</strong>${esc(state.curseforgeSearchError)}<br><button class="btn btn-small" type="button" data-action="curseforge-search-retry" style="margin-top:12px">Try again</button></div>`
        : state.curseforgeResults.length
          ? state.curseforgeResults.map(pack => `<article class="result-card ${String(state.curseforgeSelectedPack?.id) === String(pack.id) ? 'selected' : ''}">
              ${imageOrFallback(pack.icon, pack.name, 'result-icon')}
              <div class="result-info"><div class="result-title">${esc(pack.name)}</div><div class="result-desc">${esc(pack.summary || 'CurseForge Minecraft modpack')}</div><div class="result-foot"><span>${number(pack.downloads)} downloads</span><span>CurseForge</span></div></div>
              <button class="btn btn-small ${String(state.curseforgeSelectedPack?.id) === String(pack.id) ? 'btn-primary' : ''}" type="button" data-select-curseforge-pack="${esc(pack.id)}">${String(state.curseforgeSelectedPack?.id) === String(pack.id) ? 'Selected ✓' : 'Select pack'}</button>
            </article>`).join('')
          : `<div class="empty-results"><strong>${state.curseforgeQuery ? 'No modpacks found' : 'Search CurseForge modpacks'}</strong>${state.curseforgeQuery ? 'Try another title or keyword.' : 'Search the official CurseForge Minecraft modpack catalog.'}</div>`;
  const curseforgeCreatePanel = `<div class="panel create-panel"><div class="panel-head"><h3>Server details</h3><span class="source-chip">CURSEFORGE</span></div><div class="panel-body">
      ${state.curseforgeSelectedPack ? `<div class="selected-pack">${imageOrFallback(state.curseforgeSelectedPack.icon, state.curseforgeSelectedPack.name, 'result-icon')}<strong>${esc(state.curseforgeSelectedPack.name)}</strong><span>${esc(state.curseforgeSelectedPack.summary || 'Selected CurseForge modpack')}</span></div>` : `<div class="selected-pack"><strong>Choose a modpack</strong><span>Select a CurseForge result to continue.</span></div>`}
      <form id="curseforge-create-form" class="form-stack">
        <div class="field"><label for="curseforge_server_name">Server name</label><input class="input" id="curseforge_server_name" name="name" placeholder="My new world" maxlength="70" required ${state.curseforgeSelectedPack && curseforgeKeyReady ? '' : 'disabled'}></div>
        <div class="field"><label for="curseforge_file">Pack release</label><select class="select" id="curseforge_file" name="file_id" ${state.curseforgeSelectedPack && !state.curseforgeFilesBusy && state.curseforgeFiles.length && curseforgeKeyReady ? '' : 'disabled'}>${state.curseforgeFilesBusy ? '<option>Loading releases…</option>' : curseforgeOptions || '<option>Select a pack first</option>'}</select><span class="field-hint">Blockyard uses the author’s server pack when available. Otherwise it downloads the profile’s required files and prepares the server layout.</span></div>
        <div class="field"><label for="curseforge_install_root">Install location</label><input class="input" id="curseforge_install_root" name="root" value="${esc(state.config?.default_root || '')}" placeholder="C:\\Minecraft Servers" ${state.curseforgeSelectedPack && curseforgeKeyReady ? '' : 'disabled'}><span class="field-hint">Blockyard creates a folder named after your server here and remembers this location.</span></div>
        <button class="btn btn-primary create-install" type="submit" ${state.curseforgeSelectedPack && state.curseforgeFiles.length && !state.curseforgeFilesBusy && !state.curseforgeInstallBusy && curseforgeKeyReady ? '' : 'disabled'}><span>＋</span> ${state.curseforgeInstallBusy ? 'Adding server…' : 'Install &amp; prepare server'}</button>
      </form>
      <p class="field-hint" style="margin:12px 1px 0">The API key stays in Blockyard’s local settings. CurseForge profile installs download required mod files through the official API.</p>
    </div></div>`;
  const mode = state.installerMode;
  const common = `<div class="discover-hero"><div><div class="eyebrow">BLOCKYARD SERVER INSTALLER</div><h1>Set up a Minecraft server</h1><p>Choose a pack source, review the detected version and server details, then let Blockyard prepare the server folder.</p></div><span class="discover-badge">GUIDED SETUP</span></div>
    <div class="installer-source-switch" role="group" aria-label="Choose modpack source"><button class="source-choice ${mode === 'catalog' ? 'active' : ''}" type="button" data-installer-mode="catalog" aria-pressed="${mode === 'catalog'}" ${state.importBusy || state.createBusy || state.curseforgeInstallBusy ? 'disabled' : ''}><span>⌕</span><strong>Browse Modrinth</strong><small>Find and install a modpack</small></button><button class="source-choice ${mode === 'curseforge' ? 'active' : ''}" type="button" data-installer-mode="curseforge" aria-pressed="${mode === 'curseforge'}" ${state.importBusy || state.createBusy || state.curseforgeInstallBusy ? 'disabled' : ''}><span>✦</span><strong>Browse CurseForge</strong><small>Use your CurseForge API key</small></button><button class="source-choice ${mode === 'import' ? 'active' : ''}" type="button" data-installer-mode="import" aria-pressed="${mode === 'import'}" ${state.importBusy || state.createBusy || state.curseforgeInstallBusy ? 'disabled' : ''}><span>⇧</span><strong>Import a pack</strong><small>Convert an exported profile or server pack</small></button></div>
    ${renderInstallerSteps(mode)}`;
  if (mode === 'import') return `${common}<div class="installer-import-content">${importPanel}</div>`;
  if (mode === 'curseforge') return `${common}
    <div class="discover-tools"><div class="search-wrap"><span class="search-symbol" aria-hidden="true">⌕</span><input id="curseforge-search" class="input search-input" aria-label="Search CurseForge modpacks" placeholder="Search CurseForge modpacks…" value="${esc(state.curseforgeQuery)}" ${curseforgeKeyReady ? '' : 'disabled'}></div><button class="btn btn-primary" type="button" data-action="curseforge-search" ${curseforgeKeyReady ? '' : 'disabled'} title="Search CurseForge’s Minecraft modpack catalog">Search</button><button class="btn btn-quiet" type="button" data-action="curseforge-refresh-search" ${curseforgeKeyReady ? '' : 'disabled'} title="Show popular CurseForge modpacks">Popular</button></div>
    <div class="result-meta">${state.curseforgeSearchBusy ? 'CONNECTING TO CURSEFORGE' : state.curseforgeResults.length ? `${number(state.curseforgeResults.length)} PACKS SHOWN${state.curseforgeQuery ? ` · “${esc(state.curseforgeQuery)}”` : ' · POPULAR'}` : 'CURSEFORGE MODPACKS'}</div>
    <div class="discover-layout"><div class="result-list">${curseforgeSearchArea}</div>${curseforgeCreatePanel}</div>`;
  return `${common}
    <div class="discover-tools"><div class="search-wrap"><span class="search-symbol" aria-hidden="true">⌕</span><input id="pack-search" class="input search-input" aria-label="Search Modrinth modpacks" placeholder="Search Modrinth modpacks…" value="${esc(state.query)}"></div><button class="btn btn-primary" type="button" data-action="search" title="Search Modrinth for matching modpacks">Search</button><button class="btn btn-quiet" type="button" data-action="refresh-search" title="Show popular modpacks">Popular</button></div>
    <div class="result-meta">${state.searchBusy ? 'CONNECTING TO MODRINTH' : state.results.length ? `${number(state.results.length)} PACKS SHOWN${state.query ? ` · “${esc(state.query)}”` : ' · POPULAR'}` : 'MODRINTH MODPACKS'}</div>
    <div class="discover-layout"><div class="result-list">${searchArea}</div>${createPanel}</div>`;
}

function renderSettings() {
  return `<div class="page-head"><div class="page-head-left"><div class="eyebrow">PREFERENCES</div><h1>Settings</h1><p class="subheading">Set your workspace defaults, app theme, and pack source credentials.</p></div></div>
    <div class="settings-layout"><section class="panel settings-section">
      <div class="settings-section-head"><h2>Server files</h2><p>Set the default folder Blockyard uses for new modpack installs and imported server packs.</p></div>
      <form id="config-form" class="form-stack">
        <div class="field"><label for="default_root_settings">Default server install location</label><input class="input" id="default_root_settings" name="default_root" value="${esc(state.config?.default_root || '')}"><span class="field-hint">New server folders are created inside this directory. The most recent location you use in the installer becomes the default.</span></div>
        <div class="settings-divider"></div>
        <div class="settings-section-head compact"><h2>Theme color</h2><p>Choose the accent color used for buttons, selected navigation, and highlights.</p></div>
        <div class="theme-color-row"><label class="theme-color-control" for="theme_color"><input id="theme_color" name="theme_color" type="color" value="${esc(state.config?.theme_color || '#54a9ff')}" aria-label="Choose Blockyard theme color"><span>Accent color</span></label><span class="theme-color-preview">Your selection previews immediately across Blockyard.</span><button class="btn btn-quiet btn-small" type="button" data-action="reset-theme">Reset color</button></div>
        <div class="settings-divider"></div>
        <div class="settings-section-head compact"><h2>CurseForge access</h2><p>Save the CurseForge API key once for all CurseForge searches and installs in this Blockyard app.</p></div>
        <div class="field"><label for="curseforge_api_key">CurseForge API key ${state.config?.curseforge_api_key_set ? '<span class="key-status">Saved locally</span>' : '<span class="field-optional">not set</span>'}</label><input class="input" id="curseforge_api_key" name="curseforge_api_key" type="password" autocomplete="new-password" placeholder="Paste an active CurseForge API key"><span class="field-hint">The key is stored in Blockyard’s local settings file, never returned to the browser, and sent to CurseForge only for authenticated API requests. <a href="https://docs.curseforge.com/rest-api/" target="_blank" rel="noreferrer">CurseForge API key documentation</a>.</span></div>
        ${state.config?.curseforge_api_key_set ? '<label class="checkbox-line"><input type="checkbox" name="clear_curseforge_api_key" value="true"><span>Remove the saved CurseForge API key</span></label>' : ''}
        <div class="save-row"><button class="btn btn-primary" type="submit">Save settings</button></div>
      </form>
    </section>
    <section class="panel settings-section" style="margin-top:14px">
      <div class="settings-section-head"><h2>Client-only mod manifest</h2><p>Edit the learned fingerprints Blockyard uses to recognize client-only mods in future packs. Save checks the JSON and keeps the current file if validation fails.</p></div>
      <form id="client-manifest-form" class="form-stack">
        <div class="field"><label for="client-manifest-path">Manifest file path</label><input class="input manifest-path" id="client-manifest-path" value="${esc(state.clientManifestPath || 'Loading manifest path…')}" readonly aria-label="Manifest file path"><span class="field-hint">This is Blockyard’s local data file. Edits saved here are used by future modpack scans.</span></div>
        ${state.clientManifestLoadError ? `<div class="warning-card">Could not load the manifest: ${esc(state.clientManifestLoadError)}</div>` : ''}
        <div class="field"><label for="client-manifest-json">Manifest contents · JSON</label><textarea class="input manifest-editor" id="client-manifest-json" spellcheck="false" ${!state.clientManifestLoaded || state.clientManifestLoadError || state.clientManifestSaving ? 'disabled' : ''} placeholder="${state.clientManifestLoading ? 'Loading manifest…' : 'Manifest JSON will appear here.'}">${esc(state.clientManifestText)}</textarea><span class="field-hint">Expected format: version 1 with a mods array. Each entry needs a mod ID, normalized filename, or SHA-256 fingerprint. Filenames are normalized on save. JSON errors include the line and column.</span></div>
        <div class="save-row manifest-actions"><button class="btn btn-quiet" type="button" data-action="reload-client-manifest" ${state.clientManifestLoading || state.clientManifestSaving ? 'disabled' : ''}>${state.clientManifestLoading ? 'Loading…' : state.clientManifestLoadError ? 'Try loading again' : 'Reload from file'}</button><button class="btn btn-primary" type="submit" ${!state.clientManifestLoaded || state.clientManifestLoadError || state.clientManifestSaving || !state.clientManifestDirty ? 'disabled' : ''}>${state.clientManifestSaving ? 'Saving…' : 'Save manifest'}</button></div>
      </form>
    </section>
    <section class="panel settings-section" style="margin-top:14px"><div class="settings-section-head"><h2>About this app</h2><p>Blockyard runs as a local web app on this computer. Its manager page listens only on 127.0.0.1.</p></div><div class="info-list"><div class="info-row"><span>Server data</span><span class="mono">${esc(state.config?.data_folder || '')}</span></div><div class="info-row"><span>Pack sources</span><span>Modrinth, CurseForge, and local server packs</span></div><div class="info-row"><span>Client-only rules</span><span><a href="https://github.com/LianJordaan/client-side-mods-list" target="_blank" rel="noreferrer">Curated list, cached locally</a></span></div><div class="info-row"><span>Runtime</span><span>Python standard library + Java</span></div></div></section></div>`;
}

async function loadClientOnlyManifest(force = false) {
  if (state.clientManifestLoading || (!force && state.clientManifestLoaded)) return;
  state.clientManifestLoading = true;
  state.clientManifestLoadError = '';
  if (state.view === 'settings') render();
  try {
    const result = await api('/api/client-only-manifest');
    state.clientManifestPath = result.path || '';
    state.clientManifestText = result.content || '{\n  "version": 1,\n  "mods": []\n}';
    state.clientManifestSavedText = state.clientManifestText;
    state.clientManifestLoaded = true;
    state.clientManifestDirty = false;
  } catch (err) {
    state.clientManifestLoadError = err.message;
    state.clientManifestLoaded = true;
  } finally {
    state.clientManifestLoading = false;
    if (state.view === 'settings') render();
  }
}

function render() {
  saveNavigationState();
  updateSidebar();
  updateBreadcrumb();
  const content = $('#main-content');
  if (state.view === 'discover') content.innerHTML = renderDiscover();
  else if (state.view === 'settings') content.innerHTML = renderSettings();
  else {
    const server = currentServer();
    content.innerHTML = server ? serverContent(server) : emptyServers();
  }
  const mini = $('#mini-console');
  if (mini) mini.scrollTop = mini.scrollHeight;
  const full = $('#full-console');
  if (full) full.scrollTop = full.scrollHeight;
  if (state.view === 'settings' && !state.clientManifestLoaded && !state.clientManifestLoading) loadClientOnlyManifest();
}

function renderKeepingSearchFocus() {
  if (state.view === 'discover' && state.installerMode === 'import') return;
  const focusId = ['pack-search', 'curseforge-search'].includes(document.activeElement?.id) ? document.activeElement.id : '';
  const input = focusId ? document.activeElement : null;
  const value = input?.value;
  const start = input?.selectionStart;
  const end = input?.selectionEnd;
  render();
  const replacement = focusId ? document.getElementById(focusId) : null;
  if (input && replacement) {
    replacement.value = value;
    replacement.focus({preventScroll:true});
    if (typeof start === 'number') replacement.setSelectionRange(start, end);
  }
}

async function refreshState() {
  const [configResult, serverResult] = await Promise.all([api('/api/config'), api('/api/servers')]);
  state.config = configResult;
  applyThemeColor(configResult.theme_color);
  state.servers = serverResult.servers || [];
  if (state.currentId && !state.servers.some(x => x.id === state.currentId)) state.currentId = null;
  if (!state.currentId && state.servers.length) state.currentId = state.servers[0].id;
  render();
}

async function refreshServersQuietly() {
  const result = await api('/api/servers');
  const nextServers = result.servers || [];
  const previousStatus = JSON.stringify(state.servers.map(server => [server.id, server.status, server.pid, server.setup_status, server.setup_error, server.log_seq, server.mod_compatibility?.checked_at, server.mod_compatibility?.counts?.client_only]));
  const nextStatus = JSON.stringify(nextServers.map(server => [server.id, server.status, server.pid, server.setup_status, server.setup_error, server.log_seq, server.mod_compatibility?.checked_at, server.mod_compatibility?.counts?.client_only]));
  const changed = previousStatus !== nextStatus;
  state.servers = nextServers;
  if (state.currentId && !state.servers.some(x => x.id === state.currentId)) state.currentId = null;
  if (!state.currentId && state.servers.length) state.currentId = state.servers[0].id;
  if (state.view === 'servers' && changed) {
    const focused = document.activeElement;
    const focusId = focused?.id;
    const focusName = focused?.name;
    const controls = $$('input,select,textarea').filter(control => control.type !== 'file').map(control => ({
      id: control.id, name: control.name, type: control.type, value: control.value, checked: control.checked,
      selectionStart: typeof control.selectionStart === 'number' ? control.selectionStart : null,
      selectionEnd: typeof control.selectionEnd === 'number' ? control.selectionEnd : null
    }));
    render();
    for (const saved of controls) {
      const replacement = saved.id ? document.getElementById(saved.id) : $$('input,select,textarea').find(control => control.name === saved.name && control.type === saved.type);
      if (!replacement) continue;
      if (saved.type === 'checkbox' || saved.type === 'radio') replacement.checked = saved.checked;
      else replacement.value = saved.value;
      if (saved.id === focusId || (!focusId && saved.name && saved.name === focusName)) {
        replacement.focus({preventScroll:true});
        if (saved.selectionStart !== null && typeof replacement.setSelectionRange === 'function') replacement.setSelectionRange(saved.selectionStart, saved.selectionEnd);
      }
    }
  } else {
    updateSidebar();
  }
}

async function doSearch(query = state.query) {
  state.query = query.trim();
  const requestId = ++state.searchRequestId;
  state.searchBusy = true;
  state.searchError = '';
  renderKeepingSearchFocus();
  try {
    const data = await api(`/api/modrinth/search?q=${encodeURIComponent(state.query)}`);
    if (requestId !== state.searchRequestId) return;
    state.results = data.results || [];
    if (state.chosenPack && !state.results.some(pack => pack.id === state.chosenPack.id)) state.results.unshift(state.chosenPack);
  } catch (err) {
    if (requestId !== state.searchRequestId) return;
    state.results = [];
    state.searchError = err.message;
  } finally {
    if (requestId === state.searchRequestId) {
      state.searchBusy = false;
      renderKeepingSearchFocus();
    }
  }
}

async function selectPack(id) {
  const pack = state.results.find(item => String(item.id) === String(id));
  if (!pack) return;
  state.chosenPack = pack;
  const requestId = ++state.versionsRequestId;
  state.files = [];
  state.selectedFile = null;
  state.selectedFileId = null;
  state.filesBusy = true;
  render();
  try {
    const data = await api(`/api/modrinth/projects/${encodeURIComponent(pack.id)}/versions`);
    if (requestId !== state.versionsRequestId) return;
    state.files = data.files || [];
    const preferred = state.files[0];
    if (preferred) { state.selectedFile = preferred; state.selectedFileId = preferred.id; }
  } catch (err) {
    if (requestId === state.versionsRequestId) toast(err.message, 'error');
  } finally {
    if (requestId === state.versionsRequestId) {
      state.filesBusy = false;
      if (state.view === 'discover' && state.installerMode === 'catalog') render();
    }
  }
}

async function doCurseForgeSearch(query = state.curseforgeQuery) {
  state.curseforgeQuery = query.trim();
  const requestId = ++state.curseforgeSearchRequestId;
  state.curseforgeSearchBusy = true;
  state.curseforgeSearchError = '';
  renderKeepingSearchFocus();
  try {
    const data = await api(`/api/curseforge/search?q=${encodeURIComponent(state.curseforgeQuery)}`);
    if (requestId !== state.curseforgeSearchRequestId) return;
    state.curseforgeResults = data.results || [];
    if (state.curseforgeSelectedPack && !state.curseforgeResults.some(pack => String(pack.id) === String(state.curseforgeSelectedPack.id))) {
      state.curseforgeResults.unshift(state.curseforgeSelectedPack);
    }
  } catch (err) {
    if (requestId !== state.curseforgeSearchRequestId) return;
    state.curseforgeResults = [];
    state.curseforgeSearchError = err.message;
  } finally {
    if (requestId === state.curseforgeSearchRequestId) {
      state.curseforgeSearchBusy = false;
      renderKeepingSearchFocus();
    }
  }
}

async function selectCurseForgePack(id) {
  const pack = state.curseforgeResults.find(item => String(item.id) === String(id));
  if (!pack) return;
  state.curseforgeSelectedPack = pack;
  const requestId = ++state.curseforgeFilesRequestId;
  state.curseforgeFiles = [];
  state.curseforgeSelectedFile = null;
  state.curseforgeFileId = null;
  state.curseforgeFilesBusy = true;
  render();
  try {
    const data = await api(`/api/curseforge/mods/${encodeURIComponent(pack.id)}/files`);
    if (requestId !== state.curseforgeFilesRequestId) return;
    state.curseforgeFiles = data.files || [];
    const preferred = state.curseforgeFiles.find(file => file.is_server_pack) || state.curseforgeFiles[0];
    if (preferred) {
      state.curseforgeSelectedFile = preferred;
      state.curseforgeFileId = preferred.id;
    }
  } catch (err) {
    if (requestId === state.curseforgeFilesRequestId) toast(err.message, 'error');
  } finally {
    if (requestId === state.curseforgeFilesRequestId) {
      state.curseforgeFilesBusy = false;
      if (state.view === 'discover' && state.installerMode === 'curseforge') render();
    }
  }
}

function confirmModal(title, copy, confirmText, danger = false) {
  return new Promise(resolve => {
    const root = $('#modal-root');
    root.innerHTML = `<div class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true"><h2>${esc(title)}</h2><p>${esc(copy)}</p><div class="modal-actions"><button class="btn btn-quiet" data-modal="cancel">Cancel</button><button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-modal="confirm">${esc(confirmText)}</button></div></section></div>`;
    root.onclick = event => {
      if (event.target.classList.contains('modal-backdrop') || event.target.closest('[data-modal="cancel"]')) { root.innerHTML = ''; root.onclick = null; resolve(false); }
      if (event.target.closest('[data-modal="confirm"]')) { root.innerHTML = ''; root.onclick = null; resolve(true); }
    };
  });
}

async function detectServerFolders() {
  const root = $('#modal-root');
  let open = true;
  let scanResult = null;
  let busyPath = '';
  let message = '';
  let error = '';
  const close = () => { open = false; root.innerHTML = ''; root.onclick = null; };
  const renderModal = () => {
    const candidates = scanResult?.servers || [];
    const content = !scanResult
      ? `<div class="detect-empty"><span class="loading-dots"><i></i><i></i><i></i></span>Scanning the configured server folder…</div>`
      : !scanResult.directory_exists
        ? `<div class="detect-empty"><strong>Server folder not found</strong><span>Set the default server install location in Settings, then scan again.</span><button class="btn btn-small btn-quiet" type="button" data-detect-settings>Open Settings</button></div>`
        : candidates.length
          ? `<div class="detect-server-list">${candidates.map(server => `<article class="detect-server-card"><div class="detect-server-info"><strong>${esc(server.name)}</strong><span class="detect-server-path mono">${esc(server.path)}</span><span class="detect-server-meta">Minecraft ${esc(server.minecraft_version)} · ${esc(server.loader)} · ${number(server.mod_count)} mod JAR${server.mod_count === 1 ? '' : 's'} · ${server.eula_accepted ? 'EULA accepted' : 'EULA not accepted'}</span><span class="detect-server-launch">Launch file: ${esc(server.launch_file)}</span></div><button class="btn btn-primary btn-small" type="button" data-detected-add="${esc(server.path)}" ${busyPath ? 'disabled' : ''}>${busyPath === server.path ? 'Adding…' : 'Add to Blockyard'}</button></article>`).join('')}</div>`
          : `<div class="detect-empty"><strong>No unregistered server folders found</strong><span>Blockyard looks for a server launch file directly in this location or in one of its child folders.</span></div>`;
    root.innerHTML = `<div class="modal-backdrop detect-modal-backdrop"><section class="modal detect-modal" role="dialog" aria-modal="true" aria-labelledby="detect-server-title"><div class="detect-modal-head"><div><div class="eyebrow">LOCAL SERVER FOLDERS</div><h2 id="detect-server-title">Detect existing servers</h2></div><button class="icon-button" type="button" data-detect-close aria-label="Close">×</button></div><p>Choose a server folder to add it to Blockyard. Detection only reads its files; it does not move or change them.</p>${scanResult?.scan_root ? `<div class="detect-root"><span>Scanning</span><span class="mono">${esc(scanResult.scan_root)}</span></div>` : ''}${message ? `<div class="detect-message">${esc(message)}</div>` : ''}${error ? `<div class="detect-error">${esc(error)}</div>` : ''}${content}<div class="modal-actions"><button class="btn btn-quiet" type="button" data-detect-close>Close</button><button class="btn" type="button" data-detect-rescan ${busyPath ? 'disabled' : ''}>Scan again</button></div></section></div>`;
  };
  const scan = async () => {
    scanResult = null;
    error = '';
    renderModal();
    try {
      scanResult = await api('/api/servers/detect', {method:'POST', body:{operation:'scan'}});
    } catch (err) {
      error = err.message;
    }
    if (open) renderModal();
  };
  root.onclick = async event => {
    if (event.target.classList.contains('modal-backdrop') || event.target.closest('[data-detect-close]')) { close(); return; }
    if (event.target.closest('[data-detect-settings]')) { close(); state.view = 'settings'; render(); return; }
    if (event.target.closest('[data-detect-rescan]')) { await scan(); return; }
    const addButton = event.target.closest('[data-detected-add]');
    if (!addButton || busyPath) return;
    busyPath = addButton.dataset.detectedAdd;
    message = '';
    error = '';
    renderModal();
    try {
      const result = await api('/api/servers/detect', {method:'POST', body:{operation:'add', path:busyPath}});
      await refreshState();
      if (result.server) {
        state.currentId = result.server.id;
        state.view = 'servers';
        state.tab = 'overview';
        render();
      }
      message = `${result.server?.name || 'Server'} added to Blockyard. Its folder is unchanged.`;
      busyPath = '';
      await scan();
    } catch (err) {
      busyPath = '';
      error = err.message;
      if (open) renderModal();
    }
  };
  renderModal();
  await scan();
}

function deleteServerModal(server) {
  return new Promise(resolve => {
    const root = $('#modal-root');
    root.innerHTML = `<div class="modal-backdrop"><section class="modal delete-server-modal" role="dialog" aria-modal="true" aria-labelledby="delete-server-title"><h2 id="delete-server-title">Delete ${esc(server.name)}?</h2><p>Choose whether to keep its server folder. Deleting the folder also removes its worlds, mods, and backups.</p><div class="modal-actions delete-server-options"><button class="btn btn-quiet" data-modal="cancel">Cancel</button><button class="btn" data-modal="keep">Remove from Blockyard<br><small>Keep server files</small></button><button class="btn btn-danger" data-modal="files">Delete server and files</button></div></section></div>`;
    const finish = value => { root.innerHTML = ''; root.onclick = null; root.oninput = null; resolve(value); };
    root.onclick = event => {
      if (event.target.classList.contains('modal-backdrop') || event.target.closest('[data-modal="cancel"]')) finish(null);
      else if (event.target.closest('[data-modal="keep"]')) finish('keep');
      else if (event.target.closest('[data-modal="files"]')) finish('files');
    };
  });
}

async function serverAction(action) {
  const server = currentServer();
  if (!server) return;
  if (action === 'kill') {
    const ok = await confirmModal('Kill server process?', 'This force-terminates the Java process and may lose unsaved world progress. Use Stop for a graceful shutdown.', 'Kill process', true);
    if (!ok) return;
  }
  try {
    await api(`/api/servers/${server.id}/${action}`, {method:'POST', body:{}});
    toast(action === 'restart' ? 'Restarting server…' : action === 'start' ? 'Starting server…' : action === 'kill' ? 'Server process terminated.' : 'Stopping server…');
    await refreshState();
  } catch (err) {
    // The backend may know a server is running even when this tab's last
    // snapshot said Offline (for example, when another tab started it).
    await refreshServersQuietly().catch(() => {});
    toast(err.message, 'error');
    if (err.message.includes('EULA') || err.message.includes('Java executable')) { state.tab = 'properties'; render(); }
  }
}

function collectForm(form) {
  const data = new FormData(form);
  const result = Object.fromEntries(data.entries());
  $$('input[type="checkbox"]', form).forEach(input => result[input.name] = input.checked);
  for (const field of ['port','max_players','view_distance','simulation_distance','ram_mb']) {
    if (field in result) result[field] = Number(result[field]);
  }
  return result;
}

function resetVisibleConsole() {
  const server = currentServer();
  if (!server) return;
  state.consoleClearThrough[server.id] = Number(server.log_seq || 0);
  render();
}

async function confirmClientOnlyMods(paths) {
  const server = currentServer();
  if (!server || !paths.length) return;
  try {
    const result = await api(`/api/servers/${encodeURIComponent(server.id)}/mods/confirm-removal`, {method:'POST', body:{paths}});
    await refreshState();
    const removed = result.removed || [];
    const failed = result.failed || [];
    if (removed.length) toast(`Moved ${removed.length} client-only mod${removed.length === 1 ? '' : 's'} to the recovery folder.`);
    if (failed.length) toast(`${failed.length} mod${failed.length === 1 ? '' : 's'} could not be moved. Review the compatibility list.`, 'error');
    if (!removed.length && !failed.length) toast('No mods were moved. Refresh the compatibility list and try again.', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function restoreClientOnlyMod(path) {
  const server = currentServer();
  if (!server || !path) return;
  try {
    const result = await api(`/api/servers/${encodeURIComponent(server.id)}/mods/restore`, {method:'POST', body:{paths:[path]}});
    await refreshState();
    const restored = result.restored || [];
    const failed = result.failed || [];
    if (restored.length) toast(`Restored ${restored.length} mod${restored.length === 1 ? '' : 's'} to the server mods folder.`);
    if (failed.length) toast(`Could not restore ${failed.length} mod${failed.length === 1 ? '' : 's'}. Review the compatibility list.`, 'error');
    if (!restored.length && !failed.length) toast('No mods were restored. Refresh the compatibility list and try again.', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

document.addEventListener('click', async event => {
  const view = event.target.closest('[data-view]');
  if (view) { if (view.tagName === 'A') event.preventDefault(); setView(view.dataset.view); return; }
  const installerMode = event.target.closest('[data-installer-mode]');
  if (installerMode) {
    const nextMode = installerMode.dataset.installerMode;
    if (nextMode !== state.installerMode) {
      if (state.installerMode === 'import') {
        state.importFileChosen = false;
        state.importFileName = '';
        state.importFileSize = 0;
      }
      state.installerMode = nextMode;
      render();
    }
    return;
  }
  const settingsAction = event.target.closest('[data-action]')?.dataset.action;
  if (settingsAction === 'reset-theme') {
    const picker = $('#theme_color');
    if (picker) picker.value = '#54a9ff';
    applyThemeColor('#54a9ff');
    return;
  }
  if (settingsAction === 'reload-client-manifest') {
    if (state.clientManifestDirty) {
      const discard = await confirmModal('Discard manifest edits?', 'Reloading replaces your unsaved editor text with the saved manifest file.', 'Discard edits', true);
      if (!discard) return;
    }
    state.clientManifestLoaded = false;
    await loadClientOnlyManifest(true);
    return;
  }
  const serverButton = event.target.closest('[data-server]');
  if (serverButton) { state.currentId = serverButton.dataset.server; state.view = 'servers'; state.tab = 'overview'; state.lastSeq = 0; state.fileServerId = null; state.filePath = ''; state.serverFiles = []; state.fileOpenPath = ''; render(); return; }
  const tab = event.target.closest('[data-tab]');
  if (tab) {
    state.tab = tab.dataset.tab; state.view = 'servers'; render();
    if (state.tab === 'files' && state.fileServerId !== state.currentId) { state.filePath = ''; state.serverFiles = []; state.fileOpenPath = ''; await loadServerFiles(''); }
    return;
  }
  const removeClientMod = event.target.closest('[data-remove-client-mod]');
  if (removeClientMod) {
    await confirmClientOnlyMods([removeClientMod.dataset.removeClientMod]);
    return;
  }
  const restoreClientMod = event.target.closest('[data-restore-client-mod]');
  if (restoreClientMod) {
    await restoreClientOnlyMod(restoreClientMod.dataset.restoreClientMod);
    return;
  }
  const directory = event.target.closest('[data-file-dir]');
  if (directory) { state.fileOpenPath = ''; state.fileEditorContent = ''; await loadServerFiles(directory.dataset.fileDir || ''); return; }
  const openFile = event.target.closest('[data-file-open]');
  if (openFile) { await openServerFile(openFile.dataset.fileOpen); return; }
  const downloadFile = event.target.closest('[data-file-download]');
  if (downloadFile) {
    const server = currentServer();
    if (!server) return;
    const params = new URLSearchParams({path:downloadFile.dataset.fileDownload});
    const anchor = document.createElement('a');
    anchor.href = `/api/servers/${encodeURIComponent(server.id)}/files/download?${params}`;
    anchor.download = '';
    document.body.append(anchor); anchor.click(); anchor.remove();
    return;
  }
  const deleteFile = event.target.closest('[data-file-delete]');
  if (deleteFile) {
    const path = deleteFile.dataset.fileDelete;
    const ok = await confirmModal('Delete this file?', `${path} will be permanently deleted from the server folder.`, 'Delete file', true);
    if (!ok) return;
    try {
      await api(`/api/servers/${encodeURIComponent(state.currentId)}/files`, {method:'POST', body:{operation:'delete', path}});
      if (state.fileOpenPath === path) { state.fileOpenPath = ''; state.fileEditorContent = ''; }
      toast('File deleted.');
      await loadServerFiles(state.filePath);
    } catch (err) { toast(err.message, 'error'); }
    return;
  }
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action === 'detect-server-folders') { await detectServerFolders(); return; }
  if (action === 'quit-app') {
    const ok = await confirmModal(
      'Close Blockyard?',
      'This closes the local manager and gracefully stops every Minecraft server it is currently managing. Server folders and files will be kept.',
      'Close Blockyard',
      true
    );
    if (!ok) return;
    const button = event.target.closest('[data-action="quit-app"]');
    if (button) { button.disabled = true; button.textContent = 'Closing…'; }
    try {
      await api('/api/shutdown', {method:'POST', body:{}});
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      $('#main-content').innerHTML = `<section class="panel app-closed"><span class="app-closed-mark">⏻</span><h1>Blockyard is closed</h1><p>The manager has shut down and its servers are stopping gracefully. You can close this browser tab.</p></section>`;
      $('#server-sections').innerHTML = '';
    } catch (err) {
      if (button) { button.disabled = false; button.textContent = '⏻ Close Blockyard'; }
      toast(err.message, 'error');
    }
    return;
  }
  if (action === 'confirm-all-client-mods') {
    const paths = pendingClientModRemovals(currentServer()).map(mod => mod.path);
    await confirmClientOnlyMods(paths);
    return;
  }
  if (action === 'start' || action === 'stop' || action === 'kill' || action === 'restart') { await serverAction(action); return; }
  if (action === 'delete-server') {
    const server = currentServer();
    if (!server) return;
    const choice = await deleteServerModal(server);
    if (!choice) return;
    try {
      await api(`/api/servers/${encodeURIComponent(server.id)}/delete`, {method:'POST', body:{delete_files:choice === 'files'}});
      state.currentId = null;
      state.view = 'servers';
      state.tab = 'overview';
      state.serverFiles = [];
      state.fileOpenPath = '';
      await refreshState();
      toast(choice === 'files' ? 'Server and files deleted.' : 'Server removed. Its folder was kept.');
    } catch (err) { toast(err.message, 'error'); }
    return;
  }
  if (action === 'use-recommended-java') {
    const server = currentServer();
    const requirement = javaRequirementForMinecraft(server?.minecraft_version);
    const selected = recommendedJavaRuntime(state.config?.java_runtimes || [], requirement);
    if (!selected) {
      toast('No compatible Java runtime was detected. Install the recommended version, then click Find Java.', 'error');
      return;
    }
    const selector = $('#java-runtime-select');
    if (selector) selector.value = selected.path;
    const input = $('#java_path');
    if (input) input.value = selected.path;
    const status = $('#java-detection-status');
    if (status) status.textContent = `Java ${selected.version} in ${selected.source}: ${selected.path}`;
    updateJavaRecommendationCard(server, selected, selected.path);
    toast(`Java ${javaVersionMajor(selected.version)} selected. Save changes to keep it.`);
    return;
  }
  if (action === 'find-java') {
    const button = event.target.closest('[data-action="find-java"]');
    const status = $('#java-detection-status');
    button.disabled = true;
    button.textContent = 'Searching…';
    try {
      const result = await api('/api/java/list?refresh=1');
      const runtimes = result.runtimes || [];
      const requirement = javaRequirementForMinecraft(currentServer()?.minecraft_version);
      const found = recommendedJavaRuntime(runtimes, requirement) || runtimes[0] || {path:'', version:'', source:''};
      if (!state.config) state.config = {};
      state.config.java_runtime = found;
      state.config.java_runtimes = runtimes;
      const selector = $('#java-runtime-select');
      if (selector) {
        const prompt = runtimes.length ? 'Custom path or PATH lookup' : 'No installed Java detected';
        selector.innerHTML = `<option value="">${prompt}</option>${runtimes.map(item => `<option value="${esc(item.path)}">Java ${esc(item.version)} · ${esc(item.source)} · ${esc(item.path)}</option>`).join('')}`;
        selector.value = found.path || '';
      }
      if (found.path) {
        $('#java_path').value = found.path;
        if (status) status.textContent = `Found ${runtimes.length} Java runtime${runtimes.length === 1 ? '' : 's'}. Java ${found.version} from ${found.source} is selected.`;
        updateJavaRecommendationCard(currentServer(), found, found.path);
        toast(`Found ${runtimes.length} Java runtime${runtimes.length === 1 ? '' : 's'}. Save changes to keep the selection.`);
      } else {
        if (status) status.textContent = 'No Java installation was found in the usual host locations. Install Java or enter its executable path.';
        updateJavaRecommendationCard(currentServer(), null, '');
        toast('No Java installation was found on this computer.', 'error');
      }
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Find Java';
    }
    return;
  }
  if (action === 'search') { await doSearch($('#pack-search')?.value || ''); return; }
  if (action === 'search-retry') { await doSearch(); return; }
  if (action === 'refresh-search') { state.query = ''; $('#pack-search') && ($('#pack-search').value = ''); await doSearch(''); return; }
  if (action === 'curseforge-search') { await doCurseForgeSearch($('#curseforge-search')?.value || ''); return; }
  if (action === 'curseforge-search-retry') { await doCurseForgeSearch(); return; }
  if (action === 'curseforge-refresh-search') { state.curseforgeQuery = ''; $('#curseforge-search') && ($('#curseforge-search').value = ''); await doCurseForgeSearch(''); return; }
  if (action === 'clear-console') { resetVisibleConsole(); return; }
  if (action === 'copy-console') {
    const text = visibleConsoleText();
    if (!text) { toast('There is no console output to copy.', 'error'); return; }
    try {
      await copyText(text);
      toast('Console output copied.');
    } catch { toast('Copy is unavailable here.', 'error'); }
    return;
  }
  if (action === 'refresh-server-files') { await loadServerFiles(state.filePath); return; }
  if (action === 'new-server-folder') { state.fileNewFolderOpen = true; render(); $('#new-folder-name')?.focus(); return; }
  if (action === 'cancel-new-folder') { state.fileNewFolderOpen = false; render(); return; }
  if (action === 'upload-server-file') { $('#server-file-upload')?.click(); return; }
  if (action === 'close-file-editor') { state.fileOpenPath = ''; state.fileEditorContent = ''; render(); return; }
  const pack = event.target.closest('[data-select-pack]');
  if (pack) { await selectPack(pack.dataset.selectPack); return; }
  const curseforgePack = event.target.closest('[data-select-curseforge-pack]');
  if (curseforgePack) { await selectCurseForgePack(curseforgePack.dataset.selectCurseforgePack); return; }
  const copy = event.target.closest('[data-copy]');
  if (copy) {
    try { await copyText(copy.dataset.copy); toast('Address copied.'); }
    catch { toast('Copy is unavailable here. Select the address to copy it manually.', 'error'); }
  }
});

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; }
    catch { /* use the local document fallback below */ }
  }
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.position = 'fixed';
  field.style.opacity = '0';
  document.body.append(field);
  field.select();
  const copied = document.execCommand('copy');
  field.remove();
  if (!copied) throw new Error('Clipboard copy failed.');
}

document.addEventListener('input', event => {
  if (event.target.id === 'pack-search') {
    clearTimeout(state.searchTimer);
    state.query = event.target.value;
    state.searchTimer = setTimeout(() => doSearch(state.query), 350);
  }
  if (event.target.id === 'curseforge-search') {
    clearTimeout(state.curseforgeSearchTimer);
    state.curseforgeQuery = event.target.value;
    state.curseforgeSearchTimer = setTimeout(() => doCurseForgeSearch(state.curseforgeQuery), 500);
  }
  if (event.target.id === 'theme_color') applyThemeColor(event.target.value);
  if (event.target.id === 'file-editor-content') state.fileEditorContent = event.target.value;
  if (event.target.id === 'client-manifest-json') {
    state.clientManifestText = event.target.value;
    state.clientManifestDirty = state.clientManifestText !== state.clientManifestSavedText;
    const save = $('#client-manifest-form button[type="submit"]');
    if (save) save.disabled = !state.clientManifestDirty || state.clientManifestSaving || !!state.clientManifestLoadError;
  }
});

document.addEventListener('change', event => {
  if (event.target.id === 'java-runtime-select') {
    const selected = (state.config?.java_runtimes || []).find(item => item.path === event.target.value);
    if (selected) {
      $('#java_path').value = selected.path;
      const status = $('#java-detection-status');
      if (status) status.textContent = `Java ${selected.version} in ${selected.source}: ${selected.path}`;
    }
    updateJavaRecommendationCard(currentServer(), selected || null, $('#java_path')?.value || '');
    return;
  }
  if (event.target.id === 'pack_file') {
    state.selectedFileId = event.target.value;
    state.selectedFile = state.files.find(file => String(file.id) === String(state.selectedFileId)) || null;
    syncInstallerSteps();
    return;
  }
  if (event.target.id === 'curseforge_file') {
    state.curseforgeFileId = event.target.value;
    state.curseforgeSelectedFile = state.curseforgeFiles.find(file => String(file.id) === String(state.curseforgeFileId)) || null;
    syncInstallerSteps();
    return;
  }
  if (event.target.id === 'import_pack_file') {
    const name = $('#import_server_name');
    const file = event.target.files?.[0];
    const supportedType = !!file && /\.(zip|mrpack)$/i.test(file.name);
    const supportedSize = !!file && file.size > 0 && file.size <= 2 * 1024 * 1024 * 1024;
    state.importFileChosen = supportedType && supportedSize;
    state.importFileName = file?.name || '';
    state.importFileSize = file?.size || 0;
    if (name && file && !name.value.trim()) name.value = file.name.replace(/\.(zip|mrpack)$/i, '').slice(0, 70);
    const status = $('#import-file-status');
    if (status) status.textContent = !file ? 'ZIP or .mrpack, up to 2 GB. Choose an exported profile, full CurseForge install, or server pack.' : !supportedType ? 'Unsupported file type. Choose a .zip or .mrpack archive.' : !supportedSize ? 'Choose a non-empty archive no larger than 2 GB.' : `${file.name} · ${formatFileSize(file.size)}`;
    const submit = $('.import-submit');
    if (submit) submit.disabled = state.importBusy || !state.importFileChosen;
    syncInstallerSteps();
  }
  if (event.target.id === 'server-file-upload') {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (file) uploadServerFile(file);
  }
});

document.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === 'config-form') {
    const values = collectForm(form);
    try {
      state.config = await api('/api/config', {method:'POST', body:values});
      applyThemeColor(state.config.theme_color);
      toast('Settings saved.');
      render();
    } catch (err) { toast(err.message, 'error'); }
  } else if (form.id === 'client-manifest-form') {
    const content = $('#client-manifest-json')?.value ?? state.clientManifestText;
    state.clientManifestSaving = true;
    render();
    try {
      const result = await api('/api/client-only-manifest', {method:'POST', body:{content}});
      state.clientManifestPath = result.path || state.clientManifestPath;
      state.clientManifestText = result.content || content;
      state.clientManifestSavedText = state.clientManifestText;
      state.clientManifestDirty = false;
      toast('Client-only manifest saved.');
    } catch (err) { toast(err.message, 'error'); }
    finally { state.clientManifestSaving = false; render(); }
  } else if (form.id === 'new-folder-form') {
    const values = collectForm(form);
    try {
      await api(`/api/servers/${encodeURIComponent(state.currentId)}/files`, {method:'POST', body:{operation:'mkdir', path:state.filePath, name:values.name}});
      state.fileNewFolderOpen = false;
      toast('Folder created.');
      await loadServerFiles(state.filePath);
    } catch (err) { toast(err.message, 'error'); }
  } else if (form.id === 'file-editor-form') {
    state.fileSaving = true;
    const content = $('#file-editor-content')?.value ?? state.fileEditorContent;
    try {
      await api(`/api/servers/${encodeURIComponent(state.currentId)}/files`, {method:'POST', body:{operation:'write', path:state.fileOpenPath, content}});
      state.fileEditorContent = content;
      toast('File saved.');
      await loadServerFiles(state.filePath);
    } catch (err) { toast(err.message, 'error'); }
    finally { state.fileSaving = false; render(); }
  } else if (form.id === 'create-form') {
    if (!state.chosenPack || !state.selectedFileId) { toast('Choose a modpack release first.', 'error'); return; }
    if (state.createBusy) return;
    const values = collectForm(form);
    values.source = 'modrinth';
    values.project_id = state.chosenPack.id;
    values.file_id = state.selectedFileId;
    values.pack_name = state.chosenPack.name;
    values.pack_icon = state.chosenPack.icon || '';
    state.createBusy = true;
    const installButton = $('.create-install', form);
    if (installButton) { installButton.disabled = true; installButton.textContent = 'Adding server…'; }
    syncInstallerSteps();
    try {
      const result = await api('/api/servers', {method:'POST', body:values});
      state.currentId = result.server.id;
      state.view = 'servers';
      state.tab = 'overview';
      state.lastSeq = 0;
      toast('Server added. Setup is starting in the background.');
      await refreshState();
    } catch (err) { toast(err.message, 'error'); }
    finally {
      state.createBusy = false;
      const button = $('.create-install');
      if (button && state.view === 'discover') {
        button.disabled = !state.chosenPack || !state.files.length || state.filesBusy;
        button.innerHTML = '<span>＋</span> Install &amp; prepare server';
        syncInstallerSteps();
      }
    }
  } else if (form.id === 'curseforge-create-form') {
    if (!state.curseforgeSelectedPack || !state.curseforgeFileId) { toast('Choose a CurseForge modpack release first.', 'error'); return; }
    if (!state.config?.curseforge_api_key_set) { toast('Add a CurseForge API key in Settings first.', 'error'); return; }
    if (state.curseforgeInstallBusy) return;
    const values = collectForm(form);
    values.mod_id = state.curseforgeSelectedPack.id;
    values.file_id = state.curseforgeFileId;
    values.pack_name = state.curseforgeSelectedPack.name;
    values.pack_icon = state.curseforgeSelectedPack.icon || '';
    state.curseforgeInstallBusy = true;
    const installButton = $('.create-install', form);
    if (installButton) { installButton.disabled = true; installButton.textContent = 'Adding server…'; }
    syncInstallerSteps();
    try {
      const result = await api('/api/curseforge/install', {method:'POST', body:values});
      state.currentId = result.server.id;
      state.view = 'servers';
      state.tab = 'overview';
      state.lastSeq = 0;
      toast('CurseForge server added. Setup is starting in the background.');
      await refreshState();
    } catch (err) { toast(err.message, 'error'); }
    finally {
      state.curseforgeInstallBusy = false;
      const button = $('.create-install');
      if (button && state.view === 'discover' && state.installerMode === 'curseforge') {
        button.disabled = !state.curseforgeSelectedPack || !state.curseforgeFiles.length || state.curseforgeFilesBusy || !state.config?.curseforge_api_key_set;
        button.innerHTML = '<span>＋</span> Install &amp; prepare server';
        syncInstallerSteps();
      }
    }
  } else if (form.id === 'local-import-form') {
    const file = $('#import_pack_file')?.files?.[0];
    const values = collectForm(form);
    if (!file) { toast('Choose a modpack archive to import.', 'error'); return; }
    if (!file.name.toLowerCase().endsWith('.zip') && !file.name.toLowerCase().endsWith('.mrpack')) { toast('Choose a .zip or .mrpack archive.', 'error'); return; }
    if (file.size <= 0 || file.size > 2 * 1024 * 1024 * 1024) { toast('Choose a non-empty modpack archive no larger than 2 GB.', 'error'); return; }
    state.importBusy = true;
    state.importProgress = 0;
    render();
    const metadata = {minecraft_version:values.minecraft_version || '', loader_type:values.loader_type || 'auto', loader_version:values.loader_version || ''};
    const params = new URLSearchParams({name:values.name || '', root:values.root || '', filename:file.name, file_size:String(file.size), ...metadata});
    try {
      await api('/api/servers/import-pack/check', {method:'POST', body:{name:values.name || '', root:values.root || '', filename:file.name, file_size:file.size, ...metadata}});
      const result = await uploadServerPack(`/api/servers/import-pack?${params}`, file, (loaded, total) => {
        state.importProgress = total ? loaded / total * 100 : 0;
        const progress = $('#import-progress');
        const label = $('#import-progress-label');
        if (progress) progress.value = state.importProgress;
        if (label) label.textContent = `${Math.round(state.importProgress)}%`;
      });
      state.currentId = result.server.id;
      state.view = 'servers';
      state.tab = 'overview';
      state.lastSeq = 0;
      toast('Modpack uploaded. Blockyard is converting it for a server.');
      await refreshState();
    } catch (err) { toast(err.message, 'error'); }
    finally {
      state.importBusy = false;
      state.importProgress = 0;
      if (state.view === 'discover') {
        state.importFileChosen = false;
        state.importFileName = '';
        state.importFileSize = 0;
        render();
      }
    }
  } else if (form.id === 'properties-form' || form.id === 'network-form') {
    const values = collectForm(form);
    try {
      await api(`/api/servers/${state.currentId}/settings`, {method:'POST', body:{settings:values}});
      toast('Server settings saved.');
      await refreshState();
    } catch (err) { toast(err.message, 'error'); }
  } else if (form.id === 'console-form') {
    const input = $('#console-input');
    const command = input.value.trim();
    if (!command) return;
    try { await api(`/api/servers/${state.currentId}/input`, {method:'POST',body:{command}}); input.value = ''; }
    catch (err) { toast(err.message, 'error'); }
  }
});

async function poll() {
  if (state.pollBusy) return;
  state.pollBusy = true;
  try {
    // Keep checking while offline too: another tab or a server process that
    // was started outside this view can change the backend's live state.
    await refreshServersQuietly();
  } catch { /* keep the current view visible during a transient local request */ }
  finally { state.pollBusy = false; }
}

async function init() {
  try {
    await refreshState();
    if (state.view === 'discover') await doSearch('');
  } catch (err) {
    $('#main-content').innerHTML = `<div class="empty-results"><strong>Couldn’t connect to Blockyard</strong>${esc(err.message)}<br>Close and reopen the app.</div>`;
  }
  state.pollTimer = setInterval(poll, 1800);
}

init();
