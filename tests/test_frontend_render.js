#!/usr/bin/env node
/* Deterministic frontend rendering checks for peer snapshot semantics.

   Exercises frontend/assets/app.js render() with a minimal DOM stub so the
   "unavailable vs zero vs cached" distinction can be asserted without a
   browser. Run: node tests/test_frontend_render.js
*/
const fs = require('fs');
const path = require('path');

let failures = 0;
function assert(cond, label) {
  if (cond) { console.log(`  ok   ${label}`); }
  else { failures += 1; console.error(`  FAIL ${label}`); }
}

function escStr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function makeEl() {
  return {
    _t: '', _inner: '', _children: [],
    get textContent() { return this._t; },
    set textContent(v) { this._t = String(v); this._inner = escStr(v); this._children = []; },
    get innerHTML() { return this._inner; },
    set innerHTML(v) { this._inner = String(v); },
    appendChild(ch) { this._children.push(ch); return ch; },
    removeChild(ch) { const i = this._children.indexOf(ch); if (i >= 0) this._children.splice(i, 1); return ch; },
    get firstChild() { return this._children.length ? this._children[0] : null; },
    setAttribute(k, v) { this[k] = v; },
    className: '',
    hidden: true,
    style: {},
    classList: { add() {}, remove() {} },
  };
}

const els = {};
global.document = {
  getElementById(id) { return els[id] || (els[id] = makeEl()); },
  createElement() { return makeEl(); },
};
global.window = { devicePixelRatio: 1 };
global.fetch = () => Promise.reject(new Error('stub fetch'));
global.setInterval = () => 0;

const src = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'assets', 'app.js'), 'utf8');
eval(src);

function reset() {
  for (const k of Object.keys(els)) {
    const el = els[k];
    el.textContent = ''; el.innerHTML = ''; el.className = ''; el.hidden = true;
  }
}
function run(snapshot) {
  reset();
  render(snapshot);
}

console.log('case 1: unknown peer count (unavailable)');
run({
  node: { online: true }, chain: { height: 1000, initial_block_download: true },
  network: { connections: 2, inbound: null, outbound: null, average_ping_ms: null, traffic: {} },
  ibd: { session_type: 'cold_ibd', peer_aggregation: { active_download_peers: null, total_blocks_inflight: null, total_ask_queue: null, peer_count: null }, peers: null },
  peers: { available: false, fresh: false, stale: false, count: null, items: null, age_seconds: null, last_error: null },
  history: [], host: {}, freshness: {}, notices: [], errors: [], api: {}, dashboard: {},
});
assert(els.networkSection.hidden === false, 'network section visible (connections known)');
assert(els.peerCount.textContent === '—', `unknown peer count renders — (got ${els.peerCount.textContent})`);
assert(els.netActive.textContent === '—', 'unknown active download peers renders —');
assert(els.netInflight.textContent === '—', 'unknown blocks in flight renders —');
assert(els.netAskqueue.textContent === '—', 'unknown queued requests renders —');
assert(els.peerTableBody.innerHTML.includes('Peer data temporarily unavailable'), 'table shows unavailable message, not "No peers"');
assert(els.peerState.textContent === '', 'no cached marker when unavailable');
assert(els.peerAge.hidden === true, 'no age indicator when unavailable');

console.log('case 2: valid zero peers (getpeerinfo returned [])');
run({
  node: { online: true }, chain: { height: 1000, initial_block_download: true },
  network: { connections: 2, traffic: {} },
  ibd: { session_type: 'cold_ibd', peer_aggregation: { active_download_peers: 0, total_blocks_inflight: 0, total_ask_queue: 0, peer_count: 0 }, peers: [] },
  peers: { available: true, fresh: true, stale: false, count: 0, items: [], age_seconds: 5, last_error: null },
  history: [], host: {}, freshness: {}, notices: [], errors: [], api: {}, dashboard: {},
});
assert(els.peerCount.textContent === '0', `valid zero peer count renders 0 (got ${els.peerCount.textContent})`);
assert(els.peerTableBody.innerHTML.includes('No peers'), 'zero peers shows "No peers"');
assert(els.peerState.textContent === '', 'no cached marker for fresh zero peers');
assert(els.netActive.textContent === '0', 'zero peers -> active download peers 0');

console.log('case 3: fresh peers with rows');
run({
  node: { online: true }, chain: { height: 1000, initial_block_download: true },
  network: { connections: 2, traffic: {} },
  ibd: { session_type: 'cold_ibd', peer_aggregation: { active_download_peers: 2, total_blocks_inflight: 3, total_ask_queue: 4, peer_count: 3 }, peers: [] },
  peers: { available: true, fresh: true, stale: false, count: 3,
           items: [{ addr: '1.2.3.4:8333', blocks_inflight: 2, ask_queue: 3, pingtime: 0.1 }], age_seconds: 5, last_error: null },
  history: [], host: {}, freshness: {}, notices: [], errors: [], api: {}, dashboard: {},
});
assert(els.peerCount.textContent === '3', `fresh count renders 3 (got ${els.peerCount.textContent})`);
assert(els.peerTableBody.innerHTML.includes('1.2.3.4:8333'), 'peer rows retained');
assert(els.peerState.textContent === '', 'no cached marker when fresh');
assert(els.peerAge.hidden === true, 'no age indicator when fresh');

