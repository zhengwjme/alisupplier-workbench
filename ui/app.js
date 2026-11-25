'use strict';

const $ = (id) => document.getElementById(id);
let emptyTpl = '';
const state = {
  status: null,
  users: [],
  convs: [],
  user: null,
  cid: null,
  msgs: [],
  filter: '',
  debug: false,
};

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return '今天';
  const y = new Date(Date.now() - 864e5);
  if (d.toDateString() === y.toDateString()) return '昨天';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

function stateLabel(s) {
  return ({
    starting: '启动中', tailing: '已连接（日志）', no_log: '未找到日志',
    error: '出错', stopped: '已停止',
  })[s] || s || '未知';
}

function renderStatus() {
  const s = state.status || {};
  const dot = $('dot');
  dot.className = 'dot ' + (s.state === 'tailing' ? 'ok' : (s.state === 'starting' ? 'wait' :
    (s.state === 'no_log' || s.state === 'error' ? 'bad' : 'idle')));
  const mb = s.log_size ? (s.log_size / 1048576).toFixed(1) + ' MB' : '';
  $('stateText').textContent = stateLabel(s.state) + (mb ? ' · 日志 ' + mb : '');
  const pidPill = $('pidPill');
  pidPill.hidden = !s.log;
  pidPill.textContent = s.log ? String(s.log).split('/').pop() : '';
  const up = $('userPill');
  up.hidden = !s.log_user;
  up.textContent = s.log_user || '';
  const c = s.counters || {};
  $('counters').innerHTML = [
    ['入', c.messages_in || 0], ['出', c.messages_out || 0],
    ['解析', c.parsed || 0], ['跳过', c.skipped || 0],
  ].map(([k, v]) => `<span class="chip"><i>${k}</i>${v}</span>`).join('');
  const hint = $('hint');
  const text = s.hint || s.last_error || '';
  hint.hidden = !text || s.state === 'tailing';
  hint.textContent = text;
  hint.className = 'hint' + (s.state === 'no_log' || s.state === 'error' ? ' bad' : '');
}

function renderAccounts() {
  const box = $('accounts');
  const items = [{ user_id: null, label: '全部' }].concat(state.users.map((u) => ({
    user_id: u.user_id, label: u.user_id, n: u.n,
  })));
  box.innerHTML = items.map((u) => {
    const active = (state.user || null) === (u.user_id || null);
    const badge = u.n ? `<span class="badge">${u.n}</span>` : '';
    return `<button class="acct${active ? ' active' : ''}" data-user="${u.user_id || ''}">${u.label}${badge}</button>`;
  }).join('');
  box.querySelectorAll('.acct').forEach((b) => {
    b.onclick = () => { state.user = b.dataset.user || null; state.cid = null; renderAccounts(); loadConvs(); renderMessages(); };
  });
}

function convName(c) {
  return c.peer || c.cid || '(无会话ID)';
}

