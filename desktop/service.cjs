const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { EventEmitter } = require('node:events');
const { existsSync } = require('node:fs');
const path = require('node:path');
const { createInterface } = require('node:readline');

const PREFIX = 'ALISUPPLIER_DESKTOP_READY ';
const STARTUP_TIMEOUT = 20000;
const SHUTDOWN_TIMEOUT = 4500;

class DesktopService extends EventEmitter {
  constructor({ backend, bundled }) {
    super();
    this.backend = backend;
    this.bundled = bundled;
    this.status = { phase: 'idle', message: '' };
    this.child = null;
    this.origin = null;
    this.token = null;
  }

  setStatus(phase, message = '') {
    this.status = { phase, message };
    this.emit('status', this.status);
  }

  candidates() {
    if (this.bundled) {
      return process.platform === 'win32'
        ? [[path.join(this.backend, 'python', 'python.exe'), []]]
        : [[path.join(this.backend, 'python', 'bin', 'python3'), []]];
    }
    const override = process.env.ALISUPPLIER_PYTHON;
    if (override) return [[override, []]];
    return process.platform === 'win32' ? [['py', ['-3']], ['python', []]] : [['python3', []]];
  }

  async start() {
    if (this.status.phase === 'running') return this.origin;
    if (this.status.phase === 'starting' || this.status.phase === 'stopping') return null;

    this.setStatus('starting');
    this.token = randomBytes(32).toString('hex');
    try {
      for (const [command, prefix] of this.candidates()) {
        const startup = await this.launch(command, prefix);
        if (startup.missing) continue;
        if (!startup.origin || this.status.phase !== 'starting') return null;
        this.origin = startup.origin;
        this.setStatus('running');
        return this.origin;
      }
      throw new Error(this.bundled ? '发行包里的 Python 运行环境缺失。请重新解压应用。' : '找不到 Python 3，请安装 Python 或设置 ALISUPPLIER_PYTHON。');
    } catch (error) {
      if (this.status.phase !== 'stopping' && this.status.phase !== 'idle') {
        this.origin = null;
        this.token = null;
        this.setStatus('error', error.message);
      }
      return null;
    }
  }

  launch(command, prefix) {
    return new Promise((resolve, reject) => {
      let stderr = '';
      let settled = false;
      let failedToSpawn = false;
      const env = { ...process.env, ALISUPPLIER_DESKTOP_TOKEN: this.token, PYTHONIOENCODING: 'utf-8' };
      delete env.PYTHONHOME;
      delete env.PYTHONPATH;
      const certificates = path.join(this.backend, 'cacert.pem');
      if (!env.SSL_CERT_FILE && existsSync(certificates)) env.SSL_CERT_FILE = certificates;

      const child = spawn(command, [...prefix, '-B', '-u', path.join(this.backend, 'server.py'),
        '--desktop', '--port', '0'], {
        cwd: this.backend, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true
      });
      this.child = child;
      let closed;
      this.closed = new Promise(done => { closed = done; });
      const timeout = setTimeout(() => {
        settled = true;
        child.stdin.end();
        this.killAfter(child);
        reject(new Error('后台服务启动超时。请检查应用数据目录是否可写后重试。'));
      }, STARTUP_TIMEOUT);

      child.stdin.on('error', () => {}); // The backend can exit before the pipe closes.
      child.stderr.setEncoding('utf8').on('data', chunk => { stderr = (stderr + chunk).slice(-1600); });
      const lines = createInterface({ input: child.stdout });
      lines.on('line', line => {
        if (settled || this.status.phase !== 'starting' || !line.startsWith(PREFIX)) return;
        try {
          const { port } = JSON.parse(line.slice(PREFIX.length));
          if (!Number.isInteger(port) || port < 1 || port > 65535) return;
          settled = true;
          clearTimeout(timeout);
          resolve({ origin: 'http://127.0.0.1:' + port });
        } catch (_) { /* Ignore partial or invalid readiness data. */ }
      });

      child.once('error', error => {
        if (settled) return;
        settled = true;
        failedToSpawn = true;
        clearTimeout(timeout);
        if (this.child === child) this.child = null;
        if (error.code === 'ENOENT') resolve({ missing: true });
        else reject(new Error('无法启动后台服务：' + error.message));
      });

      child.once('close', code => {
        clearTimeout(timeout);
        lines.close();
        const current = this.child === child;
        if (current) this.child = null;
        closed();
        if (failedToSpawn || !current) return;
        if (this.status.phase === 'stopping') {
          if (!settled) resolve({});
          this.origin = null;
          this.token = null;
          this.setStatus('idle');
          return;
        }
        const detail = stderr.trim().split(/\r?\n/).filter(Boolean).at(-1);
        const message = detail ? '后台服务退出：' + detail : '后台服务已退出（' + (code ?? '信号') + '）。';
        if (!settled) {
          settled = true;
          reject(new Error(message));
        } else if (this.status.phase === 'running') {
          this.origin = null;
          this.token = null;
          this.setStatus('error', message);
        }
      });
    });
  }

  killAfter(child) {
    const deadline = setTimeout(() => {
      if (this.child === child) child.kill('SIGKILL');
    }, SHUTDOWN_TIMEOUT);
    deadline.unref();
    child.once('close', () => clearTimeout(deadline));
  }

  async stop() {
    if (!this.child) {
      this.origin = null;
      this.token = null;
      this.setStatus('idle');
      return;
    }
    if (this.status.phase === 'stopping') return this.closed;
    this.setStatus('stopping');
    const child = this.child;
    child.stdin.end();
    this.killAfter(child);
    await this.closed;
  }
}

module.exports = { DesktopService };
