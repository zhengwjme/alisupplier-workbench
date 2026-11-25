const { app, dialog } = require('electron');
const { createDesktop } = require('./main.cjs');

const desktop = createDesktop();
desktop?.ready.catch(error => {
  dialog.showErrorBox('AliSupplier 客户跟进', '无法打开客户端：' + error.message);
  app.exit(1);
});
