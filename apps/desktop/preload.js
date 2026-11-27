// What the page sees of the app: window.noxaNative (version, change server, update), and the same object as
// window.nexisNative, the name read by servers older than 4.5.
// The server's interface uses it to know it runs inside the installed app.
const { contextBridge, ipcRenderer } = require('electron');

const info = ipcRenderer.sendSync('nexis:info');

const bridge = {
  kind: 'desktop',
  os: info.os,
  version: info.version,
  server: info.server,
  strings: info.strings,
  setServer: (url) => ipcRenderer.invoke('nexis:set-server', url),
  changeServer: () => ipcRenderer.invoke('nexis:change-server'),
  retry: () => ipcRenderer.invoke('nexis:retry'),
  checkUpdate: () => ipcRenderer.invoke('nexis:check-update'),
  downloads: (json) => ipcRenderer.send('nexis:downloads', String(json)), // download queue (sleep, taskbar)
};
contextBridge.exposeInMainWorld('noxaNative', bridge);
contextBridge.exposeInMainWorld('nexisNative', bridge);
