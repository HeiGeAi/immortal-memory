#!/usr/bin/env python3
"""Static shell for the evidence-driven Immortal Control Center."""

from __future__ import annotations

import html


def control_center_page_html(title: str = "Immortal Control Center") -> str:
    safe_title = html.escape(title)
    return _PAGE.replace("__TITLE__", safe_title)


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<style>
:root {
  --coal: #080b0b;
  --ink: #0d1212;
  --panel: #111817;
  --panel2: #151e1c;
  --paper: #edf0e8;
  --muted: #94a19b;
  --faint: #5e6c67;
  --line: #293531;
  --cyan: #4de1c1;
  --lime: #b9ef65;
  --amber: #f2bd58;
  --red: #ff7168;
  --blue: #8ab7ff;
  --radius: 3px;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; background: var(--coal); color: var(--paper); }
body {
  font-family: "Avenir Next Condensed", "DIN Condensed", "PingFang SC", sans-serif;
  line-height: 1.48;
  background:
    linear-gradient(rgba(77,225,193,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(77,225,193,.025) 1px, transparent 1px),
    radial-gradient(circle at 84% 0%, rgba(77,225,193,.08), transparent 31%),
    #080b0b;
  background-size: 42px 42px, 42px 42px, auto, auto;
}
button { font: inherit; }
.mono, .metric-value, time, .eyebrow, .proof-code, .stage-index {
  font-family: "SFMono-Regular", Menlo, Consolas, monospace;
}
.shell { width: min(1540px, 100%); margin: 0 auto; padding: 24px; }
.masthead {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 24px;
  align-items: end;
  border-top: 7px solid var(--cyan);
  border-bottom: 1px solid var(--line);
  padding: 22px 0 18px;
}
.eyebrow { color: var(--cyan); font-size: 11px; letter-spacing: .2em; text-transform: uppercase; }
h1 { margin: 7px 0 0; font-size: clamp(38px, 6vw, 78px); line-height: .85; letter-spacing: -.045em; }
.head-meta { text-align: right; color: var(--muted); font-size: 12px; }
.head-meta strong { display: block; color: var(--paper); font-size: 15px; }
.nav { display: flex; justify-content: space-between; align-items: center; gap: 14px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.nav-links, .nav-tools { display: flex; gap: 8px; flex-wrap: wrap; }
a, button { color: inherit; }
.link, .button {
  border: 1px solid var(--line);
  background: var(--ink);
  color: var(--muted);
  padding: 8px 11px;
  text-decoration: none;
  border-radius: var(--radius);
  cursor: pointer;
  min-height: 36px;
}
.link:hover, .button:hover { border-color: var(--cyan); color: var(--paper); }
.button.primary { background: var(--lime); border-color: var(--lime); color: #11170c; font-weight: 800; }
.button:disabled { cursor: not-allowed; opacity: .42; }
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(300px, .7fr);
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  margin-top: 20px;
}
.hero-main, .hero-side { background: rgba(13,18,18,.96); padding: clamp(22px, 4vw, 50px); }
.hero-main { position: relative; overflow: hidden; min-height: 280px; }
.hero-main::after {
  content: "01";
  position: absolute;
  right: 16px;
  bottom: -32px;
  color: rgba(77,225,193,.07);
  font: 900 220px/.8 "SFMono-Regular", monospace;
}
.state-line { position: relative; z-index: 1; display: flex; gap: 12px; align-items: center; }
.state-dot { width: 13px; height: 13px; background: var(--faint); border: 2px solid var(--paper); }
.state-dot.healthy, .state-dot.success { background: var(--lime); }
.state-dot.running { background: var(--cyan); }
.state-dot.attention, .state-dot.stale { background: var(--amber); }
.state-dot.failed { background: var(--red); }
.state-label { color: var(--muted); letter-spacing: .12em; font-size: 12px; }
.hero-status { position: relative; z-index: 1; margin: 18px 0 14px; font-size: clamp(48px, 8vw, 112px); line-height: .82; letter-spacing: -.055em; }
.hero-copy { position: relative; z-index: 1; max-width: 720px; color: #b6c1bc; font-size: 15px; }
.hero-side { display: grid; align-content: space-between; gap: 24px; }
.run-clock-label { color: var(--muted); font-size: 12px; }
.run-clock { margin-top: 8px; font: 700 clamp(34px, 5vw, 62px)/1 "SFMono-Regular", monospace; color: var(--cyan); }
.run-stage { border-top: 1px solid var(--line); padding-top: 18px; }
.run-stage strong { display: block; font-size: 24px; }
.run-stage span { color: var(--muted); font-size: 12px; }
.section-head { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin: 30px 0 11px; }
.section-head h2 { margin: 0; font-size: 23px; letter-spacing: -.02em; }
.section-head p { margin: 0; color: var(--muted); font-size: 12px; }
.proof-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border: 1px solid var(--line); }
.proof { min-height: 145px; padding: 15px; border-right: 1px solid var(--line); background: rgba(17,24,23,.92); }
.proof:last-child { border-right: 0; }
.proof-top { display: flex; justify-content: space-between; gap: 8px; }
.proof-code { color: var(--faint); font-size: 10px; }
.tag { display: inline-flex; padding: 3px 7px; border: 1px solid var(--line); font-size: 10px; color: var(--muted); text-transform: uppercase; }
.tag.healthy, .tag.success { color: var(--lime); border-color: rgba(185,239,101,.45); }
.tag.running { color: var(--cyan); border-color: rgba(77,225,193,.45); }
.tag.attention, .tag.stale { color: var(--amber); border-color: rgba(242,189,88,.5); }
.tag.failed { color: var(--red); border-color: rgba(255,113,104,.55); }
.proof h3 { margin: 22px 0 7px; font-size: 16px; }
.proof p { margin: 0; color: var(--muted); font-size: 12px; }
.work-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(340px, .75fr); gap: 18px; }
.panel { border: 1px solid var(--line); background: rgba(17,24,23,.92); }
.stage-list { padding: 6px 18px; }
.stage {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr) auto;
  gap: 14px;
  align-items: start;
  padding: 16px 0;
  border-bottom: 1px solid var(--line);
}
.stage:last-child { border-bottom: 0; }
.stage-index { width: 34px; height: 34px; display: grid; place-items: center; border: 1px solid var(--line); color: var(--faint); }
.stage.running .stage-index { border-color: var(--cyan); color: var(--cyan); }
.stage.success .stage-index { border-color: var(--lime); color: var(--lime); }
.stage.failed .stage-index { border-color: var(--red); color: var(--red); }
.stage h3 { margin: 0 0 4px; font-size: 15px; }
.stage p { margin: 0; color: var(--muted); font-size: 12px; white-space: pre-wrap; }
.stage-time { color: var(--muted); font: 11px "SFMono-Regular", monospace; text-align: right; }
.metrics { display: grid; grid-template-columns: 1fr 1fr; }
.metric { min-height: 150px; padding: 20px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.metric:nth-child(2n) { border-right: 0; }
.metric:nth-last-child(-n+2) { border-bottom: 0; }
.metric-value { color: var(--cyan); font-size: clamp(32px, 4vw, 54px); line-height: 1; letter-spacing: -.05em; }
.metric-label { margin-top: 12px; color: var(--muted); font-size: 12px; }
.layer-list, .attention-list, .history-list, .job-list { display: grid; gap: 8px; }
.layer-list { padding: 13px; }
.layer { display: flex; justify-content: space-between; gap: 10px; padding: 10px; background: var(--ink); border-left: 3px solid var(--line); }
.layer.healthy { border-left-color: var(--lime); }
.layer strong { font-size: 13px; }
.layer span { color: var(--muted); font-size: 11px; text-align: right; }
.attention-list, .history-list, .job-list { padding: 13px; }
.notice, .history-row, .job { padding: 13px; background: var(--ink); border: 1px solid var(--line); }
.notice.failed { border-left: 4px solid var(--red); }
.notice.attention { border-left: 4px solid var(--amber); }
.notice.unknown { border-left: 4px solid var(--faint); }
.notice h3, .history-row h3, .job h3 { margin: 0 0 5px; font-size: 14px; }
.notice p, .history-row p, .job p { margin: 0; color: var(--muted); font-size: 12px; }
.control-panel { padding: 16px; }
.control-copy { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
.control-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.log {
  margin-top: 10px;
  background: #050707;
  border: 1px solid var(--line);
  padding: 10px;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  color: #bdcbc6;
  font: 11px/1.5 "SFMono-Regular", monospace;
}
.empty { padding: 24px; color: var(--muted); font-size: 13px; text-align: center; }
.footer { display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid var(--line); margin-top: 30px; padding: 16px 0; color: var(--faint); font-size: 11px; }
@media (max-width: 1100px) {
  .proof-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .proof { border-bottom: 1px solid var(--line); }
  .work-grid, .hero { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .shell { padding: 12px; }
  .masthead { grid-template-columns: 1fr; }
  .head-meta { text-align: left; }
  .nav { align-items: flex-start; flex-direction: column; }
  .proof-grid { grid-template-columns: 1fr; }
  .proof { border-right: 0; }
  .stage { grid-template-columns: 40px minmax(0, 1fr); }
  .stage-time { grid-column: 2; text-align: left; }
  .metrics, .control-actions { grid-template-columns: 1fr; }
  .metric { border-right: 0; }
  .metric:nth-last-child(-n+2) { border-bottom: 1px solid var(--line); }
  .metric:last-child { border-bottom: 0; }
  .footer { flex-direction: column; }
}
@media print {
  :root { --coal: #fff; --ink: #fff; --panel: #fff; --panel2: #fff; --paper: #111; --muted: #444; --faint: #666; --line: #bbb; }
  body { background: #fff; }
  .shell { width: 100%; padding: 0; }
  .nav-tools, .control-panel { display: none; }
  .panel, .hero-main, .hero-side, .proof { background: #fff; break-inside: avoid; }
}
</style>
</head>
<body>
<main class="shell">
  <header class="masthead">
    <div>
      <div class="eyebrow">IMMORTAL MEMORY · LOCAL OPERATIONS</div>
      <h1>CONTROL<br>CENTER</h1>
    </div>
    <div class="head-meta">
      <strong id="version">v-</strong>
      <span>只读取本机证据 · 不上传记忆正文</span>
    </div>
  </header>

  <nav class="nav" aria-label="主导航">
    <div class="nav-links">
      <a class="link" href="/agent-factory">Context Factory</a>
      <a class="link" href="/review">Profile Review</a>
      <a class="link" href="/timeline">Timeline</a>
      <a class="link" href="/snapshot">Legacy Snapshot</a>
    </div>
    <div class="nav-tools">
      <button class="button" id="printBtn">打印</button>
      <button class="button" id="refreshBtn">立即刷新</button>
    </div>
  </nav>

  <section class="hero" aria-labelledby="heroStatus">
    <div class="hero-main">
      <div class="state-line"><span class="state-dot unknown" id="stateDot"></span><span class="state-label">SYSTEM VERDICT</span></div>
      <div class="hero-status" id="heroStatus">读取证据</div>
      <p class="hero-copy" id="heroCopy">正在核对运行心跳、调度器、最近结果与备份边界。</p>
    </div>
    <div class="hero-side">
      <div>
        <div class="run-clock-label">当前运行时长</div>
        <div class="run-clock" id="runClock">00:00</div>
      </div>
      <div class="run-stage">
        <span>当前阶段</span>
        <strong id="currentStage">未确认</strong>
        <span id="lastRefresh">等待首次刷新</span>
      </div>
    </div>
  </section>

  <div class="section-head"><div><h2>运行证明</h2><p>每个结论都附带来源，未知不会显示为健康。</p></div><p class="mono">PROOF STRIP · 05</p></div>
  <section class="proof-grid" id="proofs" aria-label="运行证明"></section>

  <section class="work-grid">
    <div>
      <div class="section-head"><div><h2>当前运行</h2><p>结构化阶段、真实耗时和失败证据。</p></div><p id="heartbeat" class="mono">HEARTBEAT · -</p></div>
      <div class="panel"><div class="stage-list" id="stages"></div></div>

      <div class="section-head"><div><h2>风险与建议</h2><p>先处理明确故障，再处理恢复边界。</p></div></div>
      <div class="panel"><div class="attention-list" id="attention"></div></div>

      <div class="section-head"><div><h2>最近运行</h2><p>历史保留最近 100 次，页面展示 20 次。</p></div></div>
      <div class="panel"><div class="history-list" id="history"></div></div>
    </div>

    <aside>
      <div class="section-head"><div><h2>本轮产出</h2><p>来自运行遥测和编排器状态。</p></div></div>
      <div class="panel metrics">
        <div class="metric"><div class="metric-value" id="metricNew">0</div><div class="metric-label">本地新增记录</div></div>
        <div class="metric"><div class="metric-value" id="metricFeishu">0</div><div class="metric-label">飞书新增记录</div></div>
        <div class="metric"><div class="metric-value" id="metricWeb">0</div><div class="metric-label">网页新增记录</div></div>
        <div class="metric"><div class="metric-value" id="metricTotal">0</div><div class="metric-label">总记录数</div></div>
      </div>

      <div class="section-head"><div><h2>输出层</h2><p>存在性和最后更新时间。</p></div></div>
      <div class="panel"><div class="layer-list" id="layers"></div></div>

      <div class="section-head"><div><h2>安全控制</h2><p>只执行本机白名单命令。</p></div></div>
      <div class="panel control-panel">
        <p class="control-copy">启动前会再次确认。全流程运行期间会禁止重复启动，日志最多保留最近内容。</p>
        <div class="control-actions">
          <button class="button primary action" data-action="run">立即运行全流程</button>
          <button class="button action" data-action="health">运行健康检查</button>
          <button class="button action" data-action="backup_verify">校验最新备份</button>
          <button class="button action" data-action="profile_refresh">刷新画像</button>
        </div>
        <div class="job-list" id="jobs"></div>
      </div>
    </aside>
  </section>

  <footer class="footer"><span>Immortal Memory · Evidence before green</span><span id="generatedAt">未生成快照</span></footer>
</main>
<script>
const API_STATE = '/api/control-center/state';
const API_ACTIONS = '/api/control-center/actions';
let snapshot = null;
let elapsedTimer = null;
const $ = (id) => document.getElementById(id);
const clean = (value, fallback = '未知') => value === null || value === undefined || value === '' ? fallback : String(value);
const esc = (value) => clean(value, '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const number = (value) => Number(value || 0).toLocaleString('zh-CN');
const statusLabel = (status) => ({healthy:'健康',success:'成功',running:'运行中',attention:'需关注',failed:'失败',unknown:'未知',stale:'心跳超时',skipped:'跳过'}[status] || clean(status));
const when = (value) => {
  if (!value) return '无时间证据';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? clean(value) : date.toLocaleString('zh-CN', {hour12:false});
};
const duration = (seconds) => {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
};

function verdictCopy(data) {
  if (data.status === 'running') return 'Immortal 正在真实运行。阶段与心跳来自编排器遥测，其他风险仍单独保留。';
  if (data.status === 'healthy') return '关键运行证据均在有效期内，没有发现明确故障。';
  if (data.status === 'failed') return '发现明确失败证据。请先查看风险区中的错误和来源。';
  if (data.status === 'attention') return '主链路可读，但存在陈旧、部分失败或恢复边界。';
  return '目前证据不足，不能把系统判断为健康。';
}

function renderProofs(items) {
  $('proofs').innerHTML = (items || []).map((item, index) => `
    <article class="proof">
      <div class="proof-top"><span class="proof-code">P${String(index + 1).padStart(2,'0')}</span><span class="tag ${esc(item.status)}">${esc(statusLabel(item.status))}</span></div>
      <h3>${esc(item.label)}</h3>
      <p>${esc(item.detail)}</p>
      <p class="proof-code" title="${esc(item.source)}">${esc(item.source)}</p>
    </article>`).join('') || '<div class="empty">没有运行证明。</div>';
}

function renderStages(run) {
  const items = run && Array.isArray(run.stages) ? run.stages : [];
  $('stages').innerHTML = items.map((item, index) => `
    <article class="stage ${esc(item.status)}">
      <div class="stage-index">${String(index + 1).padStart(2,'0')}</div>
      <div><h3>${esc(item.label || item.id)}</h3><p>${esc(item.error || item.summary || statusLabel(item.status))}</p></div>
      <div class="stage-time">${esc(statusLabel(item.status))}<br>${item.elapsed_seconds === null || item.elapsed_seconds === undefined ? when(item.started_at) : duration(item.elapsed_seconds)}</div>
    </article>`).join('') || '<div class="empty">暂无结构化阶段记录。下一次全流程运行会在这里留下证据。</div>';
}

function renderAttention(items) {
  $('attention').innerHTML = (items || []).map(item => `
    <article class="notice ${esc(item.status)}"><h3>${esc(item.label)} · ${esc(statusLabel(item.status))}</h3><p>${esc(item.detail)}</p><p class="proof-code">来源：${esc(item.source)}</p></article>`
  ).join('') || '<div class="empty">没有待处理项。</div>';
}

function renderHistory(items) {
  $('history').innerHTML = (items || []).map(item => `
    <article class="history-row"><h3><span class="tag ${esc(item.status)}">${esc(statusLabel(item.status))}</span> ${esc(item.trigger || 'unknown')}</h3>
      <p>${when(item.started_at)} · ${number((item.results || {}).new_records)} 条新增 · ${esc(item.error || '无错误记录')}</p></article>`
  ).join('') || '<div class="empty">还没有结构化运行历史。</div>';
}

function renderLayers(items) {
  $('layers').innerHTML = (items || []).map(item => `
    <div class="layer ${esc(item.status)}"><strong>${esc(item.label)}</strong><span>${item.exists ? when(item.updated_at) : '文件不存在'}<br>${number(item.bytes)} B</span></div>`
  ).join('');
}

function renderJobs(items) {
  $('jobs').innerHTML = (items || []).slice(0, 5).map(item => `
    <article class="job"><h3><span class="tag ${esc(item.status)}">${esc(statusLabel(item.status))}</span> ${esc(item.kind)}</h3>
      <p>${esc(item.summary || item.error || when(item.started_at || item.created_at))}</p>
      ${(item.stdout || item.stderr) ? `<details><summary>查看日志</summary><div class="log">${esc((item.stdout || '') + '\\n' + (item.stderr || ''))}</div></details>` : ''}
    </article>`).join('');
}

function startElapsed(run) {
  if (elapsedTimer) clearInterval(elapsedTimer);
  const tick = () => {
    if (!run || run.status !== 'running' || !run.started_at) {
      $('runClock').textContent = run && run.finished_at && run.started_at ? duration((new Date(run.finished_at) - new Date(run.started_at)) / 1000) : '00:00';
      return;
    }
    $('runClock').textContent = duration((Date.now() - new Date(run.started_at).getTime()) / 1000);
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function render(data) {
  snapshot = data;
  $('version').textContent = `v${clean(data.version, '-')}`;
  $('heroStatus').textContent = clean(data.status_label, '证据不足');
  $('heroCopy').textContent = verdictCopy(data);
  $('stateDot').className = `state-dot ${clean(data.status, 'unknown')}`;
  $('generatedAt').textContent = `快照：${when(data.generated_at)}`;
  $('lastRefresh').textContent = `最后刷新：${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
  const run = data.current_run || {};
  const current = (run.stages || []).find(item => item.id === run.current_stage);
  $('currentStage').textContent = current ? current.label : (run.status ? statusLabel(run.status) : '未运行');
  $('heartbeat').textContent = `HEARTBEAT · ${run.updated_at ? when(run.updated_at) : '无证据'}`;
  $('metricNew').textContent = number((data.metrics || {}).new_records);
  $('metricFeishu').textContent = number((data.metrics || {}).feishu_new_records);
  $('metricWeb').textContent = number((data.metrics || {}).web_new_records);
  $('metricTotal').textContent = number((data.metrics || {}).total_records);
  renderProofs(data.proofs);
  renderStages(run);
  renderAttention(data.attention);
  renderHistory(data.history);
  renderLayers(data.layers);
  renderJobs(data.jobs);
  const busy = run.status === 'running' || (data.jobs || []).some(job => ['queued','running'].includes(job.status) && job.kind === 'run');
  document.querySelectorAll('[data-action="run"]').forEach(button => button.disabled = busy);
  startElapsed(run);
}

async function refresh() {
  try {
    const response = await fetch(API_STATE, {cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    $('heroStatus').textContent = '控制台断联';
    $('heroCopy').textContent = `无法读取本地状态接口：${error.message}`;
    $('stateDot').className = 'state-dot failed';
  }
}

async function runAction(action) {
  const labels = {run:'立即运行全流程',health:'运行健康检查',backup_verify:'校验最新备份',profile_refresh:'刷新画像'};
  if (!window.confirm(`确认${labels[action] || action}？\\n只会执行本机白名单命令。`)) return;
  document.querySelectorAll('.action').forEach(button => button.disabled = true);
  try {
    const response = await fetch(API_ACTIONS, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action})
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    await refresh();
  } catch (error) {
    window.alert(`操作未启动：${error.message}`);
  } finally {
    document.querySelectorAll('.action').forEach(button => button.disabled = false);
  }
}

$('refreshBtn').addEventListener('click', refresh);
$('printBtn').addEventListener('click', () => window.print());
document.querySelectorAll('.action').forEach(button => button.addEventListener('click', () => runAction(button.dataset.action)));
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
