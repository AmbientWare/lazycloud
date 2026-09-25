"""Complete Claude's remote browser login and first-run configuration."""

CLAUDE_LOGIN_SCRIPT = r"""
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn, spawnSync } = require('node:child_process');

class LoginError extends Error {}

let child;
let childExit;
async function stopLogin() {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  const timer = setTimeout(() => child.kill('SIGKILL'), 2000);
  try { await childExit; } finally { clearTimeout(timer); }
}
for (const [signal, code] of [['SIGHUP', 129], ['SIGINT', 130], ['SIGTERM', 143]]) {
  process.once(signal, async () => {
    await stopLogin();
    process.exit(code);
  });
}

function signedIn() {
  const result = spawnSync('claude', ['auth', 'status'], {
    stdio: 'ignore', timeout: 15000, killSignal: 'SIGKILL',
  });
  if (result.error || ![0, 1].includes(result.status)) {
    throw new LoginError('Cannot verify Claude authentication. ' +
      'Run claude auth status inside the devbox.');
  }
  return result.status === 0;
}

async function login() {
  if (signedIn()) return;
  // A real terminal keeps Claude's code-paste prompt unbuffered.
  child = spawn('script', [
    '--quiet', '--return', '--flush', '--command', 'claude auth login', '/dev/null',
  ], { stdio: ['inherit', 'pipe', 'inherit'] });
  childExit = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => resolve(code));
  });
  let pending = '';
  const reportedSuccess = new Promise((resolve) => {
    child.stdout.on('data', (chunk) => {
      process.stdout.write(chunk);
      pending += chunk.toString();
      const lines = pending.split('\n');
      pending = lines.pop().slice(-1024);
      for (const line of lines) {
        const plain = line.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').trim();
        if (/^(?:Paste code here if prompted > )?Login successful\.$/.test(plain)) {
          resolve(true);
        }
      }
    });
  });
  const outcome = await Promise.race([
    reportedSuccess.then(() => ({ reported: true })),
    childExit.then((code) => ({ code })),
  ]);
  if (!outcome.reported && outcome.code !== 0) {
    throw new LoginError('Claude login did not complete. Retry login for this devbox.');
  }
  // Claude can leave its terminal prompt or shutdown work alive after saving credentials.
  // Its auth status, not terminal output, decides whether login succeeded.
  if (!signedIn()) throw new LoginError('Claude did not save a usable login. Run login again.');
  await stopLogin();
}

function completeOnboarding() {
  const directory = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
  const legacy = path.join(directory, '.config.json');
  const configured = fs.existsSync(legacy) ? legacy :
    path.join(process.env.CLAUDE_CONFIG_DIR || os.homedir(), '.claude.json');
  const config = fs.realpathSync(configured);
  const version = spawnSync('claude', ['--version'], {
    encoding: 'utf8', timeout: 15000, killSignal: 'SIGKILL',
  });
  const release = version.status === 0 && /^(\d+\.\d+\.\d+)\b/.exec(version.stdout);
  if (!release) throw new LoginError('Cannot determine the installed Claude version.');
  const lock = configured + '.lock';
  // Claude's config writer uses this same directory lock. Never remove another writer's lock.
  try { fs.mkdirSync(lock); } catch (error) {
    if (error.code === 'EEXIST') {
      throw new LoginError('Claude settings are being written by another process. ' +
        'Retry login once it finishes.');
    }
    throw error;
  }
  let temporary;
  try {
    const value = JSON.parse(fs.readFileSync(config, 'utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new LoginError('Claude settings are not a JSON object; leaving them unchanged.');
    }
    if (value.hasCompletedOnboarding === true) return;
    value.hasCompletedOnboarding = true;
    value.lastOnboardingVersion = release[1];
    temporary = path.join(path.dirname(config),
      `.lazycloud-claude-${process.pid}-${Date.now()}.json`);
    const stat = fs.statSync(config);
    const fd = fs.openSync(temporary, 'wx', stat.mode & 0o777);
    try {
      fs.fchownSync(fd, stat.uid, stat.gid);
      fs.fchmodSync(fd, stat.mode & 0o777);
      fs.writeFileSync(fd, JSON.stringify(value, null, 2) + '\n');
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, config);
    temporary = undefined;
  } finally {
    try {
      if (temporary) fs.unlinkSync(temporary);
    } finally { fs.rmdirSync(lock); }
  }
}

(async () => {
  try {
    await login();
    completeOnboarding();
    process.stdout.write('Claude Code is signed in and ready.\n');
  } catch (error) {
    await stopLogin();
    // Do not echo arbitrary native errors: they can contain credentials or config contents.
    process.stderr.write('Claude login could not finish: ' +
      (error instanceof LoginError ? error.message :
        'could not read or save Claude settings; existing settings were preserved') + '\n');
    process.exitCode = 1;
  }
})();
"""
