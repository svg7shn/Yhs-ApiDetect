(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('channel-page')) return;
  const API = '/api/channel';
  const families = {claude: 'Anthropic Claude', gpt: 'OpenAI', gemini: 'Google Gemini'};
  const state = {models: [], selected: new Set(), targets: [], sources: [], connected: false, connecting: false, starting: false, running: false, uncertain: false, family: 'claude', generation: 0, job: null, polling: false, timer: null, recordsGeneration: 0, detailGeneration: 0};
  const locked = () => state.starting || state.running || state.uncertain;
  const keyOf = m => JSON.stringify([m.family, m.id]);
  function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); if (cls) n.className = cls; return n; }
  function button(text, action, cls) { const n = el('button', text, cls); n.type = 'button'; n.addEventListener('click', action); return n; }
  function message(id, text, error = false) { const n = $(id); n.textContent = text; n.classList.toggle('ch-error', error); }
  // Never persist credentials or interpolate them into URLs, HTML, or diagnostics.
  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(API + path, {method: body ? 'POST' : 'GET', cache: 'no-store', credentials: 'same-origin', headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, signal: controller.signal});
      let data;
      try { data = await response.json(); } catch (_) { const e = new Error('服务响应格式异常，请稍后重试。'); e.uncertain = true; throw e; }
      if (!response.ok || data.ok === false) {
        const code = data.error && data.error.code;
        const errors = {auth_failed:'密钥错误：上游鉴权失败，请检查 API Key。', invalid_api_key:'密钥错误：请检查 API Key。', unauthorized:'密钥错误：上游鉴权失败。', unreachable:'地址未连接：请核对域名、端口与网络。', connection_failed:'地址未连接：请核对地址与网络。', rate_limited:'请求受限，请稍后重试。', insufficient_balance:'渠道余额不足，请充值后重试。', forbidden:'权限不足：此 Key 无权访问。'};
        const fallback = response.status === 401 ? '上游鉴权失败，请检查密钥。' : response.status === 403 ? '权限不足，请检查渠道授权。' : response.status === 429 ? '请求受限或配额不足，请稍后重试。' : '请求失败，请检查地址、权限或服务状态后重试。';
        const e = new Error(errors[code] || fallback); e.uncertain = response.status >= 500; throw e;
      }
      return data;
    } catch (error) {
      if (error instanceof TypeError || error.name === 'AbortError') { const e = new Error('网络连接中断或请求超时。'); e.uncertain = true; throw e; }
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function syncControls() {
    $('ch-url').disabled = locked(); $('ch-key').disabled = locked(); $('ch-wire').disabled = locked();
    $('ch-connect').disabled = locked() || state.connecting;
    $('ch-connect').textContent = state.connecting ? '正在验证并拉取…' : '验证连接并拉取模型';
    $('ch-all').disabled = locked() || !state.connected || !state.models.length;
    $('ch-all').checked = state.models.length > 0 && state.selected.size === state.models.length;
    $('ch-all').indeterminate = state.selected.size > 0 && state.selected.size < state.models.length;
    $('ch-configure').disabled = locked() || !state.connected || !state.selected.size;
    $('ch-models').querySelectorAll('input').forEach(n => { n.disabled = locked(); });
    $('ch-parameter-fields').disabled = locked();
    $('ch-config-close').disabled = state.starting;
    $('ch-source-retry').disabled = locked();
    updateStart();
  }
  function invalidate() {
    state.generation++; state.connected = false; state.connecting = false; state.models = []; state.selected.clear(); state.targets = [];
    $('ch-connection').textContent = '尚未验证'; message('ch-connect-status', '连接信息已变更，请重新验证。');
    renderModels(); syncControls();
  }
  function renderModels() {
    const root = $('ch-models'); root.replaceChildren();
    message('ch-model-count', state.connected ? '已拉取 ' + state.models.length + ' 个对话模型 · 已选择 ' + state.selected.size + ' 个' : '请先验证连接并拉取模型。');
    if (!state.models.length) { root.append(el('p', state.connected ? '未找到支持的三族对话模型，可更换渠道后重新验证。' : '连接成功后，模型将在这里按厂商分组。', 'ch-empty')); return; }
    for (const [family, label] of Object.entries(families)) {
      const models = state.models.filter(m => m.family === family);
      const group = el('section', undefined, 'ch-group'); const head = el('div', undefined, 'ch-row ch-between');
      head.append(el('h3', label + ' · ' + models.length));
      const allLabel = el('label', undefined, 'ch-check'); const all = el('input'); all.type = 'checkbox'; all.disabled = locked() || !models.length;
      all.checked = models.length > 0 && models.every(m => state.selected.has(keyOf(m))); all.indeterminate = !all.checked && models.some(m => state.selected.has(keyOf(m)));
      all.addEventListener('change', () => { models.forEach(m => all.checked ? state.selected.add(keyOf(m)) : state.selected.delete(keyOf(m))); renderModels(); syncControls(); });
      allLabel.append(all, document.createTextNode('全选本族')); head.append(allLabel); group.append(head);
      const chips = el('div', undefined, 'ch-chips');
      for (const model of models) { const labelEl = el('label', undefined, 'ch-chip'); const check = el('input'); check.type = 'checkbox'; check.checked = state.selected.has(keyOf(model)); check.disabled = locked();
        check.addEventListener('change', () => { check.checked ? state.selected.add(keyOf(model)) : state.selected.delete(keyOf(model)); all.checked = models.every(m => state.selected.has(keyOf(m))); all.indeterminate = !all.checked && models.some(m => state.selected.has(keyOf(m))); message('ch-model-count', '已拉取 ' + state.models.length + ' 个对话模型 · 已选择 ' + state.selected.size + ' 个'); syncControls(); });
        labelEl.append(check, el('span', model.id)); chips.append(labelEl); }
      if (!models.length) chips.append(el('span', '此渠道未返回本族模型。', 'ch-hint'));
      group.append(chips); root.append(group);
    }
  }
  $('ch-url').addEventListener('input', invalidate); $('ch-key').addEventListener('input', invalidate); $('ch-wire').addEventListener('change', invalidate);
  $('ch-connect-form').addEventListener('submit', async event => {
    event.preventDefault(); if (locked() || state.connecting) return;
    if (!$('ch-connect-form').reportValidity()) return;
    const url = $('ch-url').value.trim(); const key = $('ch-key').value.trim();
    try { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error(); } catch (_) { message('ch-connect-status', '请输入 HTTP(S) 地址，不要包含账号、密钥、查询参数或片段。', true); return; }
    if (!key) { message('ch-connect-status', '请输入 API Key。', true); return; }
    invalidate(); const generation = state.generation; state.connecting = true; syncControls(); message('ch-connect-status', '正在验证连接并获取模型…');
    try { const data = await request('/models', {base_url: url, api_key: key}); if (generation !== state.generation) return;
      if (!data.ok || !Array.isArray(data.models)) throw new Error('模型列表格式异常，请重试。');
      const seen = new Set(); state.models = data.models.filter(m => typeof m.id === 'string' && Object.hasOwn(families, m.family) && ($('ch-wire').value === 'gpt' || m.family === $('ch-wire').value) && !seen.has(keyOf(m)) && seen.add(keyOf(m)));
      state.connected = true; $('ch-connection').textContent = '连接已验证'; message('ch-connect-status', '连接成功，已拉取模型。默认未勾选，请选择本次检测项。'); renderModels();
    } catch (e) { if (generation === state.generation) { $('ch-connection').textContent = '连接失败'; message('ch-connect-status', e.message, true); } }
    finally { if (generation === state.generation) { state.connecting = false; syncControls(); } }
  });
  $('ch-all').addEventListener('change', () => { state.models.forEach(m => $('ch-all').checked ? state.selected.add(keyOf(m)) : state.selected.delete(keyOf(m))); renderModels(); syncControls(); });
  function sourceSelect(family, value, onChange) { const select = el('select'); select.append(new Option('请选择声称来源', '')); state.sources.filter(s => s.family === family).forEach(s => select.append(new Option(s.label, s.id))); select.value = value; if (onChange) select.addEventListener('change', () => onChange(select.value)); return select; }
  function validTargets() { return state.targets.length > 0 && state.targets.every(t => state.sources.some(s => s.id === t.claimed_source && s.family === t.family)); }
  function updateStart() { $('ch-start').disabled = locked() || !state.connected || !validTargets(); $('ch-start').textContent = state.starting ? '正在启动…' : '开始立即检测'; $('ch-target-count').textContent = '共 ' + state.targets.length + ' 个模型 · 已设来源 ' + state.targets.filter(t => t.claimed_source).length + ' 个'; }
  async function loadSources() { message('ch-source-status', '正在加载来源选项…'); $('ch-source-retry').hidden = true;
    try { const data = await request('/sources'); if (!Array.isArray(data.sources)) throw new Error('来源列表格式异常。'); state.sources = data.sources.filter(s => typeof s.id === 'string' && typeof s.label === 'string' && Object.hasOwn(families, s.family)); message('ch-source-status', state.sources.length ? '' : '暂无可用来源，无法开始检测。', !state.sources.length); $('ch-source-retry').hidden = state.sources.length > 0; renderParameters(); }
    catch (e) { message('ch-source-status', e.message, true); $('ch-source-retry').hidden = false; }
  }
  function renderTabs() { const tabs = $('ch-tabs'); tabs.replaceChildren(); Object.entries(families).forEach(([family, label], index) => { const tab = button(label + ' · ' + state.targets.filter(t => t.family === family).length, () => { state.family = family; renderParameters(); $('ch-tab-' + family).focus(); }); tab.id = 'ch-tab-' + family; tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(state.family === family)); tab.setAttribute('aria-controls', 'ch-family-panel'); tab.tabIndex = state.family === family ? 0 : -1; tab.disabled = state.starting;
      tab.addEventListener('keydown', event => { const keys = Object.keys(families); let next; if (event.key === 'ArrowRight') next = (index + 1) % keys.length; if (event.key === 'ArrowLeft') next = (index + keys.length - 1) % keys.length; if (event.key === 'Home') next = 0; if (event.key === 'End') next = keys.length - 1; if (next !== undefined) { event.preventDefault(); state.family = keys[next]; renderParameters(); $('ch-tab-' + state.family).focus(); } }); tabs.append(tab); }); $('ch-family-panel').setAttribute('aria-labelledby', 'ch-tab-' + state.family); }
  function renderParameters() {
    renderTabs(); const root = $('ch-targets'); root.replaceChildren();
    const bulk = $('ch-bulk-source'); const options = sourceSelect(state.family, ''); bulk.replaceChildren(...options.childNodes); bulk.value = '';
    const targets = state.targets.filter(t => t.family === state.family);
    if (!targets.length) root.append(el('p', '本族暂无检测项，可从已拉取的模型中添加。', 'ch-empty'));
    targets.forEach(target => { const row = el('div', undefined, 'ch-target'); const modelLabel = el('label', '模型'); const model = el('select'); model.setAttribute('aria-label', '检测模型 ' + target.model);
      state.models.filter(m => m.family === target.family).forEach(m => { const option = new Option(m.id, m.id); option.disabled = m.id !== target.model && state.targets.some(t => t.family === target.family && t.model === m.id); model.append(option); }); model.value = target.model;
      model.addEventListener('change', () => { target.model = model.value; renderParameters(); }); modelLabel.append(model);
      const sourceLabel = el('label', '声称来源'); const source = sourceSelect(target.family, target.claimed_source, value => { target.claimed_source = value; updateStart(); }); source.setAttribute('aria-label', target.model + ' 的声称来源'); sourceLabel.append(source);
      const remove = button('移除', () => { state.targets = state.targets.filter(t => t !== target); renderParameters(); $('ch-add-model').focus(); }, 'ch-remove'); remove.setAttribute('aria-label', '移除 ' + target.model);
      row.append(modelLabel, sourceLabel, remove); root.append(row);
    });
    $('ch-add-model').disabled = !state.models.some(m => m.family === state.family && !state.targets.some(t => t.family === m.family && t.model === m.id)); syncControls();
  }
  $('ch-bulk-source').addEventListener('change', event => { state.targets.filter(t => t.family === state.family).forEach(t => { t.claimed_source = event.target.value; }); renderParameters(); message('ch-source-status', '已更新 ' + families[state.family] + ' 检测项来源，其他族未变更。'); });
  $('ch-add-model').addEventListener('click', () => { const m = state.models.find(m => m.family === state.family && !state.targets.some(t => t.family === m.family && t.model === m.id)); if (m) { state.targets.push({model:m.id, family:m.family, claimed_source:'', wire_family:$('ch-wire').value}); renderParameters(); const selects = $('ch-targets').querySelectorAll('select'); if (selects.length) selects[selects.length - 2].focus(); } });
  $('ch-configure').addEventListener('click', () => { if (locked() || !state.connected) return; state.targets = state.models.filter(m => state.selected.has(keyOf(m))).map(m => ({model:m.id, family:m.family, claimed_source:'', wire_family:$('ch-wire').value})); state.family = state.targets[0]?.family || 'claude'; $('ch-dialog-url').textContent = $('ch-url').value.trim(); message('ch-start-status', ''); renderParameters(); $('ch-parameters').showModal(); if (!state.sources.length) loadSources(); });
  $('ch-config-close').addEventListener('click', () => $('ch-parameters').close());
  $('ch-parameters').addEventListener('cancel', event => { if (state.starting) event.preventDefault(); });
  $('ch-source-retry').addEventListener('click', loadSources);
  $('ch-start').addEventListener('click', async () => {
    if (locked() || !state.connected || !validTargets()) return;
    state.starting = true; syncControls(); renderTabs(); message('ch-start-status', '正在启动检测，请勿重复提交…');
    try { const data = await request('/run', {base_url:$('ch-url').value.trim(), api_key:$('ch-key').value.trim(), targets:state.targets.map(t => ({...t})), timeout:30});
      if (!data.ok || !data.job_id) { const e = new Error('服务未返回任务编号。'); e.uncertain = true; throw e; }
      state.job = String(data.job_id); state.running = true; $('ch-parameters').close(); $('ch-progress-section').hidden = false; $('ch-job-records').replaceChildren(); $('ch-progress').value = 0; message('ch-job-status', '任务已启动，正在获取进度…'); poll();
    } catch (e) {
      if (e.uncertain) { state.uncertain = true; message('ch-start-status', '启动响应丢失，任务可能已创建。为避免重复计费，本页禁止再次启动；请关闭弹窗并刷新检测记录确认。', true); }
      else message('ch-start-status', e.message, true);
    } finally { state.starting = false; syncControls(); renderTabs(); }
  });
  async function poll() {
    if (!state.job || !state.running || state.polling) return; state.polling = true; $('ch-poll-retry').hidden = true;
    try { const job = await request('/jobs/' + encodeURIComponent(state.job)); if (!['running','completed','failed'].includes(job.status)) throw new Error('进度响应格式异常。');
      const total = Math.max(1, Number(job.total) || 1); const completed = Math.max(0, Math.min(total, Number(job.completed) || 0)); $('ch-progress').max = total; $('ch-progress').value = completed;
      const current = typeof job.current === 'string' ? job.current : (job.current && job.current.model) || '';
      message('ch-job-status', (job.status === 'running' ? '正在检测' : job.status === 'completed' ? '检测已结束' : '任务失败，已完成结果仍可查看') + ' · 已完成 ' + completed + ' / ' + (job.total || 0) + (current ? ' · 当前模型：' + current : ''), job.status === 'failed');
      renderRecords($('ch-job-records'), Array.isArray(job.records) ? job.records : []);
      if (job.status === 'running') state.timer = setTimeout(poll, 1500); else { state.running = false; syncControls(); loadRecords(); }
    } catch (e) { message('ch-job-status', e.message + ' 获取进度已暂停；重试只查询当前任务，不会重新执行检测。', true); $('ch-poll-retry').hidden = false; }
    finally { state.polling = false; }
  }
  $('ch-poll-retry').addEventListener('click', poll);
  const verdicts = {authentic:'来源一致', suspect:'疑似异常', inconclusive:'证据不足', error:'检测错误'};
  const CATEGORY_LABELS = {signature:'签名形态', residue:'跨族残留', error:'错误族', filter:'内容过滤', protocol:'协议', capability:'能力', cache:'缓存', param:'参数校验', behavior:'行为', header:'响应头', gateway:'网关指纹'};
  const STATUS_LABELS = {pass:'通过', fail:'未通过', unknown:'信息', error:'检查错误'};
  const PROTOCOL_CATEGORIES = new Set(['signature','residue','error','filter','protocol','capability','cache','param','header']);
  // 证据级别：有协议/残留类证据即为协议级，仅有行为类证据时为行为级。
  function confidenceLabel(record) { const cats = new Set((Array.isArray(record.evidence) ? record.evidence : []).filter(e => e.status === 'pass' || e.status === 'fail').map(e => e.category)); if ([...cats].some(c => PROTOCOL_CATEGORIES.has(c))) return '协议级'; return cats.has('behavior') ? '行为级' : '无'; }
  // 五列明细表；旧记录缺少 category / weight / id 时显示占位符。
  function evidenceTable(evidence) { const table = el('table', undefined, 'ch-table'); const head = el('thead'); const headRow = el('tr'); ['信号','类别','结果','权重','说明'].forEach(h => headRow.append(el('th', h))); head.append(headRow); table.append(head); const body = el('tbody'); evidence.forEach(item => { const weight = typeof item.weight === 'number' ? item.weight : null; const cell = el('td'); cell.append(el('div', item.name || '检查项', 'ch-sig-name')); if (item.id && item.id !== item.name) cell.append(el('div', item.id, 'ch-sig-id')); const result = el('td'); const state = weight === 0 ? 'unknown' : (item.status || 'unknown'); result.append(el('span', weight === 0 ? '信息' : (STATUS_LABELS[item.status] || '信息'), 'ch-badge ch-' + state)); const row = el('tr'); row.append(cell, el('td', CATEGORY_LABELS[item.category] || item.category || '—', 'ch-cat'), result, el('td', weight === null ? '—' : String(weight), 'ch-weight'), el('td', typeof item.detail === 'string' ? item.detail : '—', 'ch-detail')); body.append(row); }); table.append(body); return table; }
  function verdict(record) { return record.verdict === 'authentic' && (!record.claimed_source || /unknown|未知/i.test(record.claimed_source)) ? 'inconclusive' : Object.hasOwn(verdicts, record.verdict) ? record.verdict : 'inconclusive'; }
  function score(record) { return typeof record.score === 'number' && Number.isFinite(record.score) ? String(record.score) + '%' : '—'; }
  // 颜色只表达显示等级，不参与来源判定；按显示精度分档以免数字与颜色矛盾。
  function metricChip(label, value, digits = 0, suffix = '') {
    const valid = typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
    const shown = valid ? Number(value.toFixed(digits)) : null;
    const tone = shown === null ? 'neutral' : shown >= 90 ? 'excellent' : shown >= 75 ? 'good' : shown >= 60 ? 'fair' : 'poor';
    const chip = el('span', label + ' ' + (shown === null ? '—' : shown.toFixed(digits) + suffix), 'ch-meta ch-grade-' + tone);
    chip.title = shown === null ? '暂无有效数据' : '等级：' + ({excellent:'优秀',good:'良好',fair:'及格',poor:'偏低'}[tone]) + '；90及以上优秀，75及以上良好，60及以上及格';
    return chip;
  }
  function evidenceChip(record) {
    const label = confidenceLabel(record);
    const chip = el('span', '证据 ' + label, 'ch-meta ch-grade-' + ({'协议级':'good','行为级':'fair','无':'neutral'}[label]));
    chip.title = '证据层级，不代表检查通过或来源已证实';
    return chip;
  }
  function renderRecords(root, records) {
    root.replaceChildren(); records.forEach(record => { const article = el('article', undefined, 'ch-record'); article.dataset.deleteUrl = '/api/channel/records/' + encodeURIComponent(record.id); const head = el('div', undefined, 'ch-row ch-between'); const text = el('div'); const result = verdict(record); text.append(el('span', verdicts[result], 'ch-badge ch-' + result), el('strong', record.model || '未命名模型')); head.append(text, button('详情', () => openDetail(record.id))); article.append(head, el('p', (record.base_url || '—') + ' · ' + (record.source_label || '未知来源'), 'ch-muted'), el('p', '加权通过率 ' + score(record) + ' · 耗时 ' + (typeof record.duration_ms === 'number' ? (record.duration_ms / 1000).toFixed(1) + ' s' : '—') + ' · ' + (record.created_at || '时间未知'), 'ch-hint')); const chips = el('div', undefined, 'ch-meta-row'); chips.append(evidenceChip(record), metricChip('得分', record.score), metricChip('能力', typeof record.capability_accuracy === 'number' ? record.capability_accuracy * 100 : null, 1, '%')); article.append(chips); if (record.summary) article.append(el('p', record.summary, 'ch-summary')); root.append(article); });
  }
  function filters() { return new URLSearchParams({days:$('ch-days').value, q:$('ch-search').value.trim()}); }
  async function loadRecords() { const generation = ++state.recordsGeneration; message('ch-record-status', '正在加载记录…'); $('ch-records').setAttribute('aria-busy','true');
    try { const data = await request('/records?' + filters()); if (generation !== state.recordsGeneration) return; if (!Array.isArray(data.records)) throw new Error('记录列表格式异常。'); renderRecords($('ch-records'), data.records); message('ch-record-status', data.records.length ? '已显示 ' + data.records.length + ' 条记录。' : '此时间范围和地址下暂无检测记录。'); }
    catch (e) { if (generation === state.recordsGeneration) { $('ch-records').replaceChildren(); message('ch-record-status', e.message + ' 点击“刷新”重试。', true); } }
    finally { if (generation === state.recordsGeneration) $('ch-records').setAttribute('aria-busy','false'); }
  }
  $('ch-search-form').addEventListener('submit', e => { e.preventDefault(); loadRecords(); }); $('ch-days').addEventListener('change', loadRecords); $('ch-record-refresh').addEventListener('click', loadRecords);
  document.querySelectorAll('[data-ch-export]').forEach(b => b.addEventListener('click', async () => { b.disabled = true; try { const query = filters(); query.set('format', b.dataset.chExport); const response = await fetch(API + '/export?' + query, {cache:'no-store', credentials:'same-origin'}); if (!response.ok) throw new Error(); const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = el('a'); a.href = url; a.download = 'channel-records.' + b.dataset.chExport; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); message('ch-record-status', '导出文件已生成。'); } catch (_) { message('ch-record-status', '导出失败，请重试。', true); } finally { b.disabled = false; } }));
  let detailId;
  async function openDetail(id) { detailId = id; const generation = ++state.detailGeneration; const root = $('ch-detail-content'); root.replaceChildren(el('p','正在加载证据…')); $('ch-detail-retry').hidden = true; if (!$('ch-detail').open) $('ch-detail').showModal();
    try { const record = await request('/records/' + encodeURIComponent(id)); if (generation !== state.detailGeneration) return; root.replaceChildren(el('h3', record.model || '检测记录'), el('p', (record.base_url || '—') + ' · ' + (record.source_label || '未知来源')), el('p', verdicts[verdict(record)] + ' · 加权通过率 ' + score(record) + ' · 证据 ' + confidenceLabel(record)), el('p', record.summary || '暂无结论摘要。'));
      const evidence = Array.isArray(record.evidence) ? record.evidence : []; if (!evidence.length) root.append(el('p','暂无有效证据，不能据此确认来源。','ch-empty'));
      else root.append(evidenceTable(evidence));
    } catch (e) { if (generation === state.detailGeneration) { root.replaceChildren(el('p',e.message,'ch-error')); $('ch-detail-retry').hidden = false; } }
  }
  $('ch-detail-close').addEventListener('click', () => $('ch-detail').close()); $('ch-detail-retry').addEventListener('click', () => openDetail(detailId));
  window.addEventListener('beforeunload', event => { if (locked()) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pagehide', () => { $('ch-key').value = ''; $('ch-url').value = ''; clearTimeout(state.timer); state.generation++; state.models = []; state.selected.clear(); state.targets = []; state.connected = false; state.connecting = false; });
  window.addEventListener('pageshow', event => { if (event.persisted) { $('ch-connection').textContent = '尚未验证'; message('ch-connect-status', '连接配置已清除，请重新验证。'); renderModels(); syncControls(); if (state.running) poll(); } });
  document.addEventListener('record-deleted', event => { if (event.detail.url.startsWith(API + '/records/')) loadRecords(); });
  renderModels(); syncControls(); loadRecords();
})();
