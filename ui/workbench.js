'use strict';

const $ = id => document.getElementById(id);
if (window.alisupplierDesktop) {
  $('desktop-control').hidden = false;
  $('desktop-control').addEventListener('click', () => window.alisupplierDesktop.openControl());
}
const state = { filter: 'follow', rows: [], selected: null, settingsLoaded: false, offset: 0 };
let listRequest = 0;
let detailRequest = 0;
let toastTimer;

function node(tag, cls = '', text = '') {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text) el.textContent = text;
  return el;
}

function button(text, action, cls = 'button') {
  const el = node('button', cls, text);
  el.type = 'button';
  el.addEventListener('click', async () => {
    try {
      el.disabled = true;
      await action();
    } catch (error) {
      notice(error.message);
    } finally {
      el.disabled = false;
    }
  });
  return el;
}

async function api(path, values) {
  const response = await fetch(path, values === undefined ? undefined : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function notice(text) {
  const inDetail = $('detail').open && $('detail-feedback');
  if (inDetail) {
    inDetail.textContent = text;
  } else {
    $('toast').textContent = text;
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4400);
  }
}

function formatTime(seconds, full = false) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  const time = {hour: '2-digit', minute: '2-digit'};
  if (full || date.toDateString() !== new Date().toDateString()) {
    Object.assign(time, {month: 'numeric', day: 'numeric'});
  }
  return date.toLocaleString('zh-CN', time);
}

function params(values) {
  return '?' + new URLSearchParams(values);
}

function statusFor(row) {
  if (row.running) return ['正在汇总', ''];
  if (row.error) return [row.failures >= 3 ? '汇总失败' : '等待重试', 'warn'];
  if (row.pending || row.forced) return ['有消息待汇总', 'warn'];
  if (row.review || (row.attachments || []).length) return ['需要核对', 'warn'];
  if (row.report_id && row.checked !== row.report_id) return ['有新变化', ''];
  return row.report_id ? ['已汇总', 'neutral'] : ['尚无概要', 'neutral'];
}

function renderList() {
  const list = $('list');
  list.replaceChildren();
  if (!state.rows.length) {
    const empty = {follow: '暂时没有待跟进的概要。', recent: '没有未查看的汇总。', review: '暂时没有需要核对的汇总。', all: '没有匹配的会话。'};
    list.append(node('div', 'empty', empty[state.filter]));
  }
  for (const row of state.rows) {
    const el = button('', () => openDetail(row), 'thread');
    const client = node('span');
    const titleEl = node('strong', 'client', row.peer || row.cid);
    if (row.stage) titleEl.append(node('span', 'stage-pill', row.stage));
    client.append(titleEl, node('small', 'muted', row.user_id));
    const summary = node('span', 'thread-summary');
    summary.append(node('span', 'change', row.changes || row.latest_content || '等待首次汇总'));
    if (row.next_step) summary.append(node('small', 'muted', '建议：' + row.next_step));
    const [label, type] = statusFor(row);
    const status = node('span', 'thread-status');
    status.append(node('span', 'badge ' + type, label), node('small', 'muted', formatTime(row.created_at || row.activity)));
    el.append(client, summary, status);
    list.append(el);
  }
  $('more').hidden = !state.more;
}

async function loadList(append = false) {
  const request = ++listRequest;
  const offset = append ? state.rows.length : 0;
  const data = await api('/api/workbench/list' + params({
    user: $('accounts').value, q: $('search').value.trim(), filter: state.filter, offset,
    count: append ? 100 : Math.min(500, Math.max(100, state.rows.length))
  }));
  if (request !== listRequest) return;
  state.rows = append ? [...state.rows, ...data.rows] : data.rows;
  state.more = data.more;
  renderList();
}

