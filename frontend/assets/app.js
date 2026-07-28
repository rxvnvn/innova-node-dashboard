const API = '/api/v1/status', INTERVAL = 5000;
const $ = id => document.getElementById(id);

function text(id, v, f = '—') {
  $(id).textContent = v === null || v === undefined || v === '' ? f : String(v);
}
function integer(v) {
  if (v === null || v === undefined) return '—';
  return new Intl.NumberFormat('ru-RU', { useGrouping: true, maximumFractionDigits: 0 }).format(Number(v)).replace(/\u00a0|\u202f/g, ' ');
}
function decimal(v) {
  if (v === null || v === undefined) return '—';
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(Number(v));
}
function date(v) {
  if (!v) return 'unknown';
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? 'unknown' : d.toLocaleString();
}
function duration(v) {
  if (v === null || v === undefined) return 'unknown';
  let s = Math.max(0, Number(v)), d = Math.floor(s / 86400);
  s %= 86400; let h = Math.floor(s / 3600); s %= 3600;
  let m = Math.floor(s / 60), r = Math.floor(s % 60), p = [];
  if (d) p.push(`${d}d`); if (h || d) p.push(`${h}h`);
  if (m || h || d) p.push(`${m}m`); p.push(`${r}s`);
  return p.join(' ');
}
function bytesHuman(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (isNaN(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, s = n;
  while (s >= 1024 && i < units.length - 1) { s /= 1024; i++; }
  return `${i === 0 ? s : s.toFixed(1)} ${units[i]}`;
}
function badge(online) {
  $('badge').className = 'badge ' + (online === true ? 'online' : online === false ? 'offline' : 'unknown');
  text('status', online === true ? 'Online' : online === false ? 'No data' : 'Checking');
}
function sourceLabel(source) {
  return source === 'debug_log' ? 'Live · Debug log' : source === 'rpc' ? 'Live · RPC' : 'Source unknown';
}
function ageLabel(v) { return v == null ? 'never' : v < 5 ? 'just now' : `${duration(v)} ago`; }

function formatBpm(v) {
  if (v === null || v === undefined) return '—';
  return decimal(v);
}
function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return ['Calculating…', ''];
  if (seconds < 60) return [`${Math.round(seconds)}s`, ''];
  if (seconds < 3600) return [`${Math.round(seconds / 60)}m`, ''];
  if (seconds < 86400) return [`${(seconds / 3600).toFixed(1)}h`, ''];
  return [`${(seconds / 86400).toFixed(1)}d`, ''];
}

const STATE_CLASS = { healthy: 'synced', slow: 'slow', stalled: 'stalled', synced: 'synced', rpc_unavailable: 'stalled', unknown: 'unknown' };
const STATE_LABEL = { healthy: 'Healthy', slow: 'Slow', stalled: 'Stalled', synced: 'Synced', rpc_unavailable: 'RPC unavailable', unknown: 'Unknown' };

function drawHeightGraph(canvas, history) {
  if (!canvas || !history || history.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.offsetWidth * (window.devicePixelRatio || 1);
  const H = canvas.height = 120 * (window.devicePixelRatio || 1);
  ctx.clearRect(0, 0, W, H);
  const heights = history.map(h => h.height);
  const minH = Math.min(...heights), maxH = Math.max(...heights);
  const range = maxH - minH || 1;
  const pad = 8, gw = W - pad * 2, gh = H - pad * 2;
  ctx.strokeStyle = 'rgba(66,134,255,0.3)';
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const y = pad + gh * i / 3;
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - pad, y); ctx.stroke();
  }
  ctx.strokeStyle = '#4286ff';
  ctx.lineWidth = 2 * (window.devicePixelRatio || 1);
  ctx.lineJoin = 'round';
  ctx.beginPath();
  for (let i = 0; i < history.length; i++) {
    const x = pad + (i / (history.length - 1)) * gw;
    const y = pad + gh - ((history[i].height - minH) / range) * gh;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = 'rgba(66,134,255,0.08)';
  ctx.lineTo(pad + gw, pad + gh);
  ctx.lineTo(pad, pad + gh);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = '#8f9bae';
  ctx.font = `${10 * (window.devicePixelRatio || 1)}px system-ui`;
  ctx.fillText(integer(maxH), pad + 4, pad + 12 * (window.devicePixelRatio || 1));
  ctx.fillText(integer(minH), pad + 4, pad + gh - 2);
}

function renderPeerTable(peers) {
  const tbody = $('peerTableBody');
  if (!peers || !peers.length) { tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8f9bae">No peers</td></tr>'; return; }
  const sorted = [...peers].sort((a, b) => (b.blocks_inflight || 0) - (a.blocks_inflight || 0));
  tbody.innerHTML = sorted.map(p => `<tr>
    <td>${esc(p.addr || '—')}</td>
    <td>${integer(p.starting_height)}</td>
    <td>${p.blocks_inflight || 0}</td>
    <td>${p.ask_queue || 0}</td>
    <td>${bytesHuman(p.bytes_received)}</td>
    <td>${bytesHuman(p.bytes_sent)}</td>
    <td>${p.pingtime != null ? (p.pingtime * 1000).toFixed(1) + ' ms' : '—'}</td>
  </tr>`).join('');
}
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

let prevHeight = null;

function render(s) {
  const n = s.node || {}, c = s.chain || {}, w = s.network || {}, f = s.freshness || {};
  const ibd = s.ibd || {}, h = s.host || {}, hist = s.history || [];
  const peers = ibd.peers || [];

  badge(n.online);
  text('height', integer(c.height));
  text('heightSource', sourceLabel(c.height_source));
  text('started', date(n.started_at));
  text('uptime', duration(n.uptime_seconds));
  text('connections', integer(w.connections));
  text('inbound', integer(w.inbound));
  text('outbound', integer(w.outbound));
  text('ping', w.average_ping_ms == null ? '—' : `${decimal(w.average_ping_ms)} ms`);
  text('received', bytesHuman(w.bytes_received));
  text('sent', bytesHuman(w.bytes_sent));
  text('version', n.version);
  text('build', n.build_commit);
  text('api', s.api?.version);
  text('dashVersion', s.dashboard?.version);

  text('ibd', c.initial_block_download === true ? 'IBD — synchronization in progress' : c.initial_block_download === false ? 'Synchronization complete' : c.height != null ? 'Node active · RPC sync state cached' : 'Sync status unknown');
  text('updated', `Dashboard updated: ${date(s.generated_at)}`);
  text('rpcFreshness', `Node info: ${ageLabel(f.getinfo_age_seconds)} · Peers: ${ageLabel(f.getpeerinfo_age_seconds)}`);

  // IBD section
  const ibdVisible = ibd.session_type != null && ibd.session_type !== 'synced';
  $('ibdSection').hidden = !ibdVisible;
  if (ibdVisible) {
    const ss = ibd.sync_state || 'unknown';
    $('syncState').className = 'sync-state ' + (STATE_CLASS[ss] || 'unknown');
    text('syncState', STATE_LABEL[ss] || 'Unknown');
    if (c.height != null && ibd.estimated_tip != null) {
      const pct = ibd.estimated_tip > 0 ? Math.min(100, (c.height / ibd.estimated_tip) * 100) : 0;
      text('ibdProgress', `${integer(c.height)} / ${integer(ibd.estimated_tip)}`);
      text('ibdPercent', `${pct.toFixed(1)}%`);
      $('ibdBarFill').style.width = pct + '%';
    } else {
      text('ibdProgress', `${integer(c.height)} / —`);
      text('ibdPercent', '—');
      $('ibdBarFill').style.width = '0%';
    }
    const stype = ibd.session_type;
    text('sessionType', stype === 'cold_ibd' ? 'Cold IBD' : stype === 'resume_ibd' ? `Resume IBD — start height: ${integer(ibd.session_start_height)}` : stype);
    text('estTip', integer(ibd.estimated_tip));
    text('ibdUptime', duration(n.uptime_seconds));
    text('lastAdvance', ibd.last_height_advance_ago_seconds != null ? ageLabel(ibd.last_height_advance_ago_seconds) : '—');
  }

  // Rate section
  const rateVisible = ibd.session_type != null && ibd.session_type !== 'synced';
  $('rateSection').hidden = !rateVisible;
  if (rateVisible) {
    text('rateCurrent', formatBpm(ibd.current_rate_bpm));
    text('rate5min', formatBpm(ibd.rate_5min_bpm));
    text('rateSession', formatBpm(ibd.session_rate_bpm));
    const [etaStr, etaUnit] = formatEta(ibd.eta_seconds);
    text('etaVal', etaStr);
    text('etaUnit', etaUnit);
  }

  // Network section
  const agg = ibd.peer_aggregation || {};
  const netVisible = agg.peer_count != null;
  $('networkSection').hidden = !netVisible;
  if (netVisible) {
    text('netConnections', integer(w.connections));
    text('netActive', integer(agg.active_download_peers));
    text('netInflight', integer(agg.total_blocks_inflight));
    text('netAskqueue', integer(agg.total_ask_queue));
    text('netReceived', bytesHuman(w.bytes_received));
    text('netSent', bytesHuman(w.bytes_sent));
    text('peerCount', integer(agg.peer_count));
    renderPeerTable(peers);
  }

  // Height graph
  const graphVisible = hist.length >= 2;
  $('heightGraphSection').hidden = !graphVisible;
  if (graphVisible) drawHeightGraph($('heightGraph'), hist);

  // Host section
  const hostVisible = h.cpu_count != null || h.total_ram_bytes != null;
  $('hostSection').hidden = !hostVisible;
  if (hostVisible) {
    text('cpuCount', h.cpu_count != null ? `${h.cpu_count} cores` : '—');
    text('totalRam', bytesHuman(h.total_ram_bytes));
  }

  // Existing details section: repurpose pid row
  const detailsEl = $('hostSection');
  if (!detailsEl.hidden) {
    // pid row was in original, now removed from HTML
  }

  const notices = Array.isArray(s.notices) ? s.notices.filter(Boolean) : [];
  $('notices').hidden = !notices.length;
  if (notices.length) text('noticeText', notices.join('\n'));
  const errors = Array.isArray(s.errors) ? s.errors.filter(Boolean) : [];
  $('errors').hidden = !errors.length;
  if (errors.length) text('errorText', errors.join('\n'));
}

async function load() {
  try {
    const r = await fetch(API, { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    render(await r.json());
  } catch (e) {
    badge(false);
    $('errors').hidden = false;
    text('errorText', `Unable to retrieve dashboard status: ${e}`);
  }
}
load();
setInterval(load, INTERVAL);
