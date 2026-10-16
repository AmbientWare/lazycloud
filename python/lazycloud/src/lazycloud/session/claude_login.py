"""Complete Claude's first-run configuration after a devbox login."""

CLAUDE_ONBOARDING_SCRIPT = r"""
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

class SetupError extends Error {}

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
  if (!release) throw new SetupError('Cannot determine the installed Claude version.');
  const lock = configured + '.lock';
  // Claude's config writer uses this same directory lock. Never remove another writer's lock.
  try { fs.mkdirSync(lock); } catch (error) {
    if (error.code === 'EEXIST') {
      throw new SetupError('Claude settings are being written by another process. ' +
        'Retry login once it finishes.');
    }
    throw error;
  }
  let temporary;
  try {
    const value = JSON.parse(fs.readFileSync(config, 'utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new SetupError('Claude settings are not a JSON object; leaving them unchanged.');
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

try {
  completeOnboarding();
} catch (error) {
  // Do not echo arbitrary native errors: they can contain credentials or config contents.
  process.stderr.write('Claude is signed in, but its first-run setup was not saved: ' +
    (error instanceof SetupError ? error.message :
      'could not read or save Claude settings; existing settings were preserved') + '\n');
  process.exitCode = 1;
}
"""
