import { api, mutate, explainApiError, createMutationAttempt } from "../api.js";
import { openDialog, closeDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = value;
  return node;
}

function reviewClaim(item, action, trigger, refresh) {
  openDialog(action === "confirm" ? "确认这条理解" : "拒绝这条理解", (body) => {
    const form = text("form", "", "action-form");
    form.append(text("p", item.summary || item.id, "state-message"));
    const label = text("label", "", "field-label");
    label.append(text("span", "说明原因"));
    const reason = document.createElement("textarea");
    reason.name = "reason";
    reason.required = true;
    label.append(reason);
    const submit = text("button", action === "confirm" ? "确认收录" : "确认拒绝");
    submit.type = "submit";
    const feedback = text("p", "", "form-feedback");
    const attempt = createMutationAttempt();
    form.append(label, submit, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const payload = { action, expected_version: item.revision, reason: reason.value };
        await mutate(`/api/v2/claims/${encodeURIComponent(item.id)}/actions`, payload, attempt.options(payload));
        closeDialog();
        await refresh();
      } catch (error) {
        feedback.textContent = explainApiError(error);
        submit.disabled = false;
      }
    });
    body.append(form);
  }, trigger);
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
    const migration = data.migration || {};
    const migrationRequired = migration.status === "migration_required";
    if (migrationRequired) {
      const gate = document.createElement("section");
      gate.className = "state-panel";
      gate.dataset.status = "attention";
      gate.append(
        text("p", "SAFE UPGRADE GATE · 安全升级门禁", "kicker"),
        text("h2", migration.status_label || "受信索引待建立"),
        text("p", migration.detail || "为保护来源可追溯性，产品不会把旧索引伪装成空记忆库。", "coverage-warning"),
        text("p", `下一步：${migration.next_step || "先完成恢复演练和隔离升级核对。"}`, "state-message"),
      );
      const gateActions = document.createElement("div");
      gateActions.className = "honest-actions";
      const gateSystem = document.createElement("button");
      gateSystem.type = "button";
      gateSystem.textContent = "查看系统依据";
      gateSystem.addEventListener("click", () => navigate("system"));
      gateActions.append(gateSystem);
      gate.append(gateActions);
      fragment.append(gate);
    }
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
    fact("今日记忆", migrationRequired ? "未读取" : String(remembered.length), migrationRequired ? "等待 v1.1 受信索引建立" : (newest.timestamp ? `${formatTimestamp(newest.timestamp)} · ${newest.source || "来源未知"}` : "今天尚无索引记录"));
    fact("理解变化", migrationRequired ? "未读取" : String((counts.added || 0) + (counts.changed || 0) + (counts.removed || 0)), migrationRequired ? "升级前不推断变化数量" : `新增 ${counts.added || 0} · 调整 ${counts.changed || 0} · 移除 ${counts.removed || 0}`);
    fact("待确认", migrationRequired ? "未读取" : String(confirmations.length), migrationRequired ? "升级前不推断待确认项目" : (confirmations[0]?.summary || "没有待确认项目"));
    fact("最近 Context", migrationRequired ? "未读取" : (context.context_id ? "已使用" : "无"), migrationRequired ? "升级前不推断 Context 使用记录" : (context.task || context.goal || context.context_id || "暂无已使用 Context"));
    fact("最近 Outcome", migrationRequired ? "未读取" : (outcome.outcome_id ? "已记录" : "无"), migrationRequired ? "升级前不推断任务结果" : (outcome.summary || outcome.result || outcome.outcome_id || "暂无任务结果"));
    fact("系统连续性", health.status_label || health.status || "未知", `版本 ${health.version || "未知"} · 关注项 ${health.attention_count ?? "未知"}`);
    fragment.append(facts);
    const claimConfirmations = confirmations.filter((item) => item.kind === "claim");
    if (claimConfirmations.length) {
      const review = text("section", "", "confirmation-list");
      review.append(text("h2", "等待你确认的理解"), text("p", "只有你确认后，它才会进入 Living Self。", "state-message"));
      claimConfirmations.forEach((item) => {
        const card = text("article", "", "context-card confirmation-card");
        card.append(text("p", item.summary || item.id));
        const actions = text("div", "", "honest-actions");
        const confirm = text("button", "确认收录");
        confirm.type = "button";
        confirm.addEventListener("click", () => reviewClaim(item, "confirm", confirm, () => navigate("home")));
        const reject = text("button", "拒绝", "secondary");
        reject.type = "button";
        reject.addEventListener("click", () => reviewClaim(item, "reject", reject, () => navigate("home")));
        actions.append(confirm, reject);
        card.append(actions);
        review.append(card);
      });
      fragment.append(review);
    }
    const actions = document.createElement("div");
    actions.className = "honest-actions";
    const memoryButton = document.createElement("button");
    memoryButton.type = "button";
    memoryButton.textContent = migrationRequired ? "受信索引建立后可查看记忆" : "进入记忆档案";
    if (migrationRequired) {
      memoryButton.disabled = true;
    } else {
      memoryButton.addEventListener("click", () => navigate("memories"));
    }
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
