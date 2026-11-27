// Noxa for Windows, macOS and Linux.
// The window shows the interface of the Noxa server chosen at first launch: so it follows each update of the
// server. The engine is the app's (Chromium), the same everywhere: no difference between browsers.
// The program itself updates from the server (/apps/desktop, files published with each version).
const { app, BrowserWindow, Menu, dialog, ipcMain, net, powerSaveBlocker, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { strings, fill } = require('./strings');

// data folder of the former name (Nexis): chosen server and offline downloads kept after the rename
app.setPath('userData', path.join(app.getPath('appData'), 'Nexis'));
const CONFIG = path.join(app.getPath('userData'), 'nexis.json');
const readConfig = () => { try { return JSON.parse(fs.readFileSync(CONFIG, 'utf8')); } catch { return {}; } };
const writeConfig = (c) => { fs.mkdirSync(path.dirname(CONFIG), { recursive: true }); fs.writeFileSync(CONFIG, JSON.stringify(c, null, 1)); };
let config = readConfig();
const originOf = (u) => { try { return new URL(u).origin; } catch { return null; } };

// Server over http on the local network: treated as secure by the engine (service worker, offline downloads),
// which no browser does. Only for that server, chosen by the person.
if (config.server && config.server.startsWith('http://') && originOf(config.server)) {
  app.commandLine.appendSwitch('unsafely-treat-insecure-origin-as-secure', originOf(config.server));
}

let win = null;
let T = strings('fr');

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
}

function createWindow() {
  const b = config.bounds || {};
  win = new BrowserWindow({
    width: b.width || 1280, height: b.height || 800, x: b.x, y: b.y, minWidth: 720, minHeight: 480,
    backgroundColor: '#0b0b0e', title: 'Noxa', autoHideMenuBar: true, show: false,
    icon: path.join(__dirname, 'build', 'icon.png'),
    // backgroundThrottling: window minimised or hidden, the page keeps the same pace (downloads)
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, spellcheck: false,
      backgroundThrottling: false },
  });
  if (config.maximized) win.maximize();
  win.once('ready-to-show', () => win.show());
  win.on('close', () => {
    config = { ...readConfig(), bounds: win.getNormalBounds(), maximized: win.isMaximized() };
    writeConfig(config);
  });
  const home = originOf(config.server);
  // links to another site: in the computer's browser; the rest stays in the app
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (home && originOf(url) === home) return { action: 'allow' };
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (url.startsWith('file:') || (home && originOf(url) === home)) return;
    e.preventDefault();
    if (/^https?:/.test(url)) shell.openExternal(url);
  });
  win.webContents.on('did-fail-load', (_e, code, desc, url, isMain) => {
    if (!isMain || code === -3) return; // -3: load aborted (next navigation)
    win.loadFile('setup.html', { query: { offline: config.server || '', error: desc || String(code) } });
  });
  // F11 / Cmd+Ctrl+F: fullscreen; F5 / Ctrl+R: reload
  win.webContents.on('before-input-event', (e, input) => {
    if (input.type !== 'keyDown') return;
    if (input.key === 'F11') { win.setFullScreen(!win.isFullScreen()); e.preventDefault(); }
    if (input.key === 'F5' || ((input.control || input.meta) && input.key.toLowerCase() === 'r')) { win.webContents.reload(); e.preventDefault(); }
  });
  if (config.server) win.loadURL(config.server);
  else win.loadFile('setup.html');
}

