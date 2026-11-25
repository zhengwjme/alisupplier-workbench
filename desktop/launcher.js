'use strict';

const byId = id => document.getElementById(id);
const desktop = window.alisupplierDesktop;
const labels = {
  idle: '服务未启动', starting: '正在启动服务', running: '正在打开工作台',
  stopping: '正在停止服务', error: '服务启动失败',
};

function render({ phase, message = '' }) {
  const status = byId('status');
  const start = byId('start');
  status.textContent = labels[phase] || labels.idle;
  status.classList.toggle('error', phase === 'error');
  start.disabled = !['idle', 'error'].includes(phase);
  start.textContent = phase === 'error' ? '重试启动' : '启动服务';
  byId('stop').hidden = phase !== 'starting';
  byId('message').hidden = !message;
  byId('message').textContent = message;
}

byId('start').addEventListener('click', async () => {
  render({ phase: 'starting' });
  try { await desktop.start(); }
  catch (error) { render({ phase: 'error', message: error.message }); }
});
byId('stop').addEventListener('click', () => desktop.stop());
desktop.onStatus(render);
desktop.status().then(render);
