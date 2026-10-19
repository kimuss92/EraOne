// Exercise the actual pure frontend helpers with the project's compiler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../ui_src/node_modules/typescript');
const source = fs.readFileSync(path.join(__dirname, '../ui_src/src/lib/hudPresentation.ts'), 'utf8');
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: exportsObject });
const { resultIntent, freshResult, dockPlacement, isResultSurface } = exportsObject;
for (const text of ['Hide that', 'Era, please hide the result.', 'dismiss the email', 'close this widget', 'hide it please']) assert.equal(resultIntent(text), 'hide', text);
for (const text of ['show the result', 'bring back my email', 'restore last card again']) assert.equal(resultIntent(text), 'show', text);
for (const text of ["don't hide that", 'Should I hide the result?', 'she said hide that', 'hide that tomorrow', 'hide the file on disk', 'delete my email', 'close Brave']) assert.equal(resultIntent(text), null, text);
assert.equal(freshResult(100, 120), true);
assert.equal(freshResult(100, 161), false);
assert.equal(freshResult(NaN, 120), false);
assert.equal(freshResult(500, 120), false);
assert.equal(isResultSurface({ type: 'email', subtitle: 'Inbox' }), true);
assert.equal(isResultSurface({ type: 'media', subtitle: 'Playing', payload: { action: 'play' } }), false);
assert.equal(isResultSurface({ type: 'system', subtitle: 'Opened', payload: { action: 'open' } }), false);
assert.equal(isResultSurface({ type: 'system', subtitle: 'Information', payload: { action: 'info' } }), true);
const ids = ['core', 'activity', 'calls', 'schedule', 'system', 'awareness', 'context', 'sources', 'memory', 'weather'];
for (const order of [ids, [...ids].reverse()]) {
  const placements = order.map(id => dockPlacement(id, order));
  for (const p of placements) assert.ok(p.x >= 0 && p.y >= 0 && p.x + p.w <= 12 && p.y + p.h <= 28);
  for (let i = 0; i < placements.length; i++) for (let j = i + 1; j < placements.length; j++) {
    const a = placements[i], b = placements[j];
    assert.equal(a.x < b.x+b.w && b.x < a.x+a.w && a.y < b.y+b.h && b.y < a.y+a.h, false);
  }
  const core = dockPlacement('core', order);
  assert.equal(core.x, 3); assert.equal(core.w, 6); assert.equal(core.h, 28);
}
assert.deepEqual(JSON.parse(JSON.stringify(dockPlacement('system', ids))), { x: 0, y: 21, w: 3, h: 7 });
assert.deepEqual(JSON.parse(JSON.stringify(dockPlacement('awareness', ids))), { x: 9, y: 21, w: 3, h: 7 });
assert.deepEqual(JSON.parse(JSON.stringify(exportsObject.dropPlacement({x:0,y:0,w:6,h:8}, 11.8, -20))), {x:6,y:0,w:6,h:8});
console.log('PASS: result intent, stale-result suppression, bounded docks and non-overlap');
