import { api } from "../api.js";
import { openDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

const STATUS_LABELS = {
  candidate: "待确认",
  confirmed: "已确认",
  rejected: "已拒绝",
  retired: "已退役",
};

const ACTIONS = {
  candidate: ["confirm", "reject", "correct"],
  confirmed: ["correct", "record_outcome", "retire"],
  rejected: ["correct"],
  retired: [],
};

const ACTION_LABELS = {
  confirm: "确认这项判断",
  reject: "拒绝这项判断",
  correct: "纠正判断内容",
  record_outcome: "记录真实结果",
  retire: "将判断退役",
};

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function field(label, name, value = "", options = {}) {
  const wrapper = document.createElement("label");
  wrapper.className = "field-label";
  wrapper.append(node("span", label));
  let control;
  if (options.choices) {
    control = document.createElement("select");
    options.choices.forEach(([choiceValue, choiceLabel]) => {
      const option = document.createElement("option");
      option.value = choiceValue;
      option.textContent = choiceLabel;
      control.append(option);
    });
  } else if (options.multiline) {
    control = document.createElement("textarea");
    control.rows = options.rows || 4;
  } else {
    control = document.createElement("input");
    control.type = options.type || "text";
  }
  control.name = name;
  control.value = value || "";
  control.required = options.required !== false;
  if (options.maxLength) control.maxLength = options.maxLength;
  wrapper.append(control);
  return wrapper;
}

function appendFact(parent, label, value) {
  if (value === undefined || value === null || value === "") return;
  const row = document.createElement("div");
  row.className = "detail-row";
  const content = Array.isArray(value) ? value.join("、") : String(value);
  row.append(node("dt", label), node("dd", content || "无"));
  parent.append(row);
}

function actionBody(action, detail, form) {
  const common = {
    action,
    expected_version: detail.revision,
    reason: String(new FormData(form).get("reason") || "").trim(),
  };
  const values = new FormData(form);
  if (action === "correct") {
    const changes = {};
    for (const key of ["title", "situation", "decision", "lesson", "next_trigger"]) {
      const value = String(values.get(key) || "").trim();
      if (value !== String(detail[key] || "").trim()) changes[key] = value;
    }
    if (!Object.keys(changes).length) throw new Error("请至少修改一项判断内容");
    return { ...common, changes };
  }
  if (action === "record_outcome") {
    const observed = String(values.get("observed_at") || "");
    return {
      ...common,
      status: String(values.get("status") || "positive"),
      summary: String(values.get("summary") || "").trim(),
      observed_at: observed ? new Date(observed).toISOString() : "",
    };
  }
  return common;
}

function openAction(action, detail, { mutate, refresh }) {
  const target = document.querySelector("#drawer .drawer-body");
  if (!target) return;
  target.replaceChildren();
  target.append(node("h3", ACTION_LABELS[action]));
  const form = document.createElement("form");
  form.className = "filter-form";
  if (action === "correct") {
    form.append(
      field("标题", "title", detail.title, { maxLength: 240 }),
      field("当时情境", "situation", detail.situation, { multiline: true, rows: 5, maxLength: 1600 }),
      field("最终决定", "decision", detail.decision, { multiline: true, rows: 5, maxLength: 1600 }),
      field("得到的教训", "lesson", detail.lesson, { multiline: true, maxLength: 1200, required: false }),
      field("下次触发条件", "next_trigger", detail.next_trigger, { multiline: true, maxLength: 800, required: false }),
    );
  }
  if (action === "record_outcome") {
    form.append(
      field("结果", "status", detail.outcome?.status === "unknown" ? "positive" : detail.outcome?.status, {
        choices: [["positive", "正向"], ["mixed", "有得有失"], ["negative", "负向"]],
      }),
      field("结果说明", "summary", detail.outcome?.summary || "", { multiline: true, maxLength: 1200 }),
      field("观察时间", "observed_at", "", { type: "datetime-local" }),
    );
  }
  const prompts = {
    confirm: "确认后，这张判断会进入可复用的已确认状态。",
    reject: "拒绝后，这张判断不会进入后续上下文。仍可通过纠正重新审视。",
    retire: "退役后，这张判断将保留为历史依据，但不能再修改。",
  };
  if (prompts[action]) form.append(node("p", prompts[action], "state-message"));
  form.append(field("操作原因", "reason", "", { multiline: true, rows: 3, maxLength: 500 }));
  const feedback = node("p", "", "state-message");
  feedback.setAttribute("role", "status");
  const actions = document.createElement("div");
  actions.className = "honest-actions";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = ACTION_LABELS[action];
  const back = document.createElement("button");
  back.type = "button";
  back.className = "secondary";
  back.textContent = "返回判断详情";
  back.addEventListener("click", refresh);
  actions.append(submit, back);
  form.append(actions, feedback);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.className = "is-pending";
    submit.textContent = "正在写入事件记录……";
    feedback.textContent = "";
    try {
      const payload = actionBody(action, detail, form);
      await mutate(`/api/v2/judgments/${encodeURIComponent(detail.card_id)}/actions`, payload);
      submit.className = "is-success";
      submit.textContent = "已经写入";
      await refresh();
    } catch (error) {
      submit.disabled = false;
      submit.className = "is-failure";
      submit.textContent = "写入失败，请重试";
      feedback.textContent = error.message || "操作失败";
    }
  });
  target.append(form);
}