function renderConvs() {
  const q = state.filter.toLowerCase();
  const list = state.convs.filter((c) => {
    if (!q) return true;
    return String(c.cid || '').toLowerCase().includes(q) || String(c.last || '').toLowerCase().includes(q);
  });
  const box = $('convList');
  if (!list.length) {
    box.innerHTML = '<div class="empty-mini">暂无会话</div>';
    return;
  }
  box.innerHTML = list.map((c) => {
    const active = state.cid === c.cid;
    const dir = c.last_direction === 'out' ? 'out' : 'in';
    return `<button class="conv${active ? ' active' : ''}" data-cid="${encodeURIComponent(c.cid || '')}">
      <div class="conv-row1"><span class="conv-name">${esc(convName(c))}</span><span class="conv-time">${fmtTime(c.t)}</span></div>
      <div class="conv-row2"><span class="prev ${dir}">${esc((c.last || '').slice(0, 80))}</span><span class="conv-n">${c.n}</span></div>
    </button>`;
  }).join('');
  box.querySelectorAll('.conv').forEach((b) => {
    b.onclick = () => { state.cid = decodeURIComponent(b.dataset.cid); renderConvs(); loadMessages(); };
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
}

function unesc(s) {
  if (s == null) return '';
  const t = document.createElement('textarea');
  t.innerHTML = String(s);
  return t.value;
}

function cardHtml(c) {
  const p = (c.products || [])[0] || {};
  const title = p.productName || c.title || '';
  const imgUrl = p.productImage || c.productImage || '';
  const moqVal = p.productMOQ || c.moq || '';
  const img = imgUrl ? `<img class="card-img" src="/api/img?u=${encodeURIComponent(imgUrl)}" loading="lazy">` : '';
  const attrs = (c.attributes || []).map((a) => `<div class="card-attr"><span class="k">${esc(unesc(a.attrName))}</span><span class="v">${esc(unesc(a.attrValue))}</span></div>`).join('');
  const price = [p.productUnitPrice, p.productUnit].filter(Boolean).join(' / ') || c.price || '';
  const moq = moqVal ? `MOQ ${esc(moqVal)}` : '';
  const url = (((p.detailAction || {}).actionParams) || {}).url
    || (((c.productAction || {}).actionParams) || {}).url || '';
  return `<div class="card">
    <div class="card-head">询盘</div>
    <div class="card-top">${img}<div class="card-title">${esc(unesc(title))}</div></div>
    <div class="card-meta">${price ? `<span>${esc(unesc(price))}</span>` : ''}${moq ? `<span>${esc(moq)}</span>` : ''}</div>
    ${attrs}
    ${c.content ? `<div class="card-req"><span class="k">详细需求</span><div>${esc(unesc(c.content)).replace(/\n/g, '<br>')}</div></div>` : ''}
    ${Array.isArray(c.items) && c.items.length ? `<div class="card-items">${c.items.slice(0, 4).map((it) => `<div>${esc(unesc(it.title || it.nick || ''))}</div>`).join('')}</div>` : ''}
    <div class="card-head" style="margin:8px 0 0;font-weight:400;color:#86909c">${esc(unesc(c.benefitFirstText || ''))}</div>
    ${c.inquiryID ? `<div class="card-id">ID: ${esc(c.inquiryID)}</div>` : ''}
    ${url ? `<a class="card-link" href="${esc(url)}" target="_blank" rel="noopener">查看详情</a>` : ''}
  </div>`;
}

function renderBody(m) {
  if (m.kind === 'card' && m.payload) {
    try { return cardHtml(JSON.parse(m.payload)); } catch (e) { }
  }
  if (m.kind === 'image' && m.media) {
    return `<img class="msg-img" src="/media?f=${encodeURIComponent(m.media)}" loading="lazy">`;
  }
  return esc(m.content || '').replace(/\n/g, '<br>');
}

function msgBadge(m) {
  if (m.direction === 'out') return '<span class="tag out">我方</span>';
  if (m.direction === 'in') return '<span class="tag in">买家</span>';
  return '<span class="tag">未知</span>';
}

function renderMessages() {
  const box = $('messages');
  if (!state.msgs.length) {
    box.innerHTML = `<div class="empty">${emptyTpl}</div>`;
    return;
  }
  let html = '';
  let lastDay = '';
  for (const m of state.msgs) {
    const ts = m.msg_time || m.captured_at;
    const day = ts ? dayLabel(ts) : '';
    if (day && day !== lastDay) { html += `<div class="day">${day}</div>`; lastDay = day; }
    const side = m.direction === 'out' ? 'right' : 'left';
    html += `<div class="msg ${side}">
      <div class="bubble">
        <div class="meta">${msgBadge(m)}<span class="who">${esc((m.sender || '').split('@')[0].slice(0, 22))}</span><span class="t">${fmtTime(ts)}</span></div>
        <div class="body">${renderBody(m)}</div>
        <div class="src muted">${esc(m.source || '')}${m.cid ? ' · ' + esc(String(m.cid).slice(0, 26)) : ''}</div>
      </div>
    </div>`;
  }
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
}

function updateChatHead() {
  if (state.cid) {
    const cur = state.convs.find((c) => c.cid === state.cid);
    $('chatTitle').textContent = (cur && cur.peer) || state.cid;
    $('chatSub').textContent = `${state.msgs.length} 条 · ${state.user || '全部账号'}`;
  } else {
    $('chatTitle').textContent = '选择左侧会话';
    $('chatSub').textContent = '';
  }
}

async function loadStatus() {
  try {
    state.status = await api('/api/status');
    renderStatus();
  } catch (e) { /* server restarting */ }
}

async function loadUsers() {
  state.users = await api('/api/users');
  renderAccounts();
}

async function loadConvs() {
  const q = state.user ? `?user=${encodeURIComponent(state.user)}` : '';
  state.convs = await api('/api/conversations' + q);
  renderConvs();
}

async function loadMessages() {
  if (!state.cid) { state.msgs = []; renderMessages(); updateChatHead(); return; }
  const q = `?cid=${encodeURIComponent(state.cid)}` + (state.user ? `&user=${encodeURIComponent(state.user)}` : '') + '&limit=500';
  state.msgs = await api('/api/messages' + q);
  renderMessages();
  updateChatHead();
}

function onMessage(m) {
  if (state.cid === m.cid && (!state.user || state.user === m.user_id)) {
    if (!state.msgs.some((x) => x.id === m.id || (x.mid && x.mid === m.mid))) {
      state.msgs.push(m);
      renderMessages();
    }
  }
  loadConvs();
  loadUsers();
  updateChatHead();
}

function connect() {
  const es = new EventSource('/api/stream');
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.kind === 'status') { state.status = d.status; renderStatus(); }
    else if (d.kind === 'message') { onMessage(d.message); }
    else if (d.kind === 'debug' && state.debug) { appendDebug(d.event); }
  };
  es.onerror = () => { es.close(); setTimeout(connect, 2500); };
}

