const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { createReadStream } = require('node:fs');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { parseArgs } = require('node:util');
const pkg = require('./package.json');
const electronChecksums = require('electron/checksums.json');

const appName = 'AliSupplier-Workbench';
const parent = path.join(__dirname, '..');
const cache = path.join(__dirname, '.cache');
const tar = process.platform === 'win32' ? 'tar.exe' : '/usr/bin/tar';

// Pinned artifacts from Astral Python Build Standalone and PyPI.
const pythonVersion = '3.13.18';
const pythonRelease = '20261026';
const targets = {
  'darwin-arm64': ['aarch64-apple-darwin', '8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405'],
  'darwin-x64': ['x86_64-apple-darwin', '7412c19d896c99431b74a18a5643e110449a7f9590fbab179c3e5ab27b9aa9e3'],
  'win32-x64': ['x86_64-pc-windows-msvc', '7369f7a35f261222a6ac8b3a87c1c88eeebee8af945d85346202591a64038dd3'],
  'win32-arm64': ['aarch64-pc-windows-msvc', 'bd3315abb8abb8ce5b19c2c2b709d318f983e0b8753d0830831df07c46474699'],
};
const ca = {
  url: 'https://files.pythonhosted.org/packages/6d/39/5eec3296860b94ac20bb8ed807725b88f6adfab3f343df592afd975f5f86/certifi-2026.11.20-py3-none-any.whl',
  hash: '7422df25243767b9877031a2248cb14d09ddac9beee846f4cef3d5c9191d488f',
};

function run(command, args, extra = {}) {
  const result = spawnSync(command, args, { ...extra, stdio: 'inherit', windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(command + ' exited with ' + result.status);
}

async function checksum(file) {
  const hash = createHash('sha256');
  for await (const bytes of createReadStream(file)) hash.update(bytes);
  return hash.digest('hex');
}

async function download(name, url, expected) {
  await fs.mkdir(cache, { recursive: true });
  const final = path.join(cache, name);
  if (await checksum(final).catch(error => {
    if (error.code === 'ENOENT') return null;
    throw error;
  }) === expected) return final;

  const temp = path.join(cache, name + '.' + process.pid + '.part');
  console.log('Downloading ' + name);
  try {
    // curl uses the platform trust store and respects the host proxy configuration.
    const curl = process.platform === 'win32' ? 'curl.exe' : '/usr/bin/curl';
    run(curl, ['-fLsS', '--retry', '2', '--connect-timeout', '20', '--max-time', '240', '--output', temp, url]);
    if (await checksum(temp) !== expected) throw new Error(name + ' checksum mismatch');
    await fs.rename(temp, final);
    return final;
  } finally {
    await fs.rm(temp, { force: true });
  }
}

async function makeBackend(temp, platform, target, hash) {
  const backend = path.join(temp, 'backend');
  const caDir = path.join(temp, 'certificates');
  await Promise.all([fs.mkdir(backend), fs.mkdir(caDir)]);
  const pythonFile = 'cpython-' + pythonVersion + '+' + pythonRelease + '-' + target + '-install_only_stripped.tar.gz';
  const pythonUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/' +
    pythonRelease + '/' + encodeURIComponent(pythonFile);
  // Both downloads are checksum-verified before extracting anything.
  const [python, certificates] = await Promise.all([
    download(pythonFile, pythonUrl, hash),
    download('certifi-2026.11.20.whl', ca.url, ca.hash),
  ]);
  run(tar, ['-xf', python, '-C', backend]);
  run(tar, ['-xf', certificates, '-C', caDir]);
  await fs.copyFile(path.join(caDir, 'certifi', 'cacert.pem'), path.join(backend, 'cacert.pem'));
  await fs.copyFile(path.join(caDir, 'certifi-2026.11.20.dist-info', 'licenses', 'LICENSE'),
    path.join(backend, 'certifi-LICENSE'));
  await fs.access(platform === 'win32' ? path.join(backend, 'python', 'python.exe') :
    path.join(backend, 'python', 'bin', 'python3'));
  await Promise.all(['server.py', 'ai_gateway.py', 'workbench.py', 'workbench_media.py', 'frida_agent.js'].map(file =>
    fs.copyFile(path.join(parent, file), path.join(backend, file))));
  await fs.cp(path.join(parent, 'ui'), path.join(backend, 'ui'), { recursive: true });
  return backend;
}

async function ensureElectron(platform, arch) {
  const version = pkg.devDependencies.electron;
  const archive = 'electron-v' + version + '-' + platform + '-' + arch + '.zip';
  const digest = electronChecksums[archive];
  if (!digest) throw new Error('No matching Electron checksum for ' + archive);
  return download(archive, 'https://github.com/electron/electron/releases/download/v' + version + '/' + archive, digest);
}

async function main() {
  const { values } = parseArgs({
    options: { platform: { type: 'string', default: process.platform }, arch: { type: 'string', default: process.arch } },
  });
  const { platform, arch } = values;
  const python = targets[platform + '-' + arch];
  if (!python) throw new Error('Supported targets: ' + Object.keys(targets).join(', '));
  if (platform === 'darwin' && process.platform !== 'darwin') throw new Error('Build macOS distributions on macOS.');
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'alisupplier-desktop-'));
  try {
    const electronVersion = pkg.devDependencies.electron;
    const binary = await ensureElectron(platform, arch);
    const backend = await makeBackend(temp, platform, ...python);
    const { packager } = await import('@electron/packager');
    const dist = path.join(parent, 'dist');
    const [packageDir] = await packager({
      dir: __dirname, out: dist, name: appName, platform, arch, overwrite: true,
      electronVersion, electronZipDir: path.dirname(binary), asar: true, prune: true,
      appBundleId: 'com.alisupplier.workbench', appVersion: pkg.version,
      icon: platform === 'darwin' ? path.join(__dirname, 'icon.icns') : undefined,
      ignore: /^\/(?:tests|\.cache|node_modules|(?:build|dev)\.cjs|package-lock\.json)(?:\/|$)/,
    });
    const resources = platform === 'darwin'
      ? path.join(packageDir, appName + '.app', 'Contents', 'Resources')
      : path.join(packageDir, 'resources');
    await fs.cp(backend, path.join(resources, 'backend'), { recursive: true, verbatimSymlinks: true });
    const zip = path.join(dist, appName + '-' + pkg.version + '-' + platform + '-' + arch + '.zip');
    const stagingZip = path.join(temp, path.basename(zip));
    if (platform === 'darwin') {
      const bundle = path.join(packageDir, appName + '.app');
      run('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', bundle]);
      run('/usr/bin/codesign', ['--verify', '--deep', '--strict', bundle]);
      run('/usr/bin/ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', bundle, stagingZip]);
    } else if (process.platform === 'win32') {
      run(tar, ['-a', '-cf', stagingZip, '-C', path.dirname(packageDir), path.basename(packageDir)]);
    } else {
      run('/usr/bin/zip', ['-qry', stagingZip, path.basename(packageDir)], { cwd: path.dirname(packageDir) });
    }
    await fs.copyFile(stagingZip, zip);
    await fs.writeFile(zip + '.sha256', (await checksum(zip)) + '  ' + path.basename(zip) + '\n');
    console.log('Release: ' + zip);
  } finally {
    await fs.rm(temp, { recursive: true, force: true });
  }
}

if (require.main === module) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
module.exports = { ensureElectron };
