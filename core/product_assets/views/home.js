import { api } from "../api.js";

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = value;
  return node;
}

export async function renderHome(root, { signal, isCurrent, navigate, updateHealth }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(text("p", "正在读取今天的记忆连续性……", "state-message"));
  try {
    const data = await api("/api/v2/home", { signal });
    if (!isCurrent()) return;
    const fragment = document.createDocumentFragment();
    const hero = document.createElement("section");
    hero.className = "archive-hero";
    hero.append(
      text("p", "LIVING RECORD · 此刻", "kicker"),
      text("h1", "你的记忆，没有停在昨天。"),
      text("p", "这里不替你编造完整。它只呈现系统确实保存、能够追溯、仍在延续的部分。", "lede"),
    );
    fragment.append(hero);
    const facts = document.createElement("section");
    facts.className = "fact-grid";
    facts.setAttribute("aria-label", "当前档案摘要");
    const remembered = Array.isArray(data.remembered_today) ? data.remembered_today : [];
    const newest = remembered[0] || {};
    const counts = data.understanding_changes?.counts || {};
    const confirmations = Array.isArray(data.needs_confirmation) ? data.needs_confirmation : [];
    const context = data.latest_context_use || {};
    const outcome = data.latest_outcome || {};
    const health = data.system_health || {};
    updateHealth(health.status_label || health.status || "连续性未知", health.status);
    const fact = (label, value, note) => {
      const card = document.createElement("article");
      card.className = "fact-card";
      card.append(text("span", label, "fact-label"), text("strong", value, "fact-value"), text("small", note, "fact-note"));
      facts.append(card);
    };
    fact("今日记忆", String(remembered.length), newest.timestamp ? `${newest.timestamp} · ${newest.source || "来源未知"}` : "今天尚无索引记录");
    fact("理解变化", String((counts.added || 0) + (counts.changed || 0) + (counts.removed || 0)), `新增 ${counts.added || 0} · 调整 ${counts.changed || 0} · 移除 ${counts.removed || 0}`);
    fact("待确认", String(confirmations.length), confirmations[0]?.summary || "没有待确认项目");
    fact("最近 Context", context.context_id ? "已使用" : "无", context.task || context.goal || context.context_id || "暂无已使用 Context");
    fact("最近 Outcome", outcome.outcome_id ? "已记录" : "无", outcome.summary || outcome.result || outcome.outcome_id || "暂无任务结果");
    fact("系统连续性", health.status_label || health.status || "未知", `版本 ${health.version || "未知"} · 关注项 ${health.attention_count ?? "未知"}`);
    fragment.append(facts);
    const actions = document.createElement("div");
    actions.className = "honest-actions";
    const memoryButton = document.createElement("button");
    memoryButton.type = "button";
    memoryButton.textContent = "进入记忆档案";
    memoryButton.addEventListener("click", () => navigate("memories"));
    const systemButton = document.createElement("button");
    systemButton.type = "button";
    systemButton.className = "secondary";
    systemButton.textContent = "查看系统依据";
    systemButton.addEventListener("click", () => navigate("system"));
    actions.append(memoryButton, systemButton);
    fragment.append(actions);
    root.replaceChildren(fragment);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    updateHealth("连续性未知", "unknown");
    const box = text("div", "", "state-panel error-state");
    box.append(text("h2", "首页暂时无法读取"), text("p", error.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重试";
    retry.addEventListener("click", () => navigate("home"));
    box.append(retry);
    root.replaceChildren(box);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