// ------------------------------------------------------------------ server choice
async function probe(base) {
  const res = await net.fetch(`${base}/api/status`, { headers: { 'X-Noxa': '1' }, signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const j = await res.json();
  if (!j || typeof j.version !== 'string') throw new Error('not nexis');
  return j;
}

async function setServer(raw) {
  let s = String(raw || '').trim().replace(/\/+$/, '');
  if (!s) return { ok: false, error: T.notNexis };
  const candidates = /^https?:\/\//i.test(s) ? [s] : [`https://${s}`, `http://${s}`];
  let last = null;
  for (const c of candidates) {
    if (!originOf(c)) continue;
    try {
      await probe(c);
      const insecureBefore = (config.server || '').startsWith('http://') ? originOf(config.server) : null;
      config = { ...readConfig(), server: originOf(c) + (new URL(c).pathname.replace(/\/+$/, '')) };
      writeConfig(config);
      // an http server must be declared when the engine starts: the app is relaunched
      if (config.server.startsWith('http://') && originOf(config.server) !== insecureBefore) {
        app.relaunch();
        app.exit(0);
        return { ok: true };
      }
      win.loadURL(config.server);
      setupUpdates();
      return { ok: true };
    } catch (e) {
      last = e;
    }
  }
  return { ok: false, error: last && last.message === 'not nexis' ? T.notNexis : fill(T.unreachable, { error: last ? last.message : '?' }) };
}

// the server only changes from the app's own server choice screen (setup.html), never from a page of a website
ipcMain.handle('nexis:set-server', (e, url) => {
  const from = (e.senderFrame && e.senderFrame.url) || '';
  if (!from.startsWith('file:')) throw new Error('not allowed');
  return setServer(url);
});
ipcMain.handle('nexis:change-server', () => { win.loadFile('setup.html', { query: { change: '1', current: config.server || '' } }); });
ipcMain.handle('nexis:retry', () => { if (config.server) win.loadURL(config.server); });
ipcMain.handle('nexis:check-update', () => checkUpdates(true));
// download in progress: the computer does not go to sleep (the screen can turn off)
let blocker = null;
ipcMain.on('nexis:downloads', (_e, json) => {
  let active = 0;
  let percent = 0;
  try { ({ active = 0, percent = 0 } = JSON.parse(json)); } catch { /* ignore */ }
  if (active > 0 && blocker === null) blocker = powerSaveBlocker.start('prevent-app-suspension');
  if (active <= 0 && blocker !== null) { powerSaveBlocker.stop(blocker); blocker = null; }
  if (win) win.setProgressBar(active > 0 ? Math.max(0.01, percent / 100) : -1);
});
ipcMain.on('nexis:info', (e) => { e.returnValue = { version: app.getVersion(), os: process.platform, server: config.server || null, strings: T }; });

// ------------------------------------------------------------------ program updates
// Windows (installer) and Linux (AppImage): downloaded from the server and installed at restart.
// macOS and .deb: without an Apple signature or with a system-managed package, the download is offered.
let updater = null;
function canSelfUpdate() {
  return process.platform === 'win32' || (process.platform === 'linux' && !!process.env.APPIMAGE);
}

function setupUpdates() {
  if (!config.server || !app.isPackaged) return;
  if (updater) { updater.setFeedURL({ provider: 'generic', url: `${config.server}/apps/desktop` }); checkUpdates(false); return; }
  if (canSelfUpdate()) {
    try {
      ({ autoUpdater: updater } = require('electron-updater'));
      updater.setFeedURL({ provider: 'generic', url: `${config.server}/apps/desktop` });
      updater.autoDownload = true;
      updater.on('update-downloaded', async () => {
        const r = await dialog.showMessageBox(win, { type: 'info', buttons: [T.restart, T.later], defaultId: 0, cancelId: 1,
          message: T.updateReady, detail: T.updateReadyDetail });
        if (r.response === 0) updater.quitAndInstall();
      });
      updater.on('error', () => { /* server without published apps: will try again */ });
    } catch { updater = null; }
  }
  checkUpdates(false);
}

const newer = (a, b) => {
  const pa = String(a).split('.').map(Number); const pb = String(b).split('.').map(Number);
  for (let i = 0; i < 3; i++) if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) > (pb[i] || 0);
  return false;
};

async function checkUpdates(manual) {
  if (!config.server) return;
  if (updater) {
    try { const r = await updater.checkForUpdates(); if (manual && (!r || !r.isUpdateAvailable)) dialog.showMessageBox(win, { message: fill(T.upToDate, { version: app.getVersion() }) }); } catch { /* ignore */ }
    return;
  }
  try {
    const res = await net.fetch(`${config.server}/api/apps`, { signal: AbortSignal.timeout(10000) });
    const j = await res.json();
    const latest = j && j.versions && j.versions.desktop;
    if (latest && newer(latest, app.getVersion())) {
      const id = process.platform === 'darwin' ? 'macos' : 'linux-deb';
      const r = await dialog.showMessageBox(win, { type: 'info', buttons: [T.download, T.later], defaultId: 0, cancelId: 1,
        message: fill(T.updateAvailable, { version: latest }), detail: T.updateManual });
      if (r.response === 0) shell.openExternal(`${config.server}/apps/${id}`);
    } else if (manual) {
      dialog.showMessageBox(win, { message: fill(T.upToDate, { version: app.getVersion() }) });
    }
  } catch { /* hors ligne */ }
}

// ------------------------------------------------------------------ menu
function buildMenu() {
  const mac = process.platform === 'darwin';
  const template = [
    ...(mac ? [{ role: 'appMenu' }] : []),
    { label: T.menuServer, submenu: [
      { label: T.changeServer, click: () => win && win.loadFile('setup.html', { query: { change: '1', current: config.server || '' } }) },
      { label: T.menuCheckUpdate, click: () => checkUpdates(true) },
      { type: 'separator' },
      mac ? { role: 'close' } : { role: 'quit' },
    ] },
    { label: T.menuEdit, submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: T.menuView, submenu: [{ role: 'reload' }, { role: 'togglefullscreen' }, { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'toggleDevTools' }] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  T = strings(app.getLocale());
  buildMenu();
  createWindow();
  setupUpdates();
  setInterval(() => checkUpdates(false), 6 * 3600 * 1000);
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
