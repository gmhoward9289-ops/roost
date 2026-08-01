#!/usr/bin/env node
// npm's job here is delivery, not reimplementation. roost is one stdlib-only
// Python file and stays that way; this shim only finds a Python to run it with
// and gets out of the way.
//
// The reason to be on npm at all is Windows: there is no Windows package for
// roost, and `npm i -g roost-top` generates a real roost.cmd on PATH -- which is
// exactly the hand-rolled shim COOPER was maintaining by hand.
//
// There is deliberately no postinstall Python check. A failing postinstall would
// break `npm ci` in a project that merely lists roost as a devDependency; a
// missing interpreter is a run-time problem, so it is reported at run time.

'use strict';

const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const SCRIPT = path.join(__dirname, '..', 'roost.py');
const MIN = [3, 9]; // matches requires-python in pyproject.toml

// On Windows the py launcher goes first: it exists even when `python` resolves
// to the Microsoft Store app-execution alias, which is a stub that opens the
// Store and never runs anything. The probe below rejects that stub anyway
// (it prints no version), but trying py first avoids the detour.
const CANDIDATES =
  process.platform === 'win32'
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];

const PROBE = 'import sys; print("%d.%d" % sys.version_info[:2])';

function probe(cmd, pre) {
  const r = spawnSync(cmd, pre.concat(['-c', PROBE]), {
    encoding: 'utf8',
    windowsHide: true,
  });
  if (r.error || r.status !== 0) return null;
  const m = /^(\d+)\.(\d+)/.exec((r.stdout || '').trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2])];
}

function tooOld(v) {
  return v[0] < MIN[0] || (v[0] === MIN[0] && v[1] < MIN[1]);
}

let chosen = null;
const rejected = [];

for (const [cmd, pre] of CANDIDATES) {
  const v = probe(cmd, pre);
  if (!v) continue;
  if (tooOld(v)) {
    rejected.push(`${[cmd].concat(pre).join(' ')} is ${v[0]}.${v[1]}`);
    continue;
  }
  chosen = { cmd, pre };
  break;
}

if (!chosen) {
  const names = CANDIDATES.map(([c, p]) => [c].concat(p).join(' ')).join(', ');
  process.stderr.write(
    rejected.length
      ? `roost needs Python ${MIN.join('.')} or newer; found only: ${rejected.join(', ')}\n`
      : `roost needs Python ${MIN.join('.')} or newer on PATH (tried: ${names})\n`
  );
  process.stderr.write('  https://www.python.org/downloads/\n');
  process.exit(127);
}

// The child owns the terminal: roost puts it in raw mode and restores it on the
// way out. If this process died on Ctrl-C first, the shell would come back with
// the terminal still raw, so signals are swallowed here and the child is left to
// quit on its own -- which it does, q and Ctrl-C both.
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  try {
    process.on(sig, () => {});
  } catch (e) {
    // not every signal exists on every platform; skip the ones that don't
  }
}

const child = spawn(chosen.cmd, chosen.pre.concat([SCRIPT], process.argv.slice(2)), {
  stdio: 'inherit',
  windowsHide: true,
});

child.on('error', (err) => {
  process.stderr.write(`roost: could not run ${chosen.cmd}: ${err.message}\n`);
  process.exit(127);
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.exit(128 + (os.constants.signals[signal] || 0));
  }
  process.exit(code === null ? 1 : code);
});