function renderFrida(frida) {
  const btn = $('frida-toggle');
  if (!btn || !frida) return;
  btn.dataset.enabled = frida.enabled ? '1' : '0';
  btn.classList.toggle('primary', Boolean(frida.enabled && frida.state === 'attached'));
  if (!frida.available) {
    btn.textContent = '集成不可用';
    btn.disabled = true;
    btn.title = frida.reason || '未检测到 frida CLI 或 frida_agent.js';
    return;
  }
  btn.disabled = false;
  if (!frida.enabled) {
    btn.textContent = '启用集成';
    btn.title = '点击启用：在阿里卖家点击买家时自动打开对应聊天详情';
  } else if (frida.state === 'attached') {
    btn.textContent = '联动中' + (frida.pid ? ` (${frida.pid})` : '');
    btn.title = '已连接阿里卖家，点击可停用集成';
  } else if (frida.state === 'attaching') {
    btn.textContent = '正在连接…';
  } else if (frida.state === 'error') {
    btn.textContent = '集成异常';
    btn.title = frida.error || 'Frida 联动异常';
  } else {
    btn.textContent = '等待阿里卖家…';
    btn.title = '已启用集成，等待阿里卖家启动';
  }
}

async function status() {
  const data = await api('/api/status');
  const connection = $('connection');
  const labels = {tailing: '采集运行中', no_log: '未找到日志', starting: '采集启动中', stopped: '采集已停止'};
  connection.replaceChildren(node('span'), document.createTextNode(labels[data.state] || '服务在线'));
  connection.classList.toggle('online', data.state === 'tailing');
  renderFrida(data.frida);
}

async function accounts() {
  const rows = await api('/api/users');
  const el = $('accounts');
  const selected = el.value;
  el.replaceChildren(new Option('全部账号', ''), ...rows.map(row => new Option(row.user_id, row.user_id)));
  el.value = [...el.options].some(item => item.value === selected) ? selected : '';
}

function selectFilter(filter) {
  state.filter = filter;
  state.rows = [];
  document.querySelectorAll('[data-filter]').forEach(el => {
    el.classList.toggle('active', el.dataset.filter === filter);
    el.setAttribute('aria-pressed', String(el.dataset.filter === filter));
  });
  loadList().catch(error => notice(error.message));
}

function reportBlock(report, row, latest = true) {
  const el = node('article', 'report');
  el.append(node('div', 'eyebrow', '已汇总到 ' + formatTime(report.covered_at, true) +
    ' · 更新于 ' + formatTime(report.created_at, true)));
  const fields = [['当前概要', 'summary'], ['本轮变化', 'changes'], ['待核对', 'review']];
  for (const [title, key] of fields) {
    if (report[key]) el.append(node('h3', '', title), node('p', '', report[key]));
  }
  if (report.next_step) {
    const box = node('div', 'next-step-box');
    box.append(node('h3', '', '建议跟进'), node('p', '', report.next_step));
    el.append(box);
  }
  if (report.attachments?.length) {
    el.append(node('p', 'attachment-note muted', report.attachments.length + ' 条附件在本次汇总时待查看'));
  }
  const actions = node('div', 'actions');
  const references = report.references?.length ? report.references : report.batch_ids || [];
  if (references.length) {
    actions.append(button('查看本批依据', () => openEvidence(row, references)));
  }
  if (latest && report.next_step && row.handled !== report.id) {
    actions.append(button('标记已处理', async () => {
      await api('/api/workbench/mark', {user: row.user_id, cid: row.cid, report: report.id, field: 'handled'});
      row.handled = report.id;
      await openDetail(row, false);
      await loadList();
      notice('已标记为处理完成');
    }));
  }
  el.append(actions);
  return el;
}

function renderCard(data) {
  const card = node('div');
  const scalarFields = values => {
    const dl = node('dl', 'card-fields');
    for (const [key, val] of Object.entries(values)) {
      if (typeof val === 'string' && val) dl.append(node('dt', '', key), node('dd', '', val));
    }
    return dl;
  };
  card.append(scalarFields(data));
  for (const product of data['商品'] || []) card.append(scalarFields(product));
  for (const attr of data['属性'] || []) {
    const dl = node('dl', 'card-fields');
    dl.append(node('dt', '', attr['名称'] || '属性'), node('dd', '', attr['内容'] || ''));
    card.append(dl);
  }
  return card;
}