function appendDebug(ev) {
  const pre = $('debugBody');
  let line = `[${new Date().toLocaleTimeString()}] ${ev.t || ev.kind || ''} `;
  if (ev.t === 'sql') line += `${ev.kind} ${ev.table || ''} ${ev.sql || ''}`;
  else if (ev.t === 'onrecv' || ev.t === 'onsend') line += `${ev.n} strings :: ` + (ev.sample || []).join(' | ').slice(0, 240);
  else line += JSON.stringify(ev).slice(0, 300);
  pre.textContent = line + '\n' + pre.textContent;
  const n = pre.textContent.split('\n').length;
  $('debugCount').textContent = `${n} 行`;
}

async function loadDebug() {
  const evs = await api('/api/events?limit=120');
  $('debugBody').textContent = evs.map((e) => {
    const p = e.payload;
    const t = p.t || e.kind;
    if (t === 'unparsed') return `[${fmtTime(e.ts)}] 跳过一行(长度 ${p.len})`;
    return `[${fmtTime(e.ts)}] ${t} ${JSON.stringify(p).slice(0, 300)}`;
  }).join('\n');
  $('debugCount').textContent = `${evs.length} 行`;
}

function wire() {
  $('btnRescan').onclick = async () => {
    await fetch('/api/rescan', { method: 'POST' });
    setTimeout(() => { loadStatus(); loadUsers(); loadConvs(); }, 600);
  };
  $('btnReload').onclick = () => { loadStatus(); loadUsers(); loadConvs(); loadMessages(); };
  $('btnDebug').onclick = () => {
    state.debug = !state.debug;
    $('debugPanel').hidden = !state.debug;
    if (state.debug) loadDebug();
  };
  $('btnDebugRefresh').onclick = loadDebug;
  $('q').oninput = (e) => { state.filter = e.target.value; renderConvs(); };
  setInterval(loadStatus, 5000);
  setInterval(() => { if (state.status && state.status.state === 'tailing') loadConvs(); }, 15000);
}

(async function main() {
  const tpl = $('empty');
  emptyTpl = tpl ? tpl.innerHTML : '';
  const qs = new URLSearchParams(location.search);
  if (qs.get('user')) state.user = qs.get('user');
  if (qs.get('cid')) state.cid = qs.get('cid');
  wire();
  await loadStatus();
  try { await loadUsers(); await loadConvs(); } catch (e) { }
  if (state.cid) { renderConvs(); await loadMessages(); }
  connect();
})();
