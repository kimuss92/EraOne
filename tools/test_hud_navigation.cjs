// Test real frontend modules in an isolated runtime: no audio or backend I/O.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../ui_src/node_modules/typescript');
function load(name, env) {
  const exports = {};
  const source = fs.readFileSync(path.join(__dirname, '../ui_src/src/lib/', name + '.ts'), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports, ...env });
  return exports;
}
let state = { voiceActive: false, agent: { state: 'idle' } }, listener;
const live = { amplitude: 0, micLevel: 0 };
const store = { getState: () => state, live, subscribe: cb => { listener = cb; }, setState: patch => { state = { ...state, ...patch }; } };
const events = {}, storage = new Map();
let clock = 1000, started = 0, stopped = 0, resumed = 0;
class AudioContext {
  state = 'running'; currentTime = 0; destination = {}; sampleRate = 100;
  resume() { resumed++; return Promise.resolve(); }
  param() { return { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} }; }
  node() { return { connect(node) { return node; }, disconnect() {} }; }
  source() { return { ...this.node(), frequency: this.param(), start: () => { started++; }, stop: () => { stopped++; } }; }
  createOscillator() { return this.source(); }
  createBufferSource() { return this.source(); }
  createBuffer() { return { getChannelData: () => new Float32Array(50) }; }
  createBiquadFilter() { return { ...this.node(), frequency: this.param(), Q: { value: 0 } }; }
  createGain() { return { ...this.node(), gain: this.param() }; }
}
const document = { hidden: false, addEventListener: (name, cb) => { events[name] = cb; } };
const sounds = load('sounds', { require: name => name === 'react' ? { useSyncExternalStore() {} } : store,
  document, AudioContext, window: { setTimeout: () => 0 }, performance: { now: () => clock },
  localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) } });
sounds.cue('ready'); assert.equal(started, 0, 'default is silent');
sounds.setSoundsEnabled(true);
sounds.cue('ready'); assert.equal(started, 0, 'backend events cannot unlock audio');
events.pointerdown({ isTrusted: false }); assert.equal(resumed, 0);
events.pointerdown({ isTrusted: true }); assert.equal(resumed, 1);
sounds.cue('focus'); assert.equal(started, 2);
sounds.cue('focus'); assert.equal(started, 2, 'repeated cues are rate limited');
for (const phase of ['listening', 'speaking', 'transcribing', 'synthesizing']) {
  state.agent.state = phase; clock += 1000; sounds.cue('tool'); assert.equal(started, 2, phase);
}
state.agent.state = 'listening';
const before = stopped; listener(); assert.ok(stopped > before, 'voice onset stops active cues');
state.agent.state = 'idle'; live.micLevel = 0.03; clock += 1000; sounds.cue('tool'); assert.equal(started, 2);
live.micLevel = 0; document.hidden = true; sounds.cue('tool'); assert.equal(started, 2);
document.hidden = false; sounds.setSoundsEnabled(false); sounds.cue('ready'); assert.equal(started, 2);
assert.equal(storage.get('era_hud_sounds'), '0');
sounds.setSoundsEnabled(true, false); sounds.cue('ready'); assert.equal(started, 2, 'local mute survives backend settings refresh');
const views = load('views', { require: name => name === './store' ? store : sounds });
for (const view of Object.keys(views.VIEWS)) {
  assert.equal(views.focusView(view), true);
  assert.equal(state.drawer, view === 'overview' ? null : view === 'tools' ? 'capabilities' : view);
  assert.equal(state.focusView, view);
}
const saved = state;
for (const invalid of [null, [], {}, '__proto__', 'constructor', 'shell', '']) assert.equal(views.focusView(invalid), false);
assert.equal(state, saved, 'invalid events cannot mutate selection');
console.log('PASS: all view destinations, invalid-event rejection, mute persistence, gesture gate, rate limit and voice suppression');