function renderMessage(message, row) {
  const el = node('article', 'message ' + (message.direction || ''));
  el.dataset.messageId = message.id;
  const direction = {in: '客户', out: '我方', unknown: '归属待核对'}[message.direction] || '发言方未知';
  el.append(node('div', 'eyebrow', direction + ' · ' + formatTime(message.msg_time || message.captured_at, true)));
  if (message.card && Object.keys(message.card).length) {
    el.append(renderCard(message.card));
  } else {
    el.append(node('p', '', message.content || (message.card ? '卡片待补全' : '附件消息')));
  }
  if (message.kind === 'image' || message.content_type === 'image') {
    if (message.image_url) {
      const link = node('a', '', '');
      link.href = message.image_url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = node('img');
      image.src = message.image_url;
      image.loading = 'lazy';
      image.alt = '查看聊天图片';
      image.addEventListener('error', () => link.replaceChildren(node('span', 'muted', '原图不可用')));
      link.append(image);
      el.append(link);
    }
    const details = node('details');
    details.append(node('summary', '', '补充或纠正图片说明'));
    const input = node('textarea');
    input.placeholder = '例如：铭牌是 A-108，最后一位是 8';
    input.setAttribute('aria-label', '图片说明');
    input.maxLength = 700;
    input.value = message.image_note || '';
    details.append(input, button('保存说明', async () => {
      await api('/api/workbench/image-note', {user: row.user_id, cid: row.cid, message_id: message.id, note: input.value});
      notice('已保存，会纳入下一批或手动汇总。');
    }));
    el.append(details);
  }
  return el;
}

async function openEvidence(row, ids) {
  const list = $('evidence-messages');
  const missing = ids.filter(id => !list.querySelector('[data-message-id="' + id + '"]'));
  const earlier = await Promise.all(missing.map(id => api('/api/workbench/message' + params({
    user: row.user_id, cid: row.cid, id
  })).catch(() => null)));
  if (!list.isConnected) return;
  list.prepend(...earlier.filter(Boolean).map(message => renderMessage(message, row)));
  for (const el of list.querySelectorAll('.message')) {
    el.classList.toggle('active', ids.includes(Number(el.dataset.messageId)));
  }
  list.querySelector('.message.active')?.scrollIntoView({block: 'nearest'});
}

