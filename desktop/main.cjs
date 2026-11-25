const { app, BrowserWindow, Menu, ipcMain, nativeImage, session } = require('electron');
const path = require('node:path');
const { readFileSync } = require('node:fs');
const { pathToFileURL } = require('node:url');
const { DesktopService } = require('./service.cjs');

const launcher = path.join(__dirname, 'launcher.html');
const launcherUrl = pathToFileURL(launcher).href;
const displayName = 'AliSupplier 客户跟进';

function createDesktop(options = {}) {
  const bundled = options.bundled ?? app.isPackaged;
  const backend = options.backend || (bundled ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, '..'));
  const service = new DesktopService({ backend, bundled });
  let window;
  let isQuitting = false;
  let desktopSession;
  let icon;

  function allowed(url) {
    if (url === launcherUrl) return true;
    try { return service.origin !== null && new URL(url).origin === service.origin; }
    catch (_) { return false; }
  }

  function rendererOptions() {
    return {
      session: desktopSession,
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true,
    };
  }

  function protect(contents, primary = false) {
    contents.on('will-navigate', (event, url) => {
      if (!allowed(url)) event.preventDefault();
    });
    contents.on('will-redirect', (event, url) => {
      if (!allowed(url)) event.preventDefault();
    });
    contents.setWindowOpenHandler(({ url }) => {
      if (!primary || !service.origin || !allowed(url)) return { action: 'deny' };
      const pathname = new URL(url).pathname;
      if (pathname !== '/api/workbench/image' && pathname !== '/media') return { action: 'deny' };
      return { action: 'allow', overrideBrowserWindowOptions: {
        width: 850, height: 700, webPreferences: rendererOptions(), autoHideMenuBar: true,
      }};
    });
    contents.on('did-create-window', child => protect(child.webContents));
  }

  function showLauncher() {
    if (window && !window.isDestroyed() && window.webContents.getURL() !== launcherUrl) {
      return window.loadFile(launcher);
    }
    return Promise.resolve();
  }

  async function start() {
    if (!window || window.isDestroyed()) return;
    const origin = await service.start();
    if (origin && service.status.phase === 'running' && !window.isDestroyed()) {
      try {
        await window.loadURL(origin + '/');
      } catch (error) {
        if (service.status.phase !== 'running') return;
        await service.stop();
        service.setStatus('error', '工作台页面加载失败：' + error.message);
      }
    }
  }

  async function stop() {
    await service.stop();
    await showLauncher();
  }

  const controlItems = () => [
    { label: '启动服务', enabled: ['idle', 'error'].includes(service.status.phase), click: () => void start() },
    { label: '停止服务', enabled: ['starting', 'running'].includes(service.status.phase), click: () => void stop() },
    { type: 'separator' },
    { role: 'quit', label: '退出客户端' },
  ];

  function updateMenu() {
    const template = [
      { label: '服务', submenu: controlItems() },
      { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
      { label: '视图', submenu: [{ role: 'reload', label: '重新加载' }, { role: 'togglefullscreen', label: '切换全屏' }] },
    ];
    if (process.platform === 'darwin') template.unshift({ label: displayName, submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'quit' }] });
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  }

  function installIpc() {
    const handle = (name, operation) => ipcMain.handle('service:' + name, (event) => {
      if (event.sender !== window?.webContents || event.senderFrame !== event.sender.mainFrame ||
          !allowed(event.senderFrame?.url || '')) throw new Error('无权访问服务控制');
      return operation();
    });
    // Return before navigation replaces the renderer that initiated the request.
    handle('start', () => { setImmediate(() => void start()); });
    handle('stop', () => { setImmediate(() => void stop()); });
    handle('status', () => service.status);
    handle('menu', () => { Menu.buildFromTemplate(controlItems()).popup({ window }); });
  }

  function createWindow() {
    window = new BrowserWindow({
      width: 1250, height: 830, minWidth: 405, minHeight: 560, show: false,
      title: displayName, icon, backgroundColor: '#f8f8f4', autoHideMenuBar: process.platform === 'win32',
      webPreferences: rendererOptions(),
    });
    protect(window.webContents, true);
    window.once('ready-to-show', () => window.show());
    window.on('closed', () => { window = null; });
    return service.origin ? window.loadURL(service.origin + '/') : window.loadFile(launcher);
  }

  app.setName(displayName);
  if (process.platform === 'win32') app.setAppUserModelId('com.alisupplier.workbench');
  const primary = options.skipSingleInstance || app.requestSingleInstanceLock();
  if (!primary) {
    app.quit();
    return null;
  }

  const ready = app.whenReady().then(async () => {
    icon = nativeImage.createFromBuffer(readFileSync(path.join(__dirname, 'icon.png')));
    desktopSession = session.fromPartition('alisupplier-workbench');
    desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    desktopSession.setPermissionCheckHandler(() => false);
    desktopSession.webRequest.onBeforeSendHeaders({ urls: ['http://127.0.0.1/*'] }, (details, callback) => {
      const { requestHeaders } = details;
      if (service.token && service.origin && new URL(details.url).origin === service.origin) {
        requestHeaders['X-AliSupplier-Desktop'] = service.token;
      }
      callback({ requestHeaders });
    });
    service.on('status', status => {
      updateMenu();
      if (window && !window.isDestroyed()) window.webContents.send('service:status-changed', status);
      if (status.phase === 'error') void showLauncher();
    });
    installIpc();
    updateMenu();
    if (process.platform === 'darwin') app.dock.setIcon(icon);
    await createWindow();
  });

  app.on('activate', () => { if (!window) void createWindow(); });
  app.on('second-instance', () => {
    if (!window) return;
    if (window.isMinimized()) window.restore();
    window.focus();
  });
  app.on('window-all-closed', () => {
    if (process.platform === 'darwin') void service.stop();
    else app.quit();
  });
  app.on('before-quit', event => {
    if (isQuitting || !service.child) return;
    event.preventDefault();
    isQuitting = true;
    void service.stop().finally(() => app.quit());
  });

  return { ready, service, getWindow: () => window };
}

module.exports = { createDesktop };
