const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const { ensureElectron } = require('./build.cjs');
const pkg = require('./package.json');

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'inherit', windowsHide: true });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(command + ' exited with ' + (signal || code)));
    });
  });
}

async function main() {
  const destination = path.join(__dirname, '.cache', 'runtime-' + pkg.devDependencies.electron + '-' +
    process.platform + '-' + process.arch);
  const entry = process.platform === 'darwin' ? 'Electron.app/Contents/MacOS/Electron' : 'electron.exe';
  const electron = path.join(destination, entry);
  if (!await fs.access(electron).then(() => true, () => false)) {
    const archive = await ensureElectron(process.platform, process.arch);
    const unpacked = await fs.mkdtemp(path.join(__dirname, '.cache', 'runtime-stage-'));
    try {
      if (process.platform === 'darwin') await run('/usr/bin/ditto', ['-x', '-k', archive, unpacked]);
      else await run('tar.exe', ['-xf', archive, '-C', unpacked]);
      await fs.rename(unpacked, destination);
    } finally {
      await fs.rm(unpacked, { recursive: true, force: true });
    }
  }
  await run(electron, [__dirname]);
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