function renderCustomerCard(data, user, cid, row) {
  const c = data.customer || {};
  const stats = data.stats || {};
  const name = c.display_name || row.peer || cid;
  const uid = c.uid || (cid.includes('-') ? cid.split('-')[0] : '');
  const domain = cid.includes('@') ? cid.split('@').pop() : '';

  const card = node('section', 'customer-card');
  const top = node('div', 'customer-top');
  const avatar = node('div', 'customer-avatar');
  if (c.avatar_url && /^https?:\/\//.test(c.avatar_url)) {
    const img = node('img');
    img.src = c.avatar_url;
    img.alt = name;
    img.addEventListener('error', () => { avatar.textContent = (name || '?').slice(0, 1).toUpperCase(); });
    avatar.append(img);
  } else {
    avatar.textContent = (name || '?').slice(0, 1).toUpperCase();
  }
  const titleWrap = node('div');
  titleWrap.append(
    node('div', 'customer-name', name),
    node('div', 'customer-sub', [uid ? '买家UID: ' + uid : '', domain ? '渠道: ' + domain : ''].filter(Boolean).join(' · ') || cid)
  );
  top.append(avatar, titleWrap);
  card.append(top);

  const grid = node('div', 'customer-grid');
  const kv = (targetGrid, label, val) => {
    if (val === null || val === undefined || val === '') return;
    const item = node('div', 'customer-kv');
    item.append(node('span', '', label), node('strong', '', String(val)));
    targetGrid.append(item);
  };
  kv(grid, '累计消息', stats.total_messages ? stats.total_messages + ' 条' : '暂无');
  kv(grid, '最近消息', stats.last_at ? formatTime(stats.last_at, true) : '暂无');
  if (stats.first_at) kv(grid, '首次互动', formatTime(stats.first_at, true));
  if (c.updated_at) kv(grid, '资料同步', formatTime(c.updated_at, true));
  if (c.extra && typeof c.extra === 'object') {
    for (const [k, v] of Object.entries(c.extra)) {
      if (v && typeof v === 'string' && !k.startsWith('_')) kv(grid, k, v);
    }
  }
  card.append(grid);

  const more = node('details', 'customer-more');
  more.append(node('summary', '', '账号与会话标识'));
  const moreGrid = node('div', 'customer-grid');
  kv(moreGrid, '卖家账号', user);
  kv(moreGrid, '会话 CID', cid);
  more.append(moreGrid);
  card.append(more);

  // 意向阶段 + 快捷标签 + 私密备注
  const crm = node('div', 'crm-panel');
  const stageRow = node('div', 'crm-row');
  const stageSel = node('select');
  stageSel.setAttribute('aria-label', '客户阶段');
  const stages = ['', '新询盘', '需求确认', '已报价', '寄样中', '谈判逼单', '已成交', '冷淡'];
  for (const s of stages) stageSel.append(new Option(s || '阶段：未标记', s));
  stageSel.value = c.stage || '';

  const presetTags = ['高意向', '价格敏感', '已加WA', '老客户', '催样中'];
  let activeTags = new Set(Array.isArray(c.tags) ? c.tags : []);
  const noteInput = node('textarea', 'crm-note');
  noteInput.placeholder = '记录客户备注（如：目标价、已加 WhatsApp、样卡单号）… 失焦自动保存';
  noteInput.value = c.note || '';

  const saveMeta = async (silent = false) => {
    try {
      await api('/api/workbench/customer-note', {
        user, cid,
        stage: stageSel.value,
        tags: [...activeTags],
        note: noteInput.value
      });
      if (!silent) notice('已保存客户档案');
      loadList().catch(() => {});
    } catch (error) {
      notice(error.message);
    }
  };
  stageSel.addEventListener('change', () => saveMeta(false));
  noteInput.addEventListener('blur', () => {
    if (noteInput.value !== (c.note || '')) {
      c.note = noteInput.value;
      saveMeta(false);
    }
  });
  stageRow.append(stageSel);
  for (const tag of presetTags) {
    const chip = button(tag, () => {
      if (activeTags.has(tag)) activeTags.delete(tag);
      else activeTags.add(tag);
      chip.classList.toggle('active', activeTags.has(tag));
      saveMeta(true);
    }, 'tag-chip' + (activeTags.has(tag) ? ' active' : ''));
    stageRow.append(chip);
  }
  crm.append(stageRow, noteInput);
  card.append(crm);
  return card;
}

