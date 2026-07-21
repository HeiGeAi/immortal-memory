import { api } from "../api.js";

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function flatten(value, prefix = "", result = []) {
  if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, child]) => flatten(child, prefix ? `${prefix} · ${key}` : key, result));
  } else {
    result.push([prefix, value === null || value === undefined ? "未知" : String(value)]);
  }
  return result.slice(0, 80);
}

function systemSection(title, value) {
  const section = document.createElement("section");
  section.className = "system-section";
  section.append(node("h2", title));
  const list = document.createElement("dl");
  flatten(value).forEach(([label, content]) => {
    const row = document.createElement("div");
    row.className = "system-row";
    row.append(node("dt", label), node("dd", content));
    list.append(row);
  });
  if (!list.children.length) list.append(node("p", "当前没有可报告的数据。", "state-message"));
  section.append(list);
  return section;
}

export async function renderSystem(root, { signal, isCurrent, navigate, updateHealth }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(node("p", "正在核对系统依据……", "state-message"));
  try {
    const data = await api("/api/v2/system", { signal });
    if (!isCurrent()) return;
    updateHealth(data.health?.status_label || data.health?.status || "连续性未知", data.health?.status);
    const fragment = document.createDocumentFragment();
    const heading = document.createElement("header");
    heading.className = "view-heading";
    heading.append(node("p", "SYSTEM EVIDENCE · 系统依据", "kicker"), node("h1", "健康不是绿灯，是可以复核的依据。"), node("p", "能力、来源、备份与诊断保持分开，避免一个正常信号掩盖另一个缺口。", "lede"));
    fragment.append(heading);
    const grid = document.createElement("div");
    grid.className = "system-grid";
    [["健康", data.health], ["能力", data.capabilities], ["来源", data.sources], ["备份", data.backups], ["诊断", data.diagnostics]].forEach(([title, value]) => grid.append(systemSection(title, value)));
    fragment.append(grid);
    root.replaceChildren(fragment);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    updateHealth("连续性未知", "unknown");
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "系统依据暂时不可读"), node("p", error.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重新核对";
    retry.addEventListener("click", () => navigate("system"));
    failure.append(retry);
    root.replaceChildren(failure);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