function detailView(detail, { mutate, refresh }) {
  const fragment = document.createDocumentFragment();
  const status = STATUS_LABELS[detail.status] || "状态未知";
  fragment.append(node("p", `${status} · 修订 ${detail.revision ?? "未知"}`, "kicker"));
  const list = document.createElement("dl");
  list.className = "detail-list";
  appendFact(list, "标题", detail.title);
  appendFact(list, "情境", detail.situation);
  appendFact(list, "目标", detail.goal);
  appendFact(list, "约束", detail.constraints);
  appendFact(list, "信号", detail.signals);
  appendFact(list, "决定", detail.decision);
  appendFact(list, "其他方案", detail.alternatives);
  appendFact(list, "结果", detail.outcome?.status && detail.outcome.status !== "unknown" ? detail.outcome.status : "尚未形成明确结果");
  appendFact(list, "结果说明", detail.outcome?.summary);
  appendFact(list, "教训", detail.lesson);
  appendFact(list, "下次触发", detail.next_trigger);
  appendFact(list, "支持证据", detail.evidence_ids);
  appendFact(list, "相关理解", detail.claim_ids);
  appendFact(list, "最近更新", formatTimestamp(detail.updated_at));
  fragment.append(list);
  const actions = document.createElement("div");
  actions.className = "honest-actions";
  (ACTIONS[detail.status] || []).forEach((action) => {
    const button = document.createElement("button");
    button.type = "button";
    if (["reject", "retire"].includes(action)) button.className = "secondary";
    button.textContent = ACTION_LABELS[action];
    button.addEventListener("click", () => openAction(action, detail, { mutate, refresh }));
    actions.append(button);
  });
  if (actions.children.length) fragment.append(actions);
  else fragment.append(node("p", "这张判断已封存为历史依据，没有可执行动作。", "state-message"));
  return fragment;
}

export async function renderJudgmentDetail(cardId, trigger, { mutate, onChanged }) {
  const body = openDialog("判断依据", (target) => target.append(node("p", "正在读取判断详情……", "state-message")), trigger);
  const refresh = async () => {
    const detail = await api(`/api/v2/judgments/${encodeURIComponent(cardId)}`);
    const currentBody = document.querySelector("#drawer .drawer-body") || body;
    currentBody.replaceChildren(detailView(detail, { mutate, refresh }));
    await onChanged?.();
  };
  try {
    await refresh();
  } catch (error) {
    body.replaceChildren(node("p", error.message || "判断详情读取失败", "state-message error-text"));
  }
}

function judgmentCard(item, open) {
  const article = document.createElement("article");
  article.className = "memory-card";
  const meta = document.createElement("div");
  meta.className = "memory-meta";
  meta.append(
    node("span", STATUS_LABELS[item.status] || "状态未知", "source-mark"),
    node("time", formatTimestamp(item.updated_at)),
    node("span", `修订 ${item.revision ?? "未知"}`),
  );
  const copy = document.createElement("div");
  copy.append(node("h2", item.title || "未命名判断"), node("p", item.decision || "详情中保存了这项判断的决定与依据。"));
  const detail = document.createElement("button");
  detail.type = "button";
  detail.className = "text-button";
  detail.textContent = "查看依据与操作";
  detail.addEventListener("click", () => open(item.card_id, detail));
  article.append(meta, copy, detail);
  return article;
}

export async function renderJudgments(root, { route, signal, isCurrent, navigate, mutate }) {
  const status = route.params.get("status") || "";
  const shell = document.createElement("div");
  const heading = document.createElement("header");
  heading.className = "view-heading";
  heading.append(
    node("p", "DECISION LEDGER · 判断账本", "kicker"),
    node("h1", "判断不是答案，是可复盘的选择。"),
    node("p", "每张卡保留当时的情境、依据、决定和后来发生的结果。状态决定当前真正允许的动作。", "lede"),
  );
  const filters = document.createElement("form");
  filters.className = "filter-form";
  filters.append(field("查看状态", "status", status, {
    required: false,
    choices: [["", "全部判断"], ["candidate", "待确认"], ["confirmed", "已确认"], ["rejected", "已拒绝"], ["retired", "已退役"]],
  }));
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "应用筛选";
  filters.append(submit);
  filters.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate("judgments", { status: new FormData(filters).get("status") || "" });
  });
  const results = document.createElement("div");
  results.className = "memory-stack";
  shell.append(heading, filters, results);
  root.replaceChildren(shell);
  root.setAttribute("aria-busy", "true");

  const query = new URLSearchParams({ limit: "20" });
  if (status) query.set("status", status);
  const load = async () => {
    query.delete("cursor");
    const page = await api(`/api/v2/judgments?${query.toString()}`, { signal });
    if (!isCurrent()) return;
    results.replaceChildren();
    const items = Array.isArray(page.items) ? page.items : [];
    if (!items.length) {
      results.append(node("p", "当前范围内没有判断卡。这里不会用示例数据填满空白。", "state-panel empty-state"));
      return;
    }
    const open = (cardId, trigger) => renderJudgmentDetail(cardId, trigger, { mutate, onChanged: load });
    items.forEach((item) => results.append(judgmentCard(item, open)));
    let cursor = page.next_cursor || "";
    if (page.has_more && cursor) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "load-more";
      more.textContent = "继续读取判断账本";
      more.addEventListener("click", async () => {
        more.disabled = true;
        query.set("cursor", cursor);
        try {
          const next = await api(`/api/v2/judgments?${query.toString()}`, { signal });
          if (!isCurrent()) return;
          (next.items || []).forEach((item) => results.insertBefore(judgmentCard(item, open), more));
          cursor = next.next_cursor || "";
          if (!next.has_more || !cursor) more.remove();
          else more.disabled = false;
        } catch (error) {
          more.disabled = false;
          more.textContent = error.message || "加载失败，请重试";
        }
      });
      results.append(more);
    }
  };

  try {
    await load();
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "判断账本暂时不可读"), node("p", error.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重新读取";
    retry.addEventListener("click", () => navigate("judgments", { status }));
    failure.append(retry);
    results.replaceChildren(failure);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