function renderDraftCard(user, cid) {
  const card = node('section', 'draft-card');
  const head = node('div', 'draft-head');
  head.append(node('span', '', 'AI 回复助手'), node('small', 'muted', '生成后一键复制粘贴到阿里卖家'));
  const inputRow = node('div', 'draft-input-row');
  const intent = node('input');
  intent.type = 'text';
  intent.placeholder = '输入中文意图（可留空，如：300米起订，$4.2/米）';
  const list = node('div', 'draft-list');
  const genBtn = button('生成回复', async () => {
    genBtn.disabled = true;
    genBtn.textContent = '生成中…';
    list.replaceChildren();
    try {
      const res = await api('/api/workbench/draft-reply', { user, cid, intent: intent.value.trim() });
      for (const d of res.drafts || []) {
        const item = node('div', 'draft-item');
        const top = node('div', 'draft-item-top');
        const copyBtn = button('复制英文', async () => {
          try {
            await navigator.clipboard.writeText(d.en || '');
            copyBtn.textContent = '已复制 ✓';
            setTimeout(() => { copyBtn.textContent = '复制英文'; }, 1600);
          } catch (_) {
            notice('复制失败，请手动选中复制');
          }
        }, 'button');
        copyBtn.style.padding = '2px 8px';
        copyBtn.style.fontSize = '11px';
        top.append(node('span', '', d.title || '建议回复'), copyBtn);
        item.append(top, node('div', 'draft-item-en', d.en || ''));
        if (d.zh) item.append(node('div', 'draft-item-zh', '中文：' + d.zh));
        list.append(item);
      }
    } catch (error) {
      notice(error.message);
    } finally {
      genBtn.disabled = false;
      genBtn.textContent = '生成回复';
    }
  }, 'button primary');
  intent.addEventListener('keydown', e => { if (e.key === 'Enter') genBtn.click(); });
  inputRow.append(intent, genBtn);
  card.append(head, inputRow, list);
  return card;
}

async function openDetail(row, checked = true) {
  const request = ++detailRequest;
  const scroll = $('detail-scroll');
  const previousScroll = state.selected === row ? scroll.scrollTop : 0;
  state.selected = row;
  const user = row.user_id, cid = row.cid;
  $('detail-title').textContent = row.peer || cid;
  $('detail-account').textContent = user;
  if (!$('detail').open) $('detail').showModal();
  const data = await api('/api/workbench/detail' + params({user, cid}));
  if (request !== detailRequest || !$('detail').open) return;
  if (data.customer?.display_name) {
    $('detail-title').textContent = data.customer.display_name;
    row.peer = data.customer.display_name;
  }
  const customerMeta = [];
  if (data.customer?.uid) customerMeta.push('买家ID: ' + data.customer.uid);
  $('detail-account').textContent = customerMeta.length ? `${user} · ${customerMeta.join(' · ')}` : user;
  const top = node('div', 'actions');
  top.append(button('立即汇总', async () => {
    const result = await api('/api/workbench/run', {user, cid});
    notice(result.queued ? '已提交，生成后会显示可刷新提示。' : '没有新内容，或该会话正在汇总。');
    await loadList();
  }, 'button primary'), button('刷新详情', () => openDetail(row, true)));
  const feedback = node('p', 'muted', row.error || (row.pending ? '有新消息待汇总。' : ''));
  feedback.id = 'detail-feedback';
  feedback.setAttribute('role', 'status');
  const report = data.reports[0];
  scroll.replaceChildren(renderCustomerCard(data, user, cid, row), renderDraftCard(user, cid), top, feedback);
  if (report) {
    scroll.append(reportBlock(report, row));
    if (data.reports.length > 1) {
      const history = node('details');
      history.append(node('summary', '', '最近的历史概要'));
      history.append(...data.reports.slice(1).map(item => reportBlock(item, row, false)));
      scroll.append(history);
    }
  } else {
    scroll.append(node('div', 'empty', '还没有概要。收到客户新消息后会按设置汇总。'));
  }
  scroll.append(node('h3', 'chat-title', '聊天证据'));
  const list = node('div');
  list.id = 'evidence-messages';
  list.append(...data.messages.map(message => renderMessage(message, row)));
  scroll.append(list);
  if (data.more) {
    let before = data.messages[0].id;
    const more = button('加载更早的消息', async () => {
      const batch = await api('/api/workbench/detail' + params({user, cid, before}));
      if (!list.isConnected) return;
      if (batch.messages.length) before = batch.messages[0].id;
      list.prepend(...batch.messages.filter(message => !list.querySelector('[data-message-id="' + message.id + '"]'))
        .map(message => renderMessage(message, row)));
      more.hidden = !batch.more;
    });
    scroll.insertBefore(more, list);
  }
  scroll.scrollTop = previousScroll;
  if (checked && report && row.checked !== report.id) {
    await api('/api/workbench/mark', {user, cid, report: report.id, field: 'checked'}).catch(() => {});
    row.checked = report.id;
    await loadList();
  }
}

