import { api } from "../api.js";

const CATEGORY_LABELS = {
  unknown_speaker: "说话人未知",
  other_view_candidate: "他人观点待确认",
  missing_evidence: "缺少支持证据",
  low_confidence: "证据置信度偏低",
  expired_model: "理解已经过期",
  conflict: "存在冲突或反例",
  source_broken: "来源链路异常",
  privacy_exclusion: "隐私策略排除",
  recent_correction: "最近纠正或替换",
  model_evaluation: "自我模型评估",
  failed_outcome: "任务结果提出复核",
};

const COVERAGE_LABELS = {
  complete: "已完整核对",
  partial: "只核对了部分范围",
  unknown: "覆盖范围暂时未知",
};

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function categoryMessage(category) {
  const parsed = Number(category.count);
  const count = Number.isFinite(parsed) ? Math.max(0, parsed) : null;
  const items = Array.isArray(category.items) ? category.items : [];
  if (count === null) return "服务没有给出这一类的可靠数量，当前不能据此判断风险。";
  if (count === 0 && category.coverage === "complete") return "完整覆盖范围内未发现这一类风险。";
  if (count === 0) return "当前可见范围内尚未发现记录，但覆盖并不完整，不能据此判断没有问题。";
  if (!items.length) return `已识别 ${count} 项，但受本页展示预算限制，具体条目没有在此展开。`;
  if (category.truncated) return `共识别 ${count} 项，本页只展示其中 ${items.length} 项。`;
  return `共识别 ${count} 项，当前条目已全部展开。`;
}

function trustCategory(key, category) {
  const section = document.createElement("details");
  section.className = "system-section";
  const title = CATEGORY_LABELS[key] || key.replaceAll("_", " ");
  const parsed = Number(category.count);
  const count = Number.isFinite(parsed) ? Math.max(0, parsed) : "未知";
  section.open = typeof count === "number" && count > 0 && count <= 5;
  const summary = document.createElement("summary");
  summary.append(
    node("p", `${COVERAGE_LABELS[category.coverage] || "覆盖状态未知"} · ${count} 项`, "kicker"),
    node("h2", title),
  );
  section.append(
    summary,
    node("p", categoryMessage(category), category.coverage === "complete" ? "state-message" : "coverage-warning"),
  );
  const items = Array.isArray(category.items) ? category.items : [];
  if (items.length) {
    const list = document.createElement("dl");
    list.className = "detail-list";
    items.forEach((item) => {
      const entry = document.createElement("div");
      entry.className = "detail-row";
      entry.append(
        node("dt", item.severity === "attention" ? "需要关注" : "供复核"),
        node("dd", `${item.summary || "需要进一步核验"}（记录 ${item.id || "未知"}）`),
      );
      list.append(entry);
    });
    section.append(list);
  }
  return section;
}

export async function renderTrust(root, { signal, isCurrent, navigate }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(node("p", "正在核对记忆可信度……", "state-message"));
  try {
    const data = await api("/api/v2/trust", { signal });
    if (!isCurrent()) return;
    const fragment = document.createDocumentFragment();
    const heading = document.createElement("header");
    heading.className = "view-heading";
    heading.append(
      node("p", "TRUST LEDGER · 信任账本", "kicker"),
      node("h1", "可信，不等于看起来没有问题。"),
      node("p", "这里公开系统知道的缺口，也公开它尚未覆盖的范围。数字为零只有在完整核对后才表示未发现风险。", "lede"),
    );
    fragment.append(heading);
    const summary = document.createElement("section");
    summary.className = "fact-grid";
    const fact = (label, value, note) => {
      const card = document.createElement("article");
      card.className = "fact-card";
      card.append(node("span", label, "fact-label"), node("strong", String(value ?? "未知"), "fact-value"), node("small", note, "fact-note"));
      summary.append(card);
    };
    fact("等待确认", data.summary?.needs_confirmation, "候选理解与候选判断的合计");
    fact("低置信度", data.summary?.low_confidence, "证据强度不足，需要谨慎使用");
    fact("隐私排除", data.summary?.privacy_exclusions, "按隐私规则未进入上下文的项目");
    fact("结果复核", data.summary?.challenged_memories, "实际任务结果挑战过的记忆，等待人工核对");
    fragment.append(summary);
    const reviewActions = document.createElement("div");
    reviewActions.className = "honest-actions";
    if (Number(data.summary?.candidate_claims) > 0) {
      const claims = node("button", "审核候选理解");
      claims.type = "button";
      claims.addEventListener("click", () => navigate("home"));
      reviewActions.append(claims);
    }
    if (Number(data.summary?.candidate_judgments) > 0) {
      const judgments = node("button", "审核候选判断", "secondary");
      judgments.type = "button";
      judgments.addEventListener("click", () => navigate("judgments"));
      reviewActions.append(judgments);
    }
    if (reviewActions.children.length) fragment.append(reviewActions);
    const grid = document.createElement("div");
    grid.className = "system-grid";
    Object.entries(data.categories || {}).forEach(([key, category]) => grid.append(trustCategory(key, category || {})));
    if (!grid.children.length) grid.append(node("p", "服务没有返回信任分类，当前不能判断系统是否健康。", "state-panel error-state"));
    fragment.append(grid);
    root.replaceChildren(fragment);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "信任账本暂时不可读"), node("p", error.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重新核对";
    retry.addEventListener("click", () => navigate("trust"));
    failure.append(retry);
    root.replaceChildren(failure);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
