#!/usr/bin/env python3
"""Semantic shell and packaged asset manifest for the Immortal product."""

from __future__ import annotations

from html import escape
from pathlib import Path


PRODUCT_ASSET_ROOT = Path(__file__).resolve().parent / "product_assets"
PRODUCT_ASSETS = {
    "product.css": "text/css; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "router.js": "text/javascript; charset=utf-8",
    "dialog.js": "text/javascript; charset=utf-8",
    "format.js": "text/javascript; charset=utf-8",
    "views/home.js": "text/javascript; charset=utf-8",
    "views/memories.js": "text/javascript; charset=utf-8",
    "views/system.js": "text/javascript; charset=utf-8",
}

_NAV_ITEMS = (
    ("home", "首页", "NOW"),
    ("memories", "记忆", "ARCHIVE"),
    ("self", "我", "SELF"),
    ("judgments", "判断", "JUDGMENTS"),
    ("use", "使用", "USE"),
    ("trust", "信任", "TRUST"),
    ("system", "系统", "SYSTEM"),
)


def product_page_html(title: str = "Immortal Memory") -> str:
    navigation = "".join(
        f'<a class="nav-link" href="/?view={view}" data-view="{view}">'
        f'<span>{escape(label)}</span><small>{escape(code)}</small></a>'
        for view, label, code in _NAV_ITEMS
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/assets/product.css">
</head>
<body>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <div class="shell">
    <aside class="rail" aria-label="产品导航">
      <a class="brand" href="/?view=home" aria-label="Immortal Memory 首页">
        <span>IMMORTAL</span><small>MEMORY · 活体档案</small>
      </a>
      <nav>{navigation}</nav>
      <div class="rail-foot">
        <span class="signal-dot" aria-hidden="true"></span>
        <span id="global-health" aria-live="polite">连续性尚未核对</span>
      </div>
    </aside>
    <main id="main" tabindex="-1">
      <header id="topbar" class="topbar">
        <p class="eyebrow">MIDNIGHT LIVING ARCHIVE</p>
        <p id="route-status" class="route-status" aria-live="polite"></p>
      </header>
      <section id="view" class="view" aria-live="polite" aria-busy="false"></section>
    </main>
  </div>
  <aside id="drawer" class="drawer" role="dialog" aria-modal="true" aria-label="证据与详情" aria-hidden="true" tabindex="-1"></aside>
  <div id="toast" class="toast" role="status" aria-live="polite"></div>
  <script type="module" src="/assets/app.js"></script>
</body>
</html>"""