const flags = ['automatic', 'ocr', 'vision', 'allow_external_images'];
const numbers = ['quiet_minutes', 'max_minutes', 'token_budget', 'image_budget'];
const selections = ['text_profile', 'image_profile'];
const baseURLs = {
  chat: 'https://api.openai.com/v1', responses: 'https://api.openai.com/v1',
  anthropic: 'https://api.anthropic.com/v1', gemini: 'https://generativelanguage.googleapis.com/v1beta'
};

function formField(parent, label, input) {
  const wrap = node('label', '', label);
  wrap.append(input);
  parent.append(wrap);
}

function profileValues(section) {
  const values = {id: section.dataset.id || undefined};
  for (const el of section.querySelectorAll('[data-key]')) {
    values[el.dataset.key] = el.type === 'number' ? el.valueAsNumber :
      (el.type === 'checkbox' ? el.checked : el.value);
  }
  return values;
}

function refreshSelectors(selected = {}) {
  const profiles = [...$('profiles').children].map(section => profileValues(section)).filter(p => p.id);
  for (const key of selections) {
    const el = $(key);
    const current = selected[key] ?? el.value;
    el.replaceChildren(new Option('未选择', ''), ...profiles.map(p => new Option(p.name || p.model || '未命名', p.id)));
    el.value = profiles.some(p => p.id === current) ? current : '';
  }
}

function profileEditor(profile = {}) {
  const section = node('section', 'profile');
  section.dataset.id = profile.id || crypto.randomUUID().replaceAll('-', '');
  const items = node('div', 'form-grid');
  const top = node('div', 'profile-top');
  const actions = node('div', 'actions');
  const result = node('p', 'profile-status');
  top.append(node('strong', '', profile.name || '新接口'), actions);
  const input = (field, label, type = 'text', value = '', required = false) => {
    const el = node('input');
    el.type = type;
    el.dataset.key = field;
    el.value = value;
    el.required = required;
    formField(items, label, el);
    return el;
  };
  input('name', '名称', 'text', profile.name, true).addEventListener('input', () => refreshSelectors());
  input('model', '模型名', 'text', profile.model, true);
  const protocol = node('select');
  protocol.dataset.key = 'protocol';
  protocol.append(...Object.entries(state.protocols).map(([key, label]) => new Option(label, key)));
  protocol.value = profile.protocol || 'chat';
  formField(items, '协议', protocol);
  const endpoint = input('base_url', '服务地址', 'url', profile.base_url || baseURLs[protocol.value], true);
  protocol.addEventListener('change', () => {
    if (Object.values(baseURLs).includes(endpoint.value)) endpoint.value = baseURLs[protocol.value];
    tokenField.parentElement.hidden = protocol.value !== 'chat';
  });
  const tokenField = node('select');
  tokenField.dataset.key = 'token_field';
  tokenField.append(new Option('max_tokens', 'max_tokens'), new Option('max_completion_tokens', 'max_completion_tokens'));
  tokenField.value = profile.token_field || 'max_tokens';
  formField(items, 'Chat 输出上限参数', tokenField);
  tokenField.parentElement.hidden = protocol.value !== 'chat';
  const key = input('key', 'API 密钥', 'password');
  key.autocomplete = 'off';
  key.placeholder = profile.has_key ? '已保存，留空继续使用' : '填写密钥（如接口需要）';
  if (profile.has_key) {
    const remove = node('input');
    remove.type = 'checkbox';
    remove.dataset.key = 'clear_key';
    formField(items, '清除已保存密钥', remove);
  }
  const timeout = input('timeout', '超时（秒）', 'number', profile.timeout || 45, true);
  timeout.min = 5; timeout.max = 120;
  for (const [label, image] of [['测文字', false], ['测看图', true]]) {
    actions.append(button(label, async () => {
      if (![...items.querySelectorAll('input, select')].every(el => el.reportValidity())) return;
      result.textContent = '正在发送测试样例…';
      try {
        await api('/api/workbench/test', {profile: profileValues(section), image});
        result.textContent = '测试通过';
      } catch (error) {
        result.textContent = error.message;
      }
    }));
  }
  actions.append(button('移除', () => { section.remove(); refreshSelectors(); }, 'button quiet'));
  section.append(top, items, result);
  return section;
}

