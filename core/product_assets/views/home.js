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
    const facts = document.createElement("section");
    facts.className = "fact-grid";
    facts.setAttribute("aria-label", "当前档案摘要");
    const remembered = Array.isArray(data.remembered_today) ? data.remembered_today : [];
    const memorySection = data.memory_evidence || {};
    const memoryStatus = data.memory_evidence?.status;
    const reviewStatus = data.claim_review?.status;
    const agentStatus = data.agent_use?.status;
    const systemStatus = data.system?.status;
    const newest = remembered[0] || {};
    const counts = data.understanding_changes?.counts || {};
    const confirmations = Array.isArray(data.needs_confirmation) ? data.needs_confirmation : [];
    const confirmationTotal = Number(data.confirmation_summary?.total);
    const safeConfirmationTotal = Number.isFinite(confirmationTotal) ? Math.max(0, confirmationTotal) : confirmations.length;
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
    const sectionFact = (status, blockedNote, attentionNote, value, note) => {
      if (status === "blocked") return { value: "不可用", note: blockedNote };
      if (status === "attention") return { value: "需关注", note: attentionNote };
      return { value, note };
    };
    const memoryFact = sectionFact(
      memoryStatus,
      "记忆索引暂不可用",
      "记忆索引需要关注",
      String(remembered.length),
      newest.timestamp ? `${formatTimestamp(newest.timestamp)} · ${newest.source || "来源未知"}` : "今天尚无索引记录",
    );
    fact("今日记忆", memoryFact.value, memoryFact.note);
    const changeFact = sectionFact(
      reviewStatus,
      "审核数据暂不可用",
      "审核数据需要关注",
      String((counts.added || 0) + (counts.changed || 0) + (counts.removed || 0)),
      `新增 ${counts.added || 0} · 调整 ${counts.changed || 0} · 移除 ${counts.removed || 0}`,
    );
    fact("理解变化", changeFact.value, changeFact.note);
    const reviewFact = sectionFact(
      reviewStatus,
      "审核数据暂不可用",
      "审核数据需要关注",
      String(safeConfirmationTotal),
      safeConfirmationTotal > confirmations.length
        ? `当前展示 ${confirmations.length} 条，逐条处理后继续加载`
        : confirmations[0]?.summary || "没有待确认项目",
    );
    fact("待确认", reviewFact.value, reviewFact.note);
    const contextFact = sectionFact(
      agentStatus,
      "Agent 使用记录暂不可用",
      "Agent 使用记录需要关注",
      context.context_id ? "已使用" : "无",
      context.task || context.goal || context.context_id || "暂无已使用 Context",
    );
    fact("最近 Context", contextFact.value, contextFact.note);
    const outcomeFact = sectionFact(
      agentStatus,
      "Agent 使用记录暂不可用",
      "Agent 使用记录需要关注",
      outcome.outcome_id ? "已记录" : "无",
      outcome.summary || outcome.result || outcome.outcome_id || "暂无任务结果",
    );
    fact("最近 Outcome", outcomeFact.value, outcomeFact.note);
    const systemFact = sectionFact(
      systemStatus,
      "系统状态暂不可用",
      "系统状态需要关注",
      health.status_label || health.status || "未知",
      `版本 ${health.version || "未知"} · 关注项 ${health.attention_count ?? "未知"}`,
    );
    fact("系统连续性", systemFact.value, systemFact.note);
    fragment.append(facts);
    const claimConfirmations = reviewStatus === "blocked" ? [] : confirmations.filter((item) => item.kind === "claim");
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
    let recoveryFeedback = null;
    const memoryButton = document.createElement("button");
    memoryButton.type = "button";
    memoryButton.textContent = "进入记忆档案";
    memoryButton.addEventListener("click", () => navigate("memories"));
    if (memoryStatus === "blocked" && memorySection.action === "rebuild_derived_index") {
      const recoveryButton = document.createElement("button");
      recoveryButton.type = "button";
      recoveryButton.textContent = "恢复记忆索引";
      recoveryFeedback = text("p", "", "form-feedback");
      recoveryFeedback.setAttribute("role", "status");
      recoveryButton.addEventListener("click", async () => {
        recoveryButton.disabled = true;
        recoveryButton.className = "is-pending";
        recoveryButton.textContent = "正在建立恢复任务……";
        recoveryFeedback.textContent = "";
        try {
          const job = await api("/api/v1/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind: "index_rebuild", params: {} }),
          });
          if (!isCurrent()) return;
          recoveryButton.className = "is-success";
          recoveryButton.textContent = "恢复任务已建立";
          recoveryFeedback.textContent = `任务 ${job.id} 已建立，正在打开真实运行记录。`;
          navigate("system");
        } catch (error) {
          if (!isCurrent()) return;
          const outcomeUnknown = !error.status || error.status >= 500;
          recoveryButton.className = "is-failure";
          if (outcomeUnknown) {
            recoveryButton.textContent = "提交结果未知，请先核对";
            recoveryFeedback.textContent = "连接中断，任务可能已经建立。为避免重复运行，请到系统页读取最新状态。";
          } else {
            recoveryButton.disabled = false;
            recoveryButton.textContent = "服务端未接收，可重试";
            recoveryFeedback.textContent = error.message || "服务端拒绝了这次恢复。";
          }
        }
      });
      actions.append(recoveryButton);
    }
    const systemButton = document.createElement("button");
    systemButton.type = "button";
    systemButton.className = "secondary";
    systemButton.textContent = "查看系统依据";
    systemButton.addEventListener("click", () => navigate("system"));
    actions.append(memoryButton, systemButton);
    fragment.append(actions);
    if (recoveryFeedback) fragment.append(recoveryFeedback);
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
