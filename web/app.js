const $ = id => document.getElementById(id);
const terminal = new Set(['run.completed', 'run.cancelled', 'run.failed']);
const types = ['run.created', 'run.started', 'model.started', 'model.delta', 'model.reasoning', 'model.usage', 'model.finished', 'tool.started', 'tool.completed', 'checkpoint.saved', ...terminal];
let profiles = [], source, current, cursor = 0, answer = '', generation = 0, snapshotVersion = 0;
let selectedCheckpointId = null, selectedTable = 'runs', tableOffset = 0, storageVersion = 0;
let detailsVersion = 0;
async function api(path, data) {
  const res = await fetch(`/api${path}`, data === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  const value = await res.json();
  if (!res.ok) throw new Error(value.error?.message ?? `HTTP ${res.status}`);
  return value;
}
function notice(message = '') { $('notice').textContent = message; }
function updateRun(run) {
  current = run;
  $('status').textContent = run.status.toUpperCase();
  $('ttft').textContent = run.ttftMs === null ? '—' : `${run.ttftMs} ms`;
  $('runId').textContent = `RUN ${run.id}  /  MODEL ${run.input.model}  /  TRACE ${run.traceId}`;
  $('cancel').disabled = run.status !== 'running';
  $('resume').disabled = !['cancelled', 'failed'].includes(run.status);
  $('download').disabled = false;
  $('replay').disabled = false;
  $('parentRun').hidden = !run.parentRunId;
  $('parentRun').textContent = run.parentRunId ? `↖ 父 Run ${run.parentRunId} · 来源快照 ${run.sourceCheckpointId ?? '旧版'}` : '';
  if (!selectedCheckpointId) $('checkpoint').textContent = JSON.stringify(run.checkpoint, null, 2);
}
function closeSource() { source?.close(); source = undefined; }
function subscribe() {
  closeSource();
  if (!current) return;
  if (current.status !== 'running' && cursor >= current.lastSeq) { $('subscription').textContent = '回放完毕'; return; }
  const id = current.id;
  const selected = generation;
  source = new EventSource(`/api/runs/${id}/events?after=${cursor}`);
  $('subscription').textContent = '连接中'; $('disconnect').disabled = false; $('reconnect').disabled = true;
  source.onopen = () => { if (selected === generation) $('subscription').textContent = '已连接'; };
  source.onerror = () => { if (selected === generation) $('subscription').textContent = '自动重连中'; };
  for (const type of types) source.addEventListener(type, message => {
    if (current?.id !== id || selected !== generation) return;
    const event = JSON.parse(message.data);
    if (event.seq <= cursor) return; // 防重，不依靠页面文本猜测是否重复。
    cursor = event.seq; $('count').textContent = String(cursor);
    const row = document.createElement('div'); row.className = 'event';
    const seq = document.createElement('span'); seq.className = 'seq'; seq.textContent = `#${event.seq}`;
    const time = document.createElement('time'); time.textContent = new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour12: false });
    const name = document.createElement('span'); name.className = 'type'; name.textContent = event.type;
    const data = document.createElement('span'); data.className = 'data'; data.textContent = JSON.stringify(event.data);
    row.append(seq, time, name, data); $('timeline').append(row);
    // 页面仅渲染最近 500 条；完整 Trace 可从服务端导出。
    while ($('timeline').children.length > 500) $('timeline').firstElementChild.remove();
    $('timeline').scrollTop = $('timeline').scrollHeight;
    if (type === 'model.delta') { answer += event.data.text; $('answer').textContent = answer; }
    if (type === 'model.reasoning') { $('reasoningBox').hidden = false; $('reasoning').textContent += event.data.text; }
    if (type === 'run.started' && current.status === 'running') $('status').textContent = 'RUNNING';
    if (type === 'checkpoint.saved') void safe(() => refreshCurrent(id));
    if (terminal.has(type)) {
      updateRun({...current, status:type.slice(4), lastSeq:cursor});
      closeSource(); $('subscription').textContent = '已结束'; $('disconnect').disabled = true;
      if (type === 'run.failed') notice(`${event.data.code}：${event.data.message}`);
      if (type === 'run.cancelled') notice('运行已取消，上游请求已中止。');
      void safe(() => refreshCurrent(id)); void safe(history);
    } else if (type === 'model.delta' && $('ttft').textContent === '—') void safe(() => refreshCurrent(id));
  });
}
async function refreshCurrent(id) {
  const version = ++snapshotVersion, selected = generation;
  const run = await api(`/runs/${id}`);
  if (current?.id === id && version === snapshotVersion && selected === generation) {
    updateRun(run); await refreshRunDetails(id, selected);
  }
}
async function select(run) {
  generation++; closeSource(); cursor = 0; answer = ''; selectedCheckpointId = null;
  $('answer').textContent = ''; $('timeline').replaceChildren(); $('reasoning').textContent = '';
  $('reasoningBox').hidden = true; $('count').textContent = '0'; notice();
  updateRun(run); localStorage.setItem('gateway-run', run.id); subscribe();
  await refreshRunDetails(run.id, generation);
}
async function history() {
  const runs = await api('/runs'); $('history').replaceChildren();
  if (!runs.length) $('history').textContent = '还没有运行记录';
  for (const run of runs) {
    const button = document.createElement('button');
    const status = document.createElement('small'); status.textContent = run.status;
    button.append(status, document.createTextNode(run.input.prompt)); button.title = run.id;
    button.onclick = () => safe(() => select(run)); $('history').append(button);
  }
}
function updateModel() {
  const p = profiles.find(p => p.id === $('model').value);
  $('thinking').disabled = !p?.thinking || p.thinking_mode === 'required';
  if (!p?.thinking) $('thinking').checked = false;
  if (p?.thinking_mode === 'required') $('thinking').checked = true;
  $('tools').disabled = !p?.tools;
  if (!p?.tools) $('tools').checked = false;
  $('modelHint').textContent = `${p?.family ?? ''} · ${p?.thinking_mode === 'required' ? '始终思考' : p?.thinking ? '可切换思考' : '不发送思考参数'} · ${p?.tools ? '支持工具' : '未启用工具'}`;
}
async function safe(fn) { try { await fn(); } catch (e) { notice(e.message); } }
$('form').onsubmit = e => { e.preventDefault(); void safe(async () => {
  $('submit').disabled = true;
  try { const run = await api('/runs', { prompt: $('prompt').value, model: $('model').value, tools: $('tools').checked, thinking: $('thinking').checked }); await select(run); await history(); }
  finally { $('submit').disabled = false; }
}); };
$('model').onchange = updateModel;
$('cancel').onclick = () => safe(async () => { const id = current.id, selected = generation; const run = await api(`/runs/${id}/cancel`, {}); if (current?.id === id && selected === generation) updateRun(run); });
$('disconnect').onclick = () => { closeSource(); $('subscription').textContent = '已断开'; $('disconnect').disabled = true; $('reconnect').disabled = false; notice('仅断开订阅。Run 继续执行；恢复后从最后事件序号重放。'); };
$('reconnect').onclick = () => safe(async () => { await refreshCurrent(current.id); notice(); subscribe(); });
$('resume').onclick = () => safe(async () => { const run = await api(`/runs/${current.id}/resume`, selectedCheckpointId ? {checkpointId:selectedCheckpointId} : {}); await select(run); await history(); await refreshStorage(); });
$('replay').onclick = () => safe(() => select(current));
$('parentRun').onclick = () => safe(async () => select(await api(`/runs/${current.parentRunId}`)));
$('refresh').onclick = () => safe(history);
$('tabEvents').onclick = () => { $('timeline').hidden = false; $('checkpointPane').hidden = true; $('tabEvents').classList.add('active'); $('tabCheckpoint').classList.remove('active'); };
$('tabCheckpoint').onclick = () => { $('timeline').hidden = true; $('checkpointPane').hidden = false; $('tabCheckpoint').classList.add('active'); $('tabEvents').classList.remove('active'); if (current) void safe(() => refreshCurrent(current.id)); };
$('download').onclick = () => safe(async () => {
  const id = current.id, all = []; let after = 0, run;
  while (true) { const page = await api(`/runs/${id}/trace?after=${after}`); all.push(...page.events); run = page.run; after = page.nextCursor; if (!page.hasMore) break; }
  const url = URL.createObjectURL(new Blob([JSON.stringify({ run, events: all }, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = `trace-${id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
void safe(async () => {
  const config = await api('/models'); profiles = config.models;
  $('provider').textContent = config.provider === 'demo' ? '本地演示 · 无需密钥' : '千问 AI 平台 · 真实模型';
  $('modeHint').textContent = config.provider === 'demo' ? '演示模式使用本地事件流，不调用阿里云、不产生费用。配置 .env 后切换真实模型。' : `真实模型模式 · ${new URL(config.baseUrl).hostname}。Qwen 与 DeepSeek 共用兼容接口，能力参数按模型配置。`;
  for (const family of [...new Set(profiles.map(p => p.family))]) {
    const group = document.createElement('optgroup'); group.label = family;
    for (const profile of profiles.filter(p => p.family === family)) { const option = document.createElement('option'); option.value = profile.id; option.textContent = profile.label; group.append(option); }
    $('model').append(group);
  }
  updateModel(); await history(); await refreshStorage();
  const id = localStorage.getItem('gateway-run'); if (id) { try { await select(await api(`/runs/${id}`)); } catch { localStorage.removeItem('gateway-run'); } }
});

async function refreshRunDetails(id, selected) {
  const version = ++detailsVersion;
  const [summary, saved] = await Promise.all([api(`/runs/${id}/summary`), api(`/runs/${id}/checkpoints`)]);
  if (current?.id !== id || generation !== selected || version !== detailsVersion) return;
  $('modelCalls').textContent = summary.modelCalls;
  $('toolCalls').textContent = summary.toolCalls;
  $('tokenCount').textContent = summary.usage.total_tokens;
  $('terminalCount').textContent = summary.terminalCount;
  $('checkpointList').replaceChildren();
  for (const checkpoint of saved.checkpoints) {
    const button = document.createElement('button');
    button.textContent = `Step ${checkpoint.step} · ${checkpoint.kind} · #${checkpoint.seq} · ${checkpoint.messageCount} 条消息`;
    button.title = checkpoint.id;
    if (selectedCheckpointId === checkpoint.id) button.classList.add('selected');
    button.onclick = () => safe(async () => {
      const value = await api(`/runs/${id}/checkpoints/${checkpoint.id}`);
      if (current?.id !== id || generation !== selected) return;
      selectedCheckpointId = checkpoint.id;
      $('checkpointSelected').textContent = `CHECKPOINT ${checkpoint.id} · ${checkpoint.timestamp} · 来源 ${checkpoint.sourceCheckpointId ?? '本 Run'}`;
      $('checkpoint').textContent = JSON.stringify(value.snapshot, null, 2);
      for (const item of $('checkpointList').children) item.classList.toggle('selected', item.title === checkpoint.id);
    });
    $('checkpointList').append(button);
  }
  if (!selectedCheckpointId) $('checkpointSelected').textContent = `最新安全边界 · Step ${current.checkpoint.step} · 共 ${saved.checkpoints.length} 个持久化快照`;
}

async function refreshStorage() {
  const version = ++storageVersion, table = selectedTable, offset = tableOffset;
  const [state, page] = await Promise.all([api('/storage'), api(`/storage/tables/${table}?offset=${offset}&limit=10`)]);
  if (version !== storageVersion) return;
  $('dbEngine').textContent = `${state.connected ? '● ' : ''}${state.engine}`;
  $('dbState').textContent = `已连接 · Schema v${state.schemaVersion} · ${(state.sizeBytes / 1024).toFixed(0)} KB · ${state.durability}`;
  const counts = Object.fromEntries(state.tables.map(t => [t.name, t.count]));
  $('dbRuns').textContent = `${counts.runs} 个 Run`;
  $('dbEvents').textContent = `${counts.events} 条事件已持久化 · ${state.agent.activeJobs} 个执行任务`;
  const latency = value => value === null ? '—' : `${Math.round(value)} ms`;
  $('dbLatency').textContent = `TTFT P50 ${latency(state.ttft.p50Ms)} / P95 ${latency(state.ttft.p95Ms)} · ${state.ttft.sampleCount} 个样本`;
  $('dbLatency').title = state.ttft.scope;
  $('dbCheckpoints').textContent = `${counts.checkpoints} 个快照`;
  $('dbWorker').textContent = `实时订阅 ${state.subscribers} · 单 worker · 最多 ${state.agent.maxSteps} 步`;
  $('databaseSchema').textContent = JSON.stringify(state.tables.map(t => ({table:t.name, columns:t.columns})), null, 2);
  $('tableDescription').textContent = {runs:'Run Store：独立 runId、状态、模型、最后事件序号、traceId 与父运行。',events:'不可变事件日志：按 run_id + seq 唯一保存，是断线重放与审计的事实来源。',checkpoints:'独立快照历史表：initial 初始边界、step 工具边界、final 完成快照；legacy 为旧版最新快照迁移。'}[table];
  const head = $('databaseTable').querySelector('thead'), body = $('databaseTable').querySelector('tbody');
  head.replaceChildren(); body.replaceChildren();
  const heading = document.createElement('tr');
  for (const name of [...page.columns, '操作']) { const cell = document.createElement('th'); cell.textContent = name; heading.append(cell); }
  head.append(heading);
  for (const row of page.rows) {
    const tr = document.createElement('tr');
    for (const name of page.columns) { const cell = document.createElement('td'); cell.textContent = row[name] ?? '—'; cell.title = cell.textContent; tr.append(cell); }
    const action = document.createElement('td'), button = document.createElement('button');
    button.textContent = '打开 Run';
    const runId = table === 'runs' ? row.id : row.run_id;
    button.onclick = () => safe(async () => { await select(await api(`/runs/${runId}`)); $('runId').scrollIntoView({behavior:'smooth',block:'center'}); });
    action.append(button); tr.append(action); body.append(tr);
  }
  if (!page.rows.length) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = page.columns.length + 1; cell.textContent = '尚无数据，创建一个 Run 后查看。'; row.append(cell); body.append(row); }
  $('tablePage').textContent = `${page.total ? offset + 1 : 0}–${offset + page.rows.length} / ${page.total} 条`;
  $('previousPage').disabled = offset === 0; $('nextPage').disabled = !page.hasMore;
}
for (const [id, table] of [['tableRuns','runs'],['tableEvents','events'],['tableCheckpoints','checkpoints']]) {
  $(id).onclick = () => safe(async () => { selectedTable = table; tableOffset = 0; for (const button of [ $('tableRuns'), $('tableEvents'), $('tableCheckpoints') ]) button.classList.toggle('active', button.id === id); await refreshStorage(); });
}
$('refreshStorage').onclick = () => safe(refreshStorage);
$('previousPage').onclick = () => safe(async () => { tableOffset = Math.max(0, tableOffset - 10); await refreshStorage(); });
$('nextPage').onclick = () => safe(async () => { tableOffset += 10; await refreshStorage(); });
setInterval(() => { if (!document.hidden) void safe(async () => { await refreshStorage(); if (current?.status === 'running') await refreshCurrent(current.id); }); }, 5000);