async function loadSettings(fill = false) {
  const data = await api('/api/workbench/settings');
  state.protocols = data.protocols;
  const settings = data.settings;
  state.textConfigured = Boolean(settings.text_profile);
  $('ocr-status').textContent = data.ocr_available ? 'Tesseract 可用' : '本机未找到 Tesseract';
  $('usage').textContent = '今日文字 Token ' + data.usage.tokens.toLocaleString() + ' · 看图 ' + data.usage.images + ' 次 · 预算填 0 表示不限';
  const notes = [];
  if (!settings.text_profile) notes.push('请到「AI 与设置」添加并选择文字模型，待处理消息已保存在本机。');
  if (!settings.automatic) notes.push('自动汇总已关闭，可以从客户详情手动汇总。');
  if (data.pending) notes.push(data.pending + ' 个会话有待处理消息，可在「全部客户」查看。');
  $('system-note').textContent = notes.join(' ');
  $('system-note').hidden = notes.length === 0;
  if (!fill) return;
  for (const field of flags) $(field).checked = settings[field];
  for (const field of numbers) $(field).value = settings[field];
  $('profiles').replaceChildren(...settings.profiles.map(profileEditor));
  refreshSelectors(settings);
  state.settingsLoaded = true;
  $('settings-fields').disabled = false;
  $('save').disabled = false;
}

$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  const values = {profiles: [...$('profiles').children].map(profileValues)};
  for (const field of flags) values[field] = $(field).checked;
  for (const field of numbers) values[field] = $(field).valueAsNumber;
  for (const field of selections) values[field] = $(field).value;
  $('save').disabled = true;
  try {
    await api('/api/workbench/settings', values);
    await loadSettings(true);
    notice('设置已保存');
  } catch (error) {
    notice(error.message);
  } finally {
    $('save').disabled = false;
  }
});
$('add-profile').addEventListener('click', () => {
  const editor = profileEditor();
  $('profiles').append(editor);
  const pick = $('profiles').children.length === 1 && !$('text_profile').value;
  refreshSelectors(pick ? {text_profile: editor.dataset.id} : {});
  $('profiles').lastElementChild.querySelector('input').focus();
});
for (const el of document.querySelectorAll('[data-screen]')) {
  el.addEventListener('click', async () => {
    const settings = el.dataset.screen === 'settings';
    $('work-screen').hidden = settings;
    $('settings-screen').hidden = !settings;
    document.querySelectorAll('[data-screen]').forEach(tab => tab.classList.toggle('active', tab === el));
    if (settings) {
      try { await loadSettings(!state.settingsLoaded); }
      catch (error) { notice(error.message); }
    } else {
      await loadList().catch(error => notice(error.message));
    }
  });
}
$('close-detail').addEventListener('click', () => $('detail').close());
$('detail').addEventListener('close', () => { state.selected = null; });
$('more').addEventListener('click', () => loadList(true).catch(error => notice(error.message)));
$('accounts').addEventListener('change', () => selectFilter(state.filter));
$('search').addEventListener('input', () => selectFilter(state.filter));
document.querySelectorAll('[data-filter]').forEach(el => el.addEventListener('click', () => selectFilter(el.dataset.filter)));