console.log('case 4: stale cached snapshot retains rows');
run({
  node: { online: true }, chain: { height: 1000, initial_block_download: true },
  network: { connections: 2, traffic: {} },
  ibd: { session_type: 'cold_ibd', peer_aggregation: { active_download_peers: 2, total_blocks_inflight: 3, total_ask_queue: 4, peer_count: 3 }, peers: [] },
  peers: { available: true, fresh: false, stale: true, count: 3,
           items: [{ addr: '5.6.7.8:8333', blocks_inflight: 1, ask_queue: 0, pingtime: 0.2 }],
           age_seconds: 134, last_error: 'RPC busy' },
  history: [], host: {}, freshness: {}, notices: [], errors: [], api: {}, dashboard: {},
});
assert(els.peerCount.textContent === '3', `stale count retains 3 (got ${els.peerCount.textContent})`);
assert(els.peerTableBody.innerHTML.includes('5.6.7.8:8333'), 'stale rows retained, not erased');
assert(els.peerState.textContent === ' · cached', `cached marker shown (got ${els.peerState.textContent})`);
assert(els.peerAge.hidden === false, 'age indicator visible when stale');
assert(els.peerAge.textContent.includes('2m 14s old'), `age text present (got ${els.peerAge.textContent})`);
assert(els.netActive.textContent === '2', 'stale active peers retained');

console.log('case 5: release version link');
run({
  node: { online: true }, chain: { height: 1000 },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: { version: 'v1' }, dashboard: { version: '0.3.3' },
});
{
  const v = els.dashVersion;
  assert(v._children.length === 1, 'version renders inside a single anchor');
  const a = v.firstChild;
  assert(a.href === 'https://github.com/rxvnvn/innova-node-dashboard/releases/tag/v0.3.3', `version href correct (got ${a.href})`);
  assert(a.textContent === '0.3.3', `version text is 0.3.3 (got ${a.textContent})`);
  assert(a.target === '_blank', 'target _blank');
  assert(a.rel === 'noopener noreferrer', 'rel noopener noreferrer');
}

console.log('case 6: malformed version degrades to plain text, no link');
run({
  node: { online: true }, chain: { height: 1000 },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: {}, dashboard: { version: 'dev-branch' },
});
assert(els.dashVersion._children.length === 0, 'no anchor for malformed version');
assert(els.dashVersion.textContent === 'dev-branch', `malformed version shown as text (got ${els.dashVersion.textContent})`);

console.log('case 7: missing version renders em dash');
run({
  node: { online: true }, chain: { height: 1000 },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: {}, dashboard: {},
});
assert(els.dashVersion._children.length === 0, 'no anchor for missing version');
assert(els.dashVersion.textContent === '—', `missing version renders — (got ${els.dashVersion.textContent})`);

console.log('case 8: confirmed stopped shows Offline and last-known height');
run({
  node: { online: false }, chain: { height: null, last_known_height: 6812442, initial_block_download: null },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: {}, dashboard: { version: '0.3.3' },
});
assert(els.badge.className === 'badge offline', `stopped -> offline badge (got ${els.badge.className})`);
assert(els.status.textContent === 'Offline', `status text Offline (got ${els.status.textContent})`);
assert(els.height.textContent === '—', 'current height stays unknown while offline');
assert(els.lastKnownHeight.hidden === false, 'last-known height visible while offline');
assert(els.lastKnownHeight.textContent.includes('6 812 442'), `last-known height formatted (got ${els.lastKnownHeight.textContent})`);

console.log('case 9: unknown state shows Checking, no last-known');
run({
  node: { online: null }, chain: { height: null, last_known_height: null },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: {}, dashboard: { version: '0.3.3' },
});
assert(els.badge.className === 'badge unknown', `unknown -> unknown badge (got ${els.badge.className})`);
assert(els.status.textContent === 'Checking', `status text Checking (got ${els.status.textContent})`);
assert(els.lastKnownHeight.hidden === true, 'no last-known height when never observed');

console.log('case 10: online hides last-known height');
run({
  node: { online: true }, chain: { height: 6812442, last_known_height: 6812442 },
  network: {}, ibd: {}, peers: {}, history: [], host: {}, freshness: {},
  notices: [], errors: [], api: {}, dashboard: { version: '0.3.3' },
});
assert(els.lastKnownHeight.hidden === true, 'last-known height hidden while online');
assert(els.height.textContent === '6 812 442', `live height rendered (got ${els.height.textContent})`);

if (failures) {
  console.error(`\n${failures} frontend assertion(s) failed`);
  process.exit(1);
}
console.log('\nall frontend rendering checks passed');
