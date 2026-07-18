#!/usr/bin/env python3
"""Unified local product shell for the Immortal Control Center."""

from __future__ import annotations

import html


def control_center_page_html(title: str = "Immortal Control Center") -> str:
    return _PAGE.replace("__TITLE__", html.escape(title))


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<style>
:root {
  --void: #07090b;
  --deck: #0c1013;
  --deck-2: #11171b;
  --deck-3: #151d21;
  --paper: #edf6f2;
  --muted: #889b96;
  --faint: #53635f;
  --line: #24312e;
  --line-hot: #3d5c55;
  --signal: #62e6c4;
  --signal-2: #24b99a;
  --good: #9be27f;
  --warn: #f0b35f;
  --bad: #ff776d;
  --blue: #8fb8ff;
  --radius: 4px;
  --mono: "IBM Plex Mono", "SFMono-Regular", Menlo, Consolas, monospace;
  --display: "Avenir Next Condensed", "PingFang SC", "Microsoft YaHei", sans-serif;
  --body: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
  --ease: cubic-bezier(.2,.8,.2,1);
}
* { box-sizing: border-box; }
html, body { min-height: 100%; margin: 0; background: var(--void); color: var(--paper); }
body {
  overflow-x: clip;
  font: 14px/1.55 var(--body);
  background:
    linear-gradient(rgba(98,230,196,.018) 1px, transparent 1px),
    linear-gradient(90deg, rgba(98,230,196,.014) 1px, transparent 1px),
    radial-gradient(circle at 74% -15%, rgba(36,185,154,.13), transparent 34%),
    var(--void);
  background-size: 52px 52px, 52px 52px, auto, auto;
}
button, input, select { font: inherit; }
button { color: inherit; }
.app { width: min(1560px, 100%); margin: 0 auto; padding: 18px 28px 34px; }
.masthead {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto minmax(220px, 1fr);
  gap: 24px;
  align-items: center;
  min-height: 64px;
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; gap: 12px; align-items: center; }
.brand-mark {
  position: relative;
  width: 30px;
  height: 30px;
  border: 1px solid rgba(98,230,196,.55);
  background: linear-gradient(145deg, rgba(98,230,196,.13), transparent);
  box-shadow: inset 0 0 18px rgba(98,230,196,.08), 0 0 28px rgba(98,230,196,.07);
}
.brand-mark::before, .brand-mark::after { content: ""; position: absolute; background: var(--signal); }
.brand-mark::before { width: 1px; height: 14px; left: 14px; top: 7px; }
.brand-mark::after { width: 14px; height: 1px; left: 7px; top: 14px; }
.eyebrow, .micro, .tag, .mono { font-family: var(--mono); }
.eyebrow { color: var(--signal); font-size: 9px; letter-spacing: .22em; }
.brand h1 { margin: 1px 0 0; font: 650 19px/1 var(--display); letter-spacing: .04em; }
.system-ribbon {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 260px;
  justify-content: center;
  color: var(--muted);
  font: 10px var(--mono);
  letter-spacing: .08em;
}
.beacon {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1px solid #80938e;
  background: var(--faint);
  box-shadow: 0 0 0 5px rgba(83,99,95,.08);
}
.beacon.healthy, .beacon.success, .beacon.ready { background: var(--good); border-color: var(--good); }
.beacon.running { background: var(--signal); border-color: var(--signal); animation: beacon 2s ease-in-out infinite; }
.beacon.attention, .beacon.partial, .beacon.stale { background: var(--warn); border-color: var(--warn); }
.beacon.failed, .beacon.error, .beacon.not_ready { background: var(--bad); border-color: var(--bad); }
.head-tools { display: flex; justify-content: flex-end; gap: 8px; align-items: center; }
.version { color: var(--muted); font: 10px var(--mono); margin-right: 4px; }
.nav {
  display: grid;
  grid-template-columns: repeat(8, minmax(96px, 1fr));
  gap: 1px;
  margin-top: 14px;
  padding: 1px;
  border: 1px solid var(--line);
  background: var(--line);
}
.nav-button {
  position: relative;
  min-height: 56px;
  border: 0;
  background: #0a0e11;
  color: var(--muted);
  cursor: pointer;
  text-align: left;
  padding: 10px 13px;
  transition: color .18s ease, background .18s ease;
}
.nav-button::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 0;
  height: 2px;
  background: var(--signal);
  transform: scaleX(0);
  transform-origin: left;
  transition: transform .22s var(--ease);
}
.nav-button:hover { color: var(--paper); background: #101619; }
.nav-button.active { color: var(--paper); background: linear-gradient(180deg, #12191c, #0d1215); }
.nav-button.active::after { transform: scaleX(1); }
.nav-button strong { display: block; font: 600 12px var(--display); letter-spacing: .04em; }
.nav-button small { color: var(--faint); font: 8px var(--mono); letter-spacing: .12em; }
.nav-button:focus-visible, .control:focus-visible, .field:focus-visible {
  outline: 2px solid var(--signal);
  outline-offset: 2px;
}
.view { min-height: 680px; padding-top: 24px; }
.view:focus { outline: none; }
.view-head {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: end;
  margin-bottom: 14px;
}
.view-kicker { color: var(--signal); font: 9px var(--mono); letter-spacing: .2em; }
.view-head h2 { margin: 5px 0 2px; font: 620 clamp(28px, 4vw, 52px)/1 var(--display); letter-spacing: -.035em; }
.view-head p { margin: 0; color: var(--muted); max-width: 700px; }
.view-meta { color: var(--faint); text-align: right; font: 10px var(--mono); }
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(320px, .6fr);
  min-height: 310px;
  border: 1px solid var(--line);
  background: linear-gradient(135deg, rgba(17,23,27,.98), rgba(8,12,14,.98));
}
.hero-main { position: relative; padding: clamp(28px, 5vw, 68px); overflow: hidden; }
.hero-main::after {
  content: "EVIDENCE BEFORE GREEN";
  position: absolute;
  right: -62px;
  top: 50%;
  transform: rotate(90deg);
  color: rgba(98,230,196,.13);
  font: 8px var(--mono);
  letter-spacing: .34em;
}
.verdict { margin: 28px 0 12px; font: 620 clamp(52px, 7vw, 100px)/.88 var(--display); letter-spacing: -.055em; }
.hero-copy { color: #a6b8b3; max-width: 760px; }
.hero-side { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-left: 1px solid var(--line); }
.hero-stat { min-width: 0; display: flex; flex-direction: column; justify-content: space-between; padding: 24px; border-bottom: 1px solid var(--line); }
.hero-stat:nth-child(odd) { border-right: 1px solid var(--line); }
.hero-stat:nth-last-child(-n+2) { border-bottom: 0; }
.hero-stat span { color: var(--muted); font: 9px var(--mono); letter-spacing: .08em; }
.hero-stat strong { font: 650 clamp(30px, 2.8vw, 44px)/1 var(--mono); letter-spacing: -.07em; }
.section-title { display: flex; justify-content: space-between; align-items: end; gap: 20px; margin: 26px 0 10px; }
.section-title h3 { margin: 0; font: 600 19px var(--display); letter-spacing: .02em; }
.section-title p { margin: 0; color: var(--muted); font-size: 11px; }
.grid { display: grid; gap: 10px; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.card {
  position: relative;
  min-width: 0;
  border: 1px solid var(--line);
  background: linear-gradient(145deg, rgba(17,23,27,.96), rgba(9,13,15,.96));
  padding: 17px;
  box-shadow: inset 0 1px rgba(255,255,255,.018);
}
button.card { width: 100%; color: inherit; cursor: pointer; text-align: left; }
button.card:hover { border-color: var(--line-hot); background: var(--deck-3); }
.card h4 { margin: 12px 0 6px; font: 600 16px var(--display); letter-spacing: .015em; overflow-wrap: anywhere; }
.card p { margin: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.card .source { margin-top: 13px; color: var(--faint); font: 9px var(--mono); word-break: break-all; }
.card-top { display: flex; justify-content: space-between; gap: 8px; align-items: start; }
.tag {
  display: inline-flex;
  padding: 3px 7px;
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: 9px;
  text-transform: uppercase;
}
.tag.healthy, .tag.success, .tag.ready { color: var(--good); border-color: rgba(155,226,127,.4); }
.tag.running { color: var(--signal); border-color: rgba(98,230,196,.42); }
.tag.attention, .tag.partial, .tag.stale, .tag.skipped { color: var(--warn); border-color: rgba(240,179,95,.42); }
.tag.failed, .tag.error, .tag.not_ready { color: var(--bad); border-color: rgba(255,119,109,.45); }
.metric {
  min-height: 150px;
  padding: 20px;
  border: 1px solid var(--line);
  background: rgba(12,16,19,.84);
}
.metric strong { display: block; font: 650 clamp(34px, 5vw, 58px)/1 var(--mono); letter-spacing: -.07em; }
.metric span { display: block; margin-top: 18px; color: var(--muted); font-size: 11px; }
.toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(130px, .3fr)) auto;
  gap: 8px;
  margin-bottom: 10px;
}
.field {
  width: 100%;
  min-height: 42px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: #090d0f;
  color: var(--paper);
  padding: 9px 12px;
  outline: 0;
}
.field::placeholder { color: var(--faint); }
.control {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 12px;
  min-height: 44px;
  padding: 8px 14px 8px 48px;
  border: 1px solid #34423f;
  border-radius: 0;
  background: linear-gradient(180deg, #12181b, #090d0f);
  color: var(--paper);
  cursor: pointer;
  clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px));
  text-align: left;
  transition: border-color .18s ease, background .18s ease, transform .1s ease;
}
.control::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 34px;
  border-right: 1px solid #34423f;
  background: linear-gradient(180deg, #172024, #0b1012);
}
.control::after {
  content: "";
  position: absolute;
  left: 12px;
  top: 50%;
  width: 8px;
  height: 8px;
  transform: translateY(-50%);
  border: 1px solid #657772;
  background: #121a1d;
  box-shadow: inset 0 0 0 2px #080b0d;
}
.control:hover { border-color: var(--signal); background: #121b1e; }
.control:hover::after, .control.primary::after {
  border-color: var(--signal);
  background: var(--signal);
  box-shadow: inset 0 0 0 2px #07100e, 0 0 12px rgba(98,230,196,.34);
}
.control:active { transform: translateY(1px); }
.control.primary { border-color: rgba(98,230,196,.58); background: linear-gradient(90deg, rgba(98,230,196,.12), #0a1012 52%); }
.control.danger:hover { border-color: var(--bad); }
.control.danger:hover::after { border-color: var(--bad); background: var(--bad); box-shadow: inset 0 0 0 2px #150807, 0 0 10px rgba(255,119,109,.28); }
.control:disabled { opacity: .42; cursor: not-allowed; transform: none; }
.control strong { display: block; font: 620 11px var(--display); letter-spacing: .035em; }
.control small { display: block; color: var(--faint); font: 7px var(--mono); letter-spacing: .12em; }
.row-list { border: 1px solid var(--line); background: rgba(12,16,19,.78); }
.row {
  display: grid;
  grid-template-columns: minmax(160px, .8fr) minmax(220px, 1.6fr) minmax(120px, .5fr) auto;
  gap: 14px;
  align-items: center;
  min-height: 66px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}
.row:last-child { border-bottom: 0; }
button.row { width: 100%; border-left: 0; border-right: 0; border-top: 0; color: inherit; background: transparent; cursor: pointer; text-align: left; }
button.row:hover { background: var(--deck-3); }
.row strong { overflow-wrap: anywhere; }
.row p { margin: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.row .mono { color: var(--faint); font-size: 9px; word-break: break-all; }
.stage-rail { display: grid; gap: 1px; border: 1px solid var(--line); background: var(--line); }
.stage {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  min-height: 76px;
  background: #0b1012;
  padding: 13px;
}
.stage-index { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--line); color: var(--faint); font: 10px var(--mono); }
.stage.running .stage-index { color: var(--signal); border-color: var(--signal); }
.stage.success .stage-index { color: var(--good); border-color: var(--good); }
.stage.failed .stage-index { color: var(--bad); border-color: var(--bad); }
.stage h4 { margin: 0 0 3px; }
.stage p { margin: 0; color: var(--muted); font-size: 11px; }
.stage time { color: var(--faint); font: 9px var(--mono); text-align: right; }
.empty, .loading, .error-box {
  min-height: 180px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--line);
  color: var(--muted);
  text-align: center;
  padding: 26px;
}
.error-box { color: var(--bad); border-color: rgba(255,119,109,.36); }
.skeleton { position: relative; overflow: hidden; background: #101619; }
.skeleton::after { content: ""; position: absolute; inset: 0; transform: translateX(-100%); background: linear-gradient(90deg, transparent, rgba(98,230,196,.08), transparent); animation: scan 1.2s linear infinite; }
.pagination { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-top: 10px; color: var(--muted); }
.drawer {
  position: fixed;
  z-index: 30;
  top: 0;
  right: 0;
  width: min(560px, 94vw);
  height: 100vh;
  border-left: 1px solid var(--line-hot);
  background: rgba(8,12,14,.985);
  box-shadow: -30px 0 90px rgba(0,0,0,.45);
  transform: translateX(105%);
  transition: transform .26s var(--ease);
  overflow: auto;
}
.drawer.open { transform: translateX(0); }
.drawer[aria-hidden="true"] { display: none; }
.drawer-head { position: sticky; top: 0; display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 18px; border-bottom: 1px solid var(--line); background: rgba(8,12,14,.96); }
.drawer-body { padding: 20px; }
.drawer-body h3 { font: 620 28px var(--display); }
.detail-block { margin: 14px 0; padding: 14px; border: 1px solid var(--line); background: #0c1113; }
.detail-block label { display: block; margin-bottom: 8px; color: var(--signal); font: 8px var(--mono); letter-spacing: .14em; }
.detail-block pre { margin: 0; white-space: pre-wrap; word-break: break-word; color: #bfd0cb; font: 11px/1.6 var(--mono); }
.dialog {
  width: min(520px, calc(100vw - 28px));
  border: 1px solid var(--line-hot);
  padding: 0;
  color: var(--paper);
  background: #0b1012;
  box-shadow: 0 36px 120px rgba(0,0,0,.7);
}
.dialog::backdrop { background: rgba(0,0,0,.76); backdrop-filter: blur(3px); }
.dialog-head { padding: 20px; border-bottom: 1px solid var(--line); }
.dialog-head .micro { color: var(--signal); font-size: 8px; letter-spacing: .18em; }
.dialog-head h3 { margin: 8px 0 0; font: 620 28px var(--display); }
.dialog-body { padding: 20px; color: var(--muted); }
.dialog-impact { margin: 14px 0 0; padding: 12px; border-left: 2px solid var(--warn); background: rgba(240,179,95,.05); color: #c6b89d; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 16px 20px; border-top: 1px solid var(--line); }
.toast {
  position: fixed;
  z-index: 50;
  right: 22px;
  bottom: 22px;
  max-width: 420px;
  padding: 13px 16px;
  border: 1px solid var(--line-hot);
  background: #101719;
  color: var(--paper);
  box-shadow: 0 18px 60px rgba(0,0,0,.42);
  transform: translateY(30px);
  opacity: 0;
  pointer-events: none;
  transition: .22s var(--ease);
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast.error { border-color: var(--bad); color: var(--bad); }
.footer { display: flex; justify-content: space-between; gap: 16px; margin-top: 26px; padding-top: 15px; border-top: 1px solid var(--line); color: var(--faint); font: 9px var(--mono); }
@keyframes beacon {
  0%,100% { box-shadow: 0 0 0 5px rgba(98,230,196,.06), 0 0 12px rgba(98,230,196,.15); }
  50% { box-shadow: 0 0 0 9px rgba(98,230,196,.025), 0 0 28px rgba(98,230,196,.32); }
}
@keyframes scan { to { transform: translateX(100%); } }
@media (max-width: 1180px) {
  .nav { grid-template-columns: repeat(4, 1fr); }
  .grid-5 { grid-template-columns: repeat(3, 1fr); }
  .grid-4 { grid-template-columns: repeat(2, 1fr); }
  .hero { grid-template-columns: 1fr; }
  .hero-side { border-left: 0; border-top: 1px solid var(--line); }
}
@media (max-width: 760px) {
  .app { padding: 12px; }
  .masthead { grid-template-columns: 1fr auto; }
  .system-ribbon { grid-column: 1 / -1; grid-row: 2; justify-content: flex-start; padding-bottom: 10px; }
  .head-tools .version { display: none; }
  .nav { grid-template-columns: repeat(2, 1fr); }
  .view-head { display: block; }
  .view-meta { margin-top: 10px; text-align: left; }
  .grid-2, .grid-3, .grid-4, .grid-5 { grid-template-columns: 1fr; }
  .toolbar { grid-template-columns: 1fr; }
  .row { grid-template-columns: 1fr; gap: 5px; }
  .stage { grid-template-columns: 36px 1fr; }
  .stage time { grid-column: 2; text-align: left; }
  .hero-side { grid-template-columns: 1fr 1fr; }
  .hero-stat { min-height: 130px; }
  .footer { flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
@media print {
  :root { --void:#fff; --deck:#fff; --deck-2:#fff; --deck-3:#fff; --paper:#111; --muted:#444; --faint:#666; --line:#bbb; }
  body { background:#fff; }
  .app { width:100%; padding:0; }
  .nav, .head-tools, .toolbar, .control, .drawer, .dialog, .toast { display:none !important; }
  .card, .metric, .hero, .row-list { background:#fff; box-shadow:none; break-inside:avoid; }
  *, *::before, *::after { animation:none !important; transition:none !important; box-shadow:none !important; }
}
</style>
</head>
<body>
<div class="app">
  <header class="masthead">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"></span>
      <div><div class="eyebrow">IMMORTAL MEMORY</div><h1>CONTROL CENTER</h1></div>
    </div>
    <div class="system-ribbon"><span class="beacon unknown" id="systemBeacon"></span><span id="systemText">正在确认本机能力</span></div>
    <div class="head-tools">
      <span class="version" id="version">v-</span>
      <button class="control" id="printBtn"><span><strong>打印</strong><small>PRINT VIEW</small></span></button>
      <button class="control primary" id="refreshBtn" aria-busy="false"><span><strong>刷新</strong><small>SYNC ACTIVE</small></span></button>
    </div>
  </header>

  <nav class="nav" aria-label="产品模块">
    <button class="nav-button" data-view="overview"><strong>总览</strong><small>OVERVIEW</small></button>
    <button class="nav-button" data-view="runs"><strong>运行中心</strong><small>RUNS</small></button>
    <button class="nav-button" data-view="sources"><strong>数据源</strong><small>SOURCES</small></button>
    <button class="nav-button" data-view="memories"><strong>记忆浏览</strong><small>MEMORIES</small></button>
    <button class="nav-button" data-view="profile"><strong>长期画像</strong><small>PROFILE</small></button>
    <button class="nav-button" data-view="agent"><strong>Agent 接入</strong><small>AGENT</small></button>
    <button class="nav-button" data-view="backup"><strong>备份恢复</strong><small>BACKUP</small></button>
    <button class="nav-button" data-view="diagnostics"><strong>系统诊断</strong><small>DIAGNOSTICS</small></button>
  </nav>

  <main class="view" id="viewRoot" tabindex="-1">
    <div class="loading skeleton">正在读取真实证据</div>
  </main>

  <footer class="footer"><span>LOCAL ONLY · EVIDENCE BEFORE GREEN</span><span id="footerTime">等待首次读取</span></footer>
</div>

<aside class="drawer" id="drawer" aria-label="详情面板" aria-hidden="true">
  <div class="drawer-head"><div><div class="micro">DETAIL INSPECTOR</div><strong id="drawerTitle">详情</strong></div><button class="control" id="drawerClose"><span><strong>关闭</strong><small>ESC</small></span></button></div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>

<dialog class="dialog" id="confirmDialog" role="dialog" aria-modal="true" aria-labelledby="dialogTitle">
  <div class="dialog-head"><div class="micro">CONTROLLED LOCAL ACTION</div><h3 id="dialogTitle">确认操作</h3></div>
  <div class="dialog-body"><p id="dialogCopy"></p><div class="dialog-impact" id="dialogImpact"></div></div>
  <div class="dialog-actions">
    <button class="control" id="dialogCancel"><span><strong>取消</strong><small>RETURN</small></span></button>
    <button class="control primary" id="dialogConfirm"><span><strong>确认执行</strong><small>LOCAL ONLY</small></span></button>
  </div>
</dialog>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const VIEWS = ['overview','runs','sources','memories','profile','agent','backup','diagnostics'];
const VIEW_META = {
  overview:['系统总览','判断系统是否真的健康，并展示每个结论的证据来源。','SYSTEM VERDICT'],
  runs:['运行中心','查看真实阶段、任务状态、脱敏日志，并安全取消或重试。','VERIFIABLE JOBS'],
  sources:['数据源','区分成功、部分完成、跳过、限流和权限不足。','SOURCE TRUTH'],
  memories:['记忆浏览','服务端筛选和分页，列表不预载完整正文。','BOUNDED RECALL'],
  profile:['长期画像','敏感候选默认遮罩，操作全部留下无正文审计记录。','PRIVACY REVIEW'],
  agent:['Agent 接入','通过固定字段生成任务上下文，不接受任意命令。','CONTEXT BRIDGE'],
  backup:['备份恢复','只校验，不在网页执行恢复、删除或清理。','RECOVERY BOUNDARY'],
  diagnostics:['系统诊断','查看版本、监听地址、依赖和只读运行边界。','LOCAL DIAGNOSTICS']
};
let activeView = 'overview';
let capabilities = null;
let pendingAction = null;
let refreshTimer = null;
let memoryOffset = 0;
let profileOffset = 0;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => Number(value || 0).toLocaleString('zh-CN');
const when = value => {
  if (!value) return '无时间证据';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {hour12:false});
};
const statusLabel = value => ({
  healthy:'健康',success:'成功',ready:'就绪',running:'运行中',attention:'需关注',
  partial:'部分完成',failed:'失败',error:'错误',unknown:'证据不足',stale:'陈旧',
  skipped:'已跳过',not_ready:'未就绪',queued:'排队中',interrupted:'被服务重启中断',
  canceled:'已取消',cancel_requested:'等待安全取消'
}[value] || value || '未知');
const duration = seconds => {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h ? `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
};
const tag = status => `<span class="tag ${esc(status)}">${esc(statusLabel(status))}</span>`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache:'no-store',
    ...options,
    headers:{'Content-Type':'application/json', ...(options.headers || {})}
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) {
    const error = payload.error || {};
    throw new Error(error.message || `HTTP ${response.status}`);
  }
  return payload;
}

function viewHead(view, extra = '') {
  const [title, copy, code] = VIEW_META[view];
  return `<header class="view-head"><div><div class="view-kicker">${esc(code)}</div><h2>${esc(title)}</h2><p>${esc(copy)}</p></div><div class="view-meta">${extra || 'LOCAL API · LIVE DATA'}</div></header>`;
}

function toast(message, error = false) {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'toast'; }, 2600);
}

function setSystem(status, text, version = '') {
  $('systemBeacon').className = `beacon ${status || 'unknown'}`;
  $('systemText').textContent = text;
  if (version) $('version').textContent = `v${version}`;
}

function showDrawer(title, body) {
  $('drawerTitle').textContent = title;
  $('drawerBody').innerHTML = body;
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
}

function detailBlocks(value) {
  return Object.entries(value || {}).map(([key, item]) => {
    const rendered = typeof item === 'object' ? JSON.stringify(item, null, 2) : String(item ?? '');
    return `<div class="detail-block"><label>${esc(key)}</label><pre>${esc(rendered)}</pre></div>`;
  }).join('');
}

function confirmAction({title, copy, impact, execute}) {
  pendingAction = execute;
  $('dialogTitle').textContent = title;
  $('dialogCopy').textContent = copy;
  $('dialogImpact').textContent = impact;
  $('confirmDialog').showModal();
}

async function runPendingAction() {
  if (!pendingAction) return;
  const execute = pendingAction;
  pendingAction = null;
  $('dialogConfirm').disabled = true;
  try {
    await execute();
    $('confirmDialog').close();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $('dialogConfirm').disabled = false;
  }
}

function actionButton(action, label, meta, primary = false, danger = false) {
  return `<button class="control${primary ? ' primary' : ''}${danger ? ' danger' : ''}" data-job-action="${esc(action)}"><span><strong>${esc(label)}</strong><small>${esc(meta)}</small></span></button>`;
}

async function renderOverview() {
  const data = await api('/api/v1/overview');
  setSystem(data.status, `${data.status_label} · ${when(data.generated_at)}`, data.version);
  const run = data.current_run || {};
  const current = (run.stages || []).find(item => item.id === run.current_stage);
  const proofCards = (data.proofs || []).map((item, index) => `
    <button class="card" data-detail-kind="json" data-detail-title="${esc(item.label)}" data-detail="${encodeURIComponent(JSON.stringify(item))}">
      <div class="card-top"><span class="mono">P${String(index + 1).padStart(2,'0')}</span>${tag(item.status)}</div>
      <h4>${esc(item.label)}</h4><p>${esc(item.detail)}</p><div class="source">${esc(item.source)}</div>
    </button>`).join('');
  const stages = (run.stages || []).map((item, index) => `
    <div class="stage ${esc(item.status)}"><div class="stage-index">${String(index + 1).padStart(2,'0')}</div>
      <div><h4>${esc(item.label || item.id)}</h4><p>${esc(item.error || item.summary || statusLabel(item.status))}</p></div>
      <time>${esc(statusLabel(item.status))}<br>${item.elapsed_seconds == null ? when(item.started_at) : duration(item.elapsed_seconds)}</time></div>`).join('');
  const attentions = (data.attention || []).map(item => `
    <button class="row" data-detail-kind="json" data-detail-title="${esc(item.label)}" data-detail="${encodeURIComponent(JSON.stringify(item))}">
      <strong>${esc(item.label)}</strong><p>${esc(item.detail)}</p>${tag(item.status)}<span class="mono">${esc(item.source)}</span></button>`).join('');
  $('viewRoot').innerHTML = `${viewHead('overview', `SNAPSHOT · ${esc(when(data.generated_at))}`)}
    <section class="hero"><div class="hero-main"><div class="micro">SYSTEM VERDICT</div><div class="verdict">${esc(data.status_label)}</div><p class="hero-copy">${esc(data.status === 'healthy' ? '关键证据均在有效期内。健康结论来自真实运行、调度器、结果与备份证明。' : '系统没有把未知或部分完成伪装成健康。请按下方证据和风险逐项检查。')}</p></div>
      <div class="hero-side">
        <div class="hero-stat"><span>NEW RECORDS</span><strong>${num(data.metrics?.new_records)}</strong></div>
        <div class="hero-stat"><span>TOTAL RECORDS</span><strong>${num(data.metrics?.total_records)}</strong></div>
        <div class="hero-stat"><span>CURRENT STAGE</span><b>${esc(current?.label || statusLabel(run.status))}</b></div>
        <div class="hero-stat"><span>QUALITY SCORE</span><strong>${num(data.quality?.score)}</strong></div>
      </div></section>
    <div class="section-title"><div><h3>运行证明</h3><p>点击任意证明查看原始字段和证据位置。</p></div><span class="micro">PROOF STRIP</span></div>
    <section class="grid grid-5">${proofCards || '<div class="empty">没有证明数据</div>'}</section>
    <div class="section-title"><div><h3>安全控制</h3><p>所有写操作先确认，再进入可恢复的任务状态机。</p></div></div>
    <section class="grid grid-4">
      ${actionButton('run','立即运行全流程','RUN · 7 STAGES',true)}
      ${actionButton('health','运行健康检查','VERIFY · HEALTH')}
      ${actionButton('backup_verify','校验最新备份','VERIFY · BACKUP')}
      ${actionButton('profile_refresh','刷新长期画像','REFRESH · PROFILE')}
    </section>
    <div class="section-title"><div><h3>当前阶段</h3><p>阶段、耗时和错误来自 runtime/current_run.json。</p></div></div>
    <section class="stage-rail">${stages || '<div class="empty">暂无结构化阶段记录</div>'}</section>
    <div class="section-title"><div><h3>风险与建议</h3><p>未知、陈旧、部分完成和失败保持独立语义。</p></div></div>
    <section class="row-list">${attentions || '<div class="empty">当前没有待处理项</div>'}</section>`;
}

async function renderRuns() {
  const [jobs, overview] = await Promise.all([api('/api/v1/jobs'), api('/api/v1/overview')]);
  setSystem(overview.status, `${overview.status_label} · 任务 ${jobs.items.length}`, overview.version);
  const current = overview.current_run || {};
  const rows = jobs.items.map(item => `
    <button class="row" data-job-id="${esc(item.id)}">
      <strong>${esc(item.kind)}<br><span class="mono">${esc(item.id)}</span></strong>
      <p>${esc(item.summary || item.error || '任务已记录，等待进一步证据')}</p>
      ${tag(item.status)}
      <span class="mono">${esc(when(item.started_at || item.created_at))}<br>${item.elapsed_seconds == null ? '' : duration(item.elapsed_seconds)}</span>
    </button>`).join('');
  const currentStages = (current.stages || []).map((item, index) => `
    <div class="stage ${esc(item.status)}"><div class="stage-index">${String(index + 1).padStart(2,'0')}</div><div><h4>${esc(item.label || item.id)}</h4><p>${esc(item.summary || item.error || '')}</p></div><time>${esc(statusLabel(item.status))}</time></div>`).join('');
  $('viewRoot').innerHTML = `${viewHead('runs', `${jobs.items.length} JOBS · ${esc(statusLabel(current.status))}`)}
    <div class="section-title"><div><h3>当前编排器运行</h3><p>心跳、阶段和 run_id 来自结构化遥测。</p></div></div>
    <section class="stage-rail">${currentStages || '<div class="empty">当前没有运行中的阶段</div>'}</section>
    <div class="section-title"><div><h3>控制任务</h3><p>点击任务查看分页日志、取消或重试。</p></div></div>
    <section class="row-list">${rows || '<div class="empty">还没有控制任务</div>'}</section>`;
}

async function renderSources() {
  const data = await api('/api/v1/sources');
  const cards = data.items.map(item => `
    <button class="card" data-detail-kind="json" data-detail-title="${esc(item.label)}" data-detail="${encodeURIComponent(JSON.stringify(item))}">
      <div class="card-top"><span class="mono">${esc(item.id)}</span>${tag(item.status)}</div>
      <h4>${esc(item.label)}</h4>
      <p>最近尝试：${esc(when(item.last_attempt))}</p>
      <p>本轮增量：${num(item.increment)} · 错误：${num(item.error_count)}</p>
      <div class="source">${esc(item.evidence)} · ${esc(item.freshness)}</div>
    </button>`).join('');
  setSystem('ready', `已读取 ${data.items.length} 个数据源`);
  $('viewRoot').innerHTML = `${viewHead('sources', `UPDATED · ${esc(when(data.generated_at))}`)}<section class="grid grid-5">${cards}</section>`;
}

async function renderMemories() {
  const q = $('memoryQuery')?.value || '';
  const source = $('memorySource')?.value || '';
  const params = new URLSearchParams({limit:'20', offset:String(memoryOffset)});
  if (q) params.set('q', q);
  if (source) params.set('source', source);
  const data = await api(`/api/v1/memories?${params}`);
  const rows = data.items.map(item => `
    <button class="row" data-memory-id="${esc(item.id)}">
      <strong>${esc(item.summary || '无摘要')}</strong>
      <p>${esc(item.source)} · ${esc(item.kind)}</p>
      <span class="tag">${esc(item.sensitivity)}</span>
      <span class="mono">${esc(when(item.timestamp))}</span>
    </button>`).join('');
  setSystem('ready', `匹配 ${num(data.total)} 条记忆`);
  const indexNotice = data.index_fresh ? '' : `<div class="error-box">检索索引落后 ${num(data.index_lag_bytes)} B。列表仍可读取，但最新记录需等待下一次索引同步。</div>`;
  $('viewRoot').innerHTML = `${viewHead('memories', `${esc((data.backend || 'unknown').toUpperCase())} · ${data.index_fresh ? 'INDEX FRESH' : 'INDEX LAG'}`)}
    ${indexNotice}
    <div class="toolbar">
      <input class="field" id="memoryQuery" value="${esc(q)}" placeholder="搜索记忆摘要和正文">
      <select class="field" id="memorySource"><option value="">全部来源</option><option value="codex"${source==='codex'?' selected':''}>Codex</option><option value="feishu-im"${source==='feishu-im'?' selected':''}>飞书 IM</option><option value="claude-code"${source==='claude-code'?' selected':''}>Claude Code</option></select>
      <span></span><span></span>
      <button class="control primary" id="memorySearch"><span><strong>查询</strong><small>SERVER FILTER</small></span></button>
    </div>
    <section class="row-list">${rows || '<div class="empty">没有匹配记忆</div>'}</section>
    <div class="pagination"><span>第 ${num(data.offset + 1)} 至 ${num(Math.min(data.total, data.offset + data.limit))} 条，共 ${num(data.total)} 条</span><div>
      <button class="control" id="memoryPrev" ${data.offset===0?'disabled':''}><span><strong>上一页</strong><small>PREV</small></span></button>
      <button class="control" id="memoryNext" ${!data.has_more?'disabled':''}><span><strong>下一页</strong><small>NEXT</small></span></button>
    </div></div>`;
}

async function renderProfile() {
  const q = $('profileQuery')?.value || '';
  const state = $('profileState')?.value || '';
  const params = new URLSearchParams({limit:'20', offset:String(profileOffset), sort:'priority'});
  if (q) params.set('q', q);
  if (state) params.set('state', state);
  const data = await api(`/api/v1/profile/candidates?${params}`);
  const rows = data.items.map(item => `
    <button class="row" data-profile-id="${esc(item.id)}">
      <strong>${esc(item.masked ? item.summary : item.statement || '无陈述')}</strong>
      <p>${esc(item.focus)} · ${esc(item.memory_type)} · ${esc(item.source_title)}</p>
      ${tag(item.review_state)}
      <span class="mono">${esc(item.sensitivity)}${item.masked ? ' · MASKED' : ''}</span>
    </button>`).join('');
  setSystem('ready', `画像候选 ${num(data.total)} 条`);
  $('viewRoot').innerHTML = `${viewHead('profile', `PENDING ${num(data.counts.pending)} · SELECTED ${num(data.counts.selected)}`)}
    <div class="toolbar">
      <input class="field" id="profileQuery" value="${esc(q)}" placeholder="搜索候选、项目或人物">
      <select class="field" id="profileState"><option value="">全部状态</option><option value="pending"${state==='pending'?' selected':''}>待处理</option><option value="selected"${state==='selected'?' selected':''}>待合并</option><option value="rejected"${state==='rejected'?' selected':''}>已跳过</option><option value="merged"${state==='merged'?' selected':''}>已合并</option></select>
      <span></span><button class="control" data-profile-merge><span><strong>合并已选项</strong><small>AUDITED MERGE</small></span></button>
      <button class="control primary" id="profileSearch"><span><strong>查询</strong><small>SERVER FILTER</small></span></button>
    </div>
    <section class="row-list">${rows || '<div class="empty">没有匹配候选</div>'}</section>
    <div class="pagination"><span>第 ${num(data.offset + 1)} 至 ${num(Math.min(data.total, data.offset + data.limit))} 条，共 ${num(data.total)} 条</span><div>
      <button class="control" id="profilePrev" ${data.offset===0?'disabled':''}><span><strong>上一页</strong><small>PREV</small></span></button>
      <button class="control" id="profileNext" ${!data.has_more?'disabled':''}><span><strong>下一页</strong><small>NEXT</small></span></button>
    </div></div>`;
}

async function renderAgent() {
  const data = await api('/api/v1/agent');
  const rows = data.contexts.map(item => `
    <button class="row" data-context-id="${esc(item.slug)}"><strong>${esc(item.goal)}</strong><p>${esc(item.mode || 'auto')}</p>${tag(item.status)}<span class="mono">${esc(when(item.generated_at))}</span></button>`).join('');
  setSystem(data.available ? 'ready' : 'attention', data.available ? 'Agent 入口已就绪' : 'Agent 入口尚未生成');
  $('viewRoot').innerHTML = `${viewHead('agent', `ENTRY · ${data.entry.exists ? 'READY' : 'MISSING'}`)}
    <section class="grid grid-2">
      <div class="card"><div class="card-top"><span class="mono">ENTRY.md</span>${tag(data.entry.exists ? 'ready' : 'unknown')}</div><h4>稳定 Agent 入口</h4><p>${data.entry.exists ? `更新时间 ${esc(when(data.entry.updated_at))} · ${num(data.entry.bytes)} B` : '读取页面不会自动生成文件。'}</p></div>
      <form class="card" id="contextForm"><div class="card-top"><span class="mono">CONTEXT COMPILER</span>${tag('ready')}</div><h4>生成任务上下文</h4><p>只接受目标和固定模式，不接受命令字符串。</p><input class="field" id="contextGoal" required maxlength="160" placeholder="例如：为发布 v1.0.0 做最终审查"><select class="field" id="contextMode"><option value="auto">自动</option><option value="answer">回答</option><option value="code">开发</option><option value="research">研究</option><option value="plan">规划</option></select><button class="control primary" type="submit"><span><strong>生成上下文</strong><small>CONTROLLED JOB</small></span></button></form>
    </section>
    <div class="section-title"><div><h3>最近上下文</h3><p>这里只展示元数据，不返回完整 vault。</p></div></div>
    <section class="row-list">${rows || '<div class="empty">还没有任务上下文</div>'}</section>`;
}

async function renderBackup() {
  const data = await api('/api/v1/backups');
  const cards = data.items.map(item => `
    <button class="card" data-detail-kind="json" data-detail-title="${esc(item.id)}" data-detail="${encodeURIComponent(JSON.stringify(item))}">
      <div class="card-top"><span class="mono">${esc(item.id)}</span>${tag(item.status)}</div><h4>${item.verified ? '恢复校验已通过' : '尚未建立恢复可信度'}</h4>
      <p>${esc(when(item.generated_at))} · ${num(item.files)} 个文件 · ${num(item.bytes)} B</p><div class="source">${esc((item.risks || []).join(' · ') || 'NO KNOWN RISK')}</div>
    </button>`).join('');
  setSystem(data.items.some(item => item.status === 'attention') ? 'attention' : 'ready', `${data.items.length} 个便携备份`);
  $('viewRoot').innerHTML = `${viewHead('backup', `RESTORE ${data.restore_available ? 'ENABLED' : 'WEB DISABLED'}`)}
    <section class="grid grid-3">${cards || '<div class="empty">没有找到便携备份</div>'}</section>
    <div class="section-title"><div><h3>受控操作</h3><p>网页只提供完整校验。恢复、删除和清理必须离线操作。</p></div></div>
    ${actionButton('backup_verify','校验最新备份','SHA256 · ALL FILES',true)}`;
}

async function renderDiagnostics() {
  const data = await api('/api/v1/diagnostics');
  setSystem(data.ready ? 'ready' : 'not_ready', data.ready ? '依赖检查就绪' : '存在未就绪依赖', data.version);
  const deps = Object.entries(data.dependencies || {}).map(([name, ok]) => `<div class="card"><div class="card-top"><span class="mono">${esc(name)}</span>${tag(ok ? 'ready' : 'not_ready')}</div><h4>${ok ? '可用' : '不可用'}</h4></div>`).join('');
  $('viewRoot').innerHTML = `${viewHead('diagnostics', `SERVICE START · ${esc(when(data.service_started_at))}`)}
    <section class="grid grid-4">
      <div class="metric"><strong>${esc(data.version)}</strong><span>Immortal 版本</span></div>
      <div class="metric"><strong>${esc(data.listen_port)}</strong><span>${esc(data.listen_address)} 本机监听</span></div>
      <div class="metric"><strong>${esc(data.python)}</strong><span>Python Runtime</span></div>
      <div class="metric"><strong>${data.ready ? 'OK' : '!'}</strong><span>Readiness</span></div>
    </section>
    <div class="section-title"><div><h3>依赖检查</h3><p>不返回凭证、正文或私密路径。</p></div></div>
    <section class="grid grid-4">${deps}</section>
    <div class="section-title"><div><h3>调度器边界</h3><p>${esc(data.scheduler.detail)}</p></div></div>`;
}

const RENDERERS = {overview:renderOverview,runs:renderRuns,sources:renderSources,memories:renderMemories,profile:renderProfile,agent:renderAgent,backup:renderBackup,diagnostics:renderDiagnostics};

async function renderActive({quiet = false} = {}) {
  if (!quiet) $('viewRoot').innerHTML = '<div class="loading skeleton">正在读取真实证据</div>';
  try {
    await RENDERERS[activeView]();
    $('footerTime').textContent = `LAST SYNC · ${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
    bindViewEvents();
  } catch (error) {
    $('viewRoot').innerHTML = `${viewHead(activeView)}<div class="error-box">读取失败：${esc(error.message)}</div>`;
    setSystem('failed', `模块读取失败 · ${error.message}`);
  }
}

async function switchView(view, {push = true} = {}) {
  if (!VIEWS.includes(view)) view = 'overview';
  activeView = view;
  document.querySelectorAll('.nav-button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  if (push) {
    const url = new URL(location.href);
    url.searchParams.set('view', view);
    history.pushState({view}, '', url);
  }
  memoryOffset = view === 'memories' ? memoryOffset : 0;
  profileOffset = view === 'profile' ? profileOffset : 0;
  await renderActive();
  $('viewRoot').focus({preventScroll:true});
  clearInterval(refreshTimer);
  if (view === 'overview' || view === 'runs') refreshTimer = setInterval(() => renderActive({quiet:true}), 5000);
}

async function openJob(jobId) {
  const [job, logs] = await Promise.all([api(`/api/v1/jobs/${jobId}`), api(`/api/v1/jobs/${jobId}/logs?limit=8000`)]);
  const actions = [];
  if (['queued','running'].includes(job.status)) actions.push(`<button class="control danger" data-job-cancel="${esc(job.id)}"><span><strong>安全取消</strong><small>STAGE BOUNDARY</small></span></button>`);
  if (['failed','canceled','interrupted','attention'].includes(job.status)) actions.push(`<button class="control" data-job-retry="${esc(job.id)}"><span><strong>重试任务</strong><small>NEW JOB</small></span></button>`);
  showDrawer(`${job.kind} · ${job.id}`, `${detailBlocks({status:job.status, summary:job.summary, error:job.error, created_at:job.created_at, started_at:job.started_at, finished_at:job.finished_at, elapsed_seconds:job.elapsed_seconds, commands:job.commands})}<div class="detail-block"><label>REDACTED LOG</label><pre>${esc(logs.text || '暂无日志')}</pre></div><div class="grid grid-2">${actions.join('')}</div>`);
  bindDrawerEvents();
}

async function openMemory(id) {
  const data = await api(`/api/v1/memories/${encodeURIComponent(id)}`);
  showDrawer(`记忆 · ${id}`, detailBlocks(data));
}

async function openProfile(id) {
  const data = await api(`/api/v1/profile/candidates/${encodeURIComponent(id)}`);
  const actions = [];
  if (data.review_state !== 'merged') {
    actions.push(`<button class="control primary" data-profile-action="approve" data-profile-target="${esc(id)}"><span><strong>批准</strong><small>AUDITED</small></span></button>`);
    actions.push(`<button class="control danger" data-profile-action="reject" data-profile-target="${esc(id)}"><span><strong>跳过</strong><small>RECOVERABLE</small></span></button>`);
    actions.push(`<button class="control" data-profile-action="unapprove" data-profile-target="${esc(id)}"><span><strong>撤回</strong><small>AUDITED</small></span></button>`);
  }
  showDrawer(`画像候选 · ${id}`, `${detailBlocks(data)}<div class="grid grid-3">${actions.join('')}</div>`);
  bindDrawerEvents();
}

async function openContext(id) {
  const data = await api(`/api/v1/agent/contexts/${encodeURIComponent(id)}`);
  showDrawer(`Agent 上下文 · ${id}`, detailBlocks(data));
}

async function submitJob(kind) {
  const job = await api('/api/v1/jobs', {method:'POST', body:JSON.stringify({kind, params:{}})});
  toast(`任务 ${job.id} 已进入队列`);
  await switchView('runs');
  await openJob(job.id);
}

function requestJobAction(kind) {
  const labels = {run:'立即运行全流程',health:'运行健康检查',backup_verify:'校验最新备份',profile_refresh:'刷新长期画像'};
  confirmAction({
    title:labels[kind] || kind,
    copy:'控制中心只会执行固定白名单命令，任务状态、证据和脱敏日志会被持久记录。',
    impact:kind === 'run' ? '全流程可能持续较长时间。已有编排器运行时会返回 409，不会重复启动。' : '操作只作用于本机 Immortal 数据，不上传记忆正文。',
    execute:() => submitJob(kind)
  });
}

function bindDrawerEvents() {
  document.querySelectorAll('[data-job-cancel]').forEach(button => button.onclick = () => confirmAction({
    title:'请求安全取消',
    copy:'不会直接 SIGKILL。编排器会在下一个安全阶段边界停止。',
    impact:'当前阶段可能继续运行一段时间，直到抵达可安全停止的位置。',
    execute:async () => { await api(`/api/v1/jobs/${button.dataset.jobCancel}/cancel`, {method:'POST', body:'{}'}); toast('取消请求已记录'); await openJob(button.dataset.jobCancel); }
  }));
  document.querySelectorAll('[data-job-retry]').forEach(button => button.onclick = () => confirmAction({
    title:'重试任务',
    copy:'系统将创建一个新 job，并保留原任务作为失败或中断证据。',
    impact:'不会覆盖原日志。',
    execute:async () => { const job = await api(`/api/v1/jobs/${button.dataset.jobRetry}/retry`, {method:'POST', body:'{}'}); toast(`新任务 ${job.id} 已创建`); closeDrawer(); await renderActive(); await openJob(job.id); }
  }));
  document.querySelectorAll('[data-profile-action]').forEach(button => button.onclick = () => {
    const action = button.dataset.profileAction;
    confirmAction({
      title:{approve:'批准候选',reject:'跳过候选',unapprove:'撤回批准'}[action] || action,
      copy:'操作会更新可恢复的 review layer，并写入不含正文的审计记录。',
      impact:'confidential 正文不会写入审计日志。',
      execute:async () => { await api(`/api/v1/profile/candidates/${button.dataset.profileTarget}/actions`, {method:'POST', body:JSON.stringify({action})}); toast('画像操作已记录'); closeDrawer(); await renderActive(); }
    });
  });
}

function bindViewEvents() {
  document.querySelectorAll('[data-detail-kind="json"]').forEach(button => button.onclick = () => {
    try { showDrawer(button.dataset.detailTitle || '详情', detailBlocks(JSON.parse(decodeURIComponent(button.dataset.detail)))); } catch (_) {}
  });
  document.querySelectorAll('[data-job-action]').forEach(button => button.onclick = () => requestJobAction(button.dataset.jobAction));
  document.querySelectorAll('[data-job-id]').forEach(button => button.onclick = () => openJob(button.dataset.jobId).catch(error => toast(error.message, true)));
  document.querySelectorAll('[data-memory-id]').forEach(button => button.onclick = () => openMemory(button.dataset.memoryId).catch(error => toast(error.message, true)));
  document.querySelectorAll('[data-profile-id]').forEach(button => button.onclick = () => openProfile(button.dataset.profileId).catch(error => toast(error.message, true)));
  document.querySelectorAll('[data-context-id]').forEach(button => button.onclick = () => openContext(button.dataset.contextId).catch(error => toast(error.message, true)));
  $('memorySearch')?.addEventListener('click', () => { memoryOffset = 0; renderMemories().then(bindViewEvents); });
  $('memoryPrev')?.addEventListener('click', () => { memoryOffset = Math.max(0, memoryOffset - 20); renderMemories().then(bindViewEvents); });
  $('memoryNext')?.addEventListener('click', () => { memoryOffset += 20; renderMemories().then(bindViewEvents); });
  $('profileSearch')?.addEventListener('click', () => { profileOffset = 0; renderProfile().then(bindViewEvents); });
  $('profilePrev')?.addEventListener('click', () => { profileOffset = Math.max(0, profileOffset - 20); renderProfile().then(bindViewEvents); });
  $('profileNext')?.addEventListener('click', () => { profileOffset += 20; renderProfile().then(bindViewEvents); });
  document.querySelector('[data-profile-merge]')?.addEventListener('click', () => confirmAction({
    title:'合并已选画像',
    copy:'将已批准候选合并到 reviewed/profile 层，并刷新长期画像。',
    impact:'操作有审计记录，但会改变真实画像输出。请确认已完成候选审阅。',
    execute:async () => { await api('/api/v1/profile/merge', {method:'POST', body:'{}'}); toast('画像已合并并刷新'); await renderActive(); }
  }));
  $('contextForm')?.addEventListener('submit', event => {
    event.preventDefault();
    const goal = $('contextGoal').value.trim();
    const mode = $('contextMode').value;
    confirmAction({
      title:'生成 Agent 上下文',
      copy:`目标：${goal}`,
      impact:'只会把 goal 和固定 mode 交给白名单 task-compile，不接受命令字符串。',
      execute:async () => { const job = await api('/api/v1/agent/contexts', {method:'POST', body:JSON.stringify({goal,mode})}); toast(`上下文任务 ${job.id} 已创建`); await switchView('runs'); await openJob(job.id); }
    });
  });
}

async function boot() {
  try {
    capabilities = await api('/api/v1/capabilities');
    const available = new Set(capabilities.modules.filter(item => item.available).map(item => item.id));
    document.querySelectorAll('.nav-button').forEach(button => {
      if (!available.has(button.dataset.view)) {
        button.disabled = true;
        button.title = capabilities.modules.find(item => item.id === button.dataset.view)?.reason || '能力不可用';
      }
    });
    const requested = new URL(location.href).searchParams.get('view');
    await switchView(VIEWS.includes(requested) ? requested : 'overview', {push:false});
  } catch (error) {
    $('viewRoot').innerHTML = `<div class="error-box">控制中心启动失败：${esc(error.message)}</div>`;
    setSystem('failed', '控制中心 API 不可用');
  }
}

document.querySelectorAll('.nav-button').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
$('refreshBtn').addEventListener('click', async () => {
  $('refreshBtn').setAttribute('aria-busy','true');
  await renderActive();
  $('refreshBtn').setAttribute('aria-busy','false');
  toast('当前模块已刷新');
});
$('printBtn').addEventListener('click', () => window.print());
$('drawerClose').addEventListener('click', closeDrawer);
$('dialogCancel').addEventListener('click', () => { pendingAction = null; $('confirmDialog').close(); });
$('dialogConfirm').addEventListener('click', runPendingAction);
window.addEventListener('popstate', event => switchView(event.state?.view || new URL(location.href).searchParams.get('view') || 'overview', {push:false}));
window.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
boot();
</script>
</body>
</html>
"""