const fridaBtn = $('frida-toggle');
if (fridaBtn) {
  fridaBtn.addEventListener('click', async () => {
    const next = fridaBtn.dataset.enabled !== '1';
    fridaBtn.disabled = true;
    try {
      const res = await api('/api/workbench/frida', { enabled: next });
      renderFrida(res.frida);
      notice(next ? '已启用阿里卖家联动集成。' : '已停用阿里卖家联动集成。');
    } catch (error) {
      notice(error.message);
    } finally {
      fridaBtn.disabled = false;
    }
  });
}

if (window.alisupplierDesktop?.platform === 'darwin') {
  document.body.classList.add('platform-darwin');
}

if (window.alisupplierDesktop?.setWidgetMode) {
  const widgetBtn = $('widget-toggle');
  const pinBtn = $('pin-toggle');
  const syncWindowMode = mode => {
    if (!mode) return;
    document.body.classList.toggle('widget-mode', Boolean(mode.widget));
    if (widgetBtn) {
      widgetBtn.hidden = false;
      widgetBtn.textContent = mode.widget ? '展开大窗' : '迷你悬浮';
      widgetBtn.classList.toggle('primary', Boolean(mode.widget));
    }
    if (pinBtn) {
      pinBtn.hidden = false;
      pinBtn.textContent = mode.alwaysOnTop ? '已置顶' : '置顶';
      pinBtn.classList.toggle('primary', Boolean(mode.alwaysOnTop));
    }
  };
  window.alisupplierDesktop.getWindowMode?.().then(syncWindowMode).catch(() => {});
  widgetBtn?.addEventListener('click', async () => {
    const next = !document.body.classList.contains('widget-mode');
    syncWindowMode(await window.alisupplierDesktop.setWidgetMode(next));
  });
  pinBtn?.addEventListener('click', async () => {
    const next = !pinBtn.classList.contains('primary');
    syncWindowMode(await window.alisupplierDesktop.setAlwaysOnTop(next));
  });
}

$('today').textContent = new Date().toLocaleDateString('zh-CN', {month: 'long', day: 'numeric', weekday: 'long'});
let refresh;
const stream = new EventSource('/api/stream');
stream.onmessage = event => {
  try {
    const data = JSON.parse(event.data);
    if (data.kind === 'frida_status' && data.frida) {
      renderFrida(data.frida);
      return;
    }
    if (data.kind === 'open_chat' && data.user_id && data.cid) {
      const existing = state.rows.find(r => r.user_id === data.user_id && r.cid === data.cid);
      const targetRow = existing || { user_id: data.user_id, cid: data.cid, peer: data.peer || data.cid };
      if (data.peer) targetRow.peer = data.peer;
      openDetail(targetRow, false);
      loadList().catch(() => {});
      return;
    }
    if (data.kind === 'workbench' || data.kind === 'message') {
      clearTimeout(refresh);
      refresh = setTimeout(() => {
        accounts().then(() => loadList()).catch(() => {});
        loadSettings().catch(() => {});
      }, 450);
      if (state.selected?.cid === (data.cid || data.message?.cid) &&
          state.selected?.user_id === (data.user || data.message?.user_id) && $('detail-feedback')) {
        $('detail-feedback').textContent = '该会话有更新，点「刷新详情」查看。';
      }
    }
  } catch (_) { /* Ignore a malformed event and keep the connection. */ }
};
stream.onerror = () => { $('connection').textContent = '正在重连本机服务'; };
(async () => {
  await Promise.all([status(), accounts(), loadSettings()]).catch(error => notice(error.message));
  if (!state.textConfigured) selectFilter('all');
  else await loadList().catch(error => notice(error.message));
})();
setInterval(() => {
  status().catch(() => {});
  if (!$('work-screen').hidden) accounts().then(() => loadList()).catch(() => {});
  loadSettings().catch(() => {});
}, 12000);
