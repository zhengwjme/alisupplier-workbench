const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('alisupplierDesktop', Object.freeze({
  start: () => ipcRenderer.invoke('service:start'),
  stop: () => ipcRenderer.invoke('service:stop'),
  status: () => ipcRenderer.invoke('service:status'),
  openControl: () => ipcRenderer.invoke('service:menu'),
  setWidgetMode: enabled => ipcRenderer.invoke('service:widget-mode', Boolean(enabled)),
  setAlwaysOnTop: enabled => ipcRenderer.invoke('service:always-on-top', Boolean(enabled)),
  getWindowMode: () => ipcRenderer.invoke('service:window-mode'),
  onStatus: callback => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('service:status-changed', listener);
    return () => ipcRenderer.removeListener('service:status-changed', listener);
  },
}));
