// What the page sees of the app: window.noxaNative (version, change server, update), and the same object as
// window.nexisNative, the name read by servers older than 4.5.
// L'interface du serveur s'en sert pour savoir qu'elle tourne dans l'application installée.
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
  downloads: (json) => ipcRenderer.send('nexis:downloads', String(json)), // file de téléchargement (veille, barre des tâches)
};
contextBridge.exposeInMainWorld('noxaNative', bridge);
contextBridge.exposeInMainWorld('nexisNative', bridge);
