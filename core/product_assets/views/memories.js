import { api, mutate, explainApiError, createMutationAttempt } from "../api.js";
import { openDialog, closeDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

const PRIVATE_PLACEHOLDER = "私密记忆（正文已隐藏）";
const TABS = [
  ["review", "待我审核"],
  ["assets", "记忆资产"],
  ["value", "价值分析"],
  ["evidence", "原始证据"],
];
const STATUS_LABELS = {
  candidate: "待确认",
  confirmed: "已确认",
  rejected: "已拒绝",
  superseded: "已被修正",
};
const ACTION_LABELS = {
  "confirm": "确认收录",
  "reject": "拒绝",
  "reconsider": "重新考虑",
  "correct": "纠正",
};
const COHORTS = [
  ["", "全部价值信号"],
  ["pending_review", "状态为待确认"],
  ["never_acknowledged", "尚无确认接收回执"],
  ["frequently_helpful", "多次确认接收，结果仅为正向且被确认"],
  ["frequently_challenged", "多次确认接收且被标记需复核"],
  ["high_confidence_unused", "高置信且长期无确认接收"],
  ["expiring", "即将到期或已经过期"],
  ["has_counter_evidence", "声明了反证引用"],
  ["missing_evidence", "没有可用支持证据"],
  ["privacy_sensitive", "私密或受限"],
  ["recently_changed", "近期发生多版本变化"],
];
const CONTEXT_STAGE_LABELS = {
  trace_selected: "选择轨迹命中",
  preview_selected: "预览选中",
  compiled_snapshot_selected: "编译快照包含",
  delivered: "已投递",
  acknowledged: "确认接收",
};
const OUTCOME_LABELS = {
  positive: "正向",
  mixed: "混合",
  negative: "负向",
  unknown: "未评价",
};

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function liveMessage(value, className = "state-message") {
  const result = node("p", value, className);
  result.setAttribute("role", "status");
  result.setAttribute("aria-live", "polite");
  return result;
}

function announce(value, status = "info") {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.dataset.status = status;
  toast.textContent = value;
}

function routeChanges(route, changes = {}) {
  const result = {};
  route.params.forEach((value, key) => {
    if (key !== "view") result[key] = value;
  });
  return { ...result, ...changes };
}

function appendFact(parent, label, value) {
  if (value === undefined || value === null || value === "") return;
  const row = document.createElement("div");
  row.className = "detail-row";
  row.append(node("dt", label), node("dd", String(value)));
  parent.append(row);
}

function selectField(name, label, options, value = "") {
  const wrapper = node("label", "", "field-label");
  wrapper.append(node("span", label));
  const select = document.createElement("select");
  select.name = name;
  options.forEach(([optionValue, optionLabel]) => {
    const option = new Option(optionLabel, optionValue);
    option.selected = optionValue === value;
    select.append(option);
  });
  wrapper.append(select);
  return wrapper;
}

function textField(name, label, value = "", minimum = 0, required = false) {
  const wrapper = node("label", "", "field-label");
  wrapper.append(node("span", label));
  const input = document.createElement("input");
  input.name = name;
  input.value = value;
  if (minimum) input.minLength = minimum;
  input.required = required;
  wrapper.append(input);
  return wrapper;
}

function retryPanel(title, error, label, retry) {
  const panel = node("div", "", "state-panel error-state");
  panel.append(node("h2", title), node("p", explainApiError(error)));
  const button = node("button", label);
  button.type = "button";
  button.addEventListener("click", retry);
  panel.append(button);
  return panel;
}

function tabBar(active, navigate, route) {
  const tabs = node("nav", "", "workbench-tabs");
  tabs.setAttribute("aria-label", "记忆工作台视图");
  TABS.forEach(([id, label]) => {
    const button = node("button", label, id === active ? "is-active" : "secondary");
    button.type = "button";
    button.dataset.memoryTab = id;
    button.setAttribute("aria-pressed", String(id === active));
    button.addEventListener("click", (event) => {
      const restoreKeyboardFocus = event.detail === 0;
      navigate("memories", routeChanges(route, { tab: id, claim: "" }));
      if (restoreKeyboardFocus) {
        requestAnimationFrame(() => document.querySelector(`[data-memory-tab="${id}"]`)?.focus());
      }
    });
    tabs.append(button);
  });
  return tabs;
}

function workbenchHeader(active, navigate, route) {
  const heading = node("header", "", "view-heading workbench-heading");
  heading.append(
    node("p", "MEMORY GOVERNANCE · 记忆治理", "kicker"),
    node("h1", "看见系统如何理解你。"),
    node("p", "原始证据、候选理解、已确认记忆与真实使用分开呈现。每个结论都能回到版本、证据和结果。", "lede"),
    tabBar(active, navigate, route),
  );
  return heading;
}

function statusChip(status) {
  const chip = node("span", STATUS_LABELS[status] || status || "状态未知", "status-chip");
  chip.dataset.status = status || "unknown";
  return chip;
}

function claimTitle(item) {
  return item?.masked === true ? PRIVATE_PLACEHOLDER : (item?.statement || "记忆正文不可见");
}

function claimCard(item, onOpen) {
  const article = node("article", "", "claim-row");
  article.dataset.status = item.status || "unknown";
  article.dataset.claimId = item.claim_id || "";
  const meta = node("div", "", "claim-row-meta");
  meta.append(statusChip(item.status), node("span", `v${item.revision ?? "?"}`, "source-mark"));
  const copy = node("div", "", "claim-row-copy");
  copy.append(
    node("h3", claimTitle(item)),
    node("p", item.masked === true ? "私密内容仅显示存在性与治理状态。" : `${item.claim_type || "未分类"} · ${item.source_kind || "来源未知"}`),
  );
  const open = node("button", "查看脉络", "text-button");
  open.type = "button";
  open.addEventListener("click", () => onOpen(item.claim_id, open, item));
  article.append(meta, copy, open);
  return article;
}

function isCurrentDialogBody(target) {
  return target.isConnected && document.querySelector("#drawer .drawer-body") === target;
}

function actionDialog(initialDetail, action, trigger, { refresh, isRouteCurrent }) {
  let authority = initialDetail;
  openDialog(ACTION_LABELS[action] || "处理记忆", (target) => {
    const form = node("form", "", "action-form");
    form.append(node("p", claimTitle(authority), "state-message"));
    let statement;
    let evidence;
    if (action === "correct") {
      const field = textField("statement", "纠正后的表述", authority.masked === true ? "" : authority.statement || "", 1, true);
      statement = field.querySelector("input");
      form.append(field);
    }
    if (action === "reconsider") {
      const field = textField("evidence_ids", "新增证据 ID，使用逗号分隔", "", 1, true);
      evidence = field.querySelector("input");
      form.append(field);
    }
    const reasonField = node("label", "", "field-label");
    reasonField.append(node("span", "说明原因"));
    const reason = document.createElement("textarea");
    reason.name = "reason";
    reason.required = true;
    reasonField.append(reason);
    const submit = node("button", ACTION_LABELS[action] || "提交");
    submit.type = "submit";
    const feedback = liveMessage("", "form-feedback");
    const attempt = createMutationAttempt();
    form.append(reasonField, submit, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      feedback.textContent = "正在写入 Claim 权威事件……";
      const payload = {
        action,
        expected_version: authority.revision,
        reason: reason.value,
      };
      if (statement) payload.statement = statement.value;
      if (evidence) payload.evidence_ids = evidence.value.split(/[，,]/).map((value) => value.trim()).filter(Boolean);
      try {
        const result = await mutate(
          `/api/v2/claims/${encodeURIComponent(authority.claim_id)}/actions`,
          payload,
          attempt.options(payload),
        );
        const message = result.derived_update_pending
          ? "权威记忆已保存，衍生视图尚未更新。请稍后重试读取。"
          : "权威记忆已保存。";
        announce(message, result.derived_update_pending ? "attention" : "success");
        if (!isCurrentDialogBody(target) || !isRouteCurrent()) return;
        closeDialog();
        await refresh(result.claim_id || authority.claim_id);
      } catch (error) {
        if (!isCurrentDialogBody(target) || !isRouteCurrent()) {
          if (error?.name !== "AbortError") announce(explainApiError(error), "attention");
          return;
        }
        if (error?.code === "version_conflict") {
          feedback.textContent = "数据已经变化，正在重新读取 Claim 权威……";
          try {
            const latest = await api(`/api/v2/claims/${encodeURIComponent(authority.claim_id)}`);
            if (!isCurrentDialogBody(target) || !isRouteCurrent()) return;
            authority = latest;
            if (!(latest.allowed_actions || []).includes(action)) {
              feedback.textContent = "最新状态不再允许这项操作。请关闭后查看权威状态。";
              return;
            }
            attempt.clear();
            feedback.textContent = `已重新读取最新版本 v${latest.revision ?? "?"}。请检查内容后再次提交。`;
            submit.disabled = false;
          } catch (reloadError) {
            if (!isCurrentDialogBody(target) || !isRouteCurrent()) return;
            feedback.textContent = `最新权威读取失败：${explainApiError(reloadError)}`;
            submit.disabled = false;
          }
          return;
        }
        feedback.textContent = explainApiError(error);
        submit.disabled = false;
      }
    });
    target.append(form);
  }, trigger);
}

function lineageSection(title, rows, render, options = {}) {
  const section = node("section", "", `lineage-section ${options.className || ""}`.trim());
  section.append(node("h3", title));
  const safeRows = Array.isArray(rows) ? rows : [];
  if (!safeRows.length) section.append(node("p", options.empty || "当前没有可展示记录。", "state-message"));
  safeRows.forEach((row) => section.append(render(row)));
  if (options.truncated) {
    section.append(node("p", `仅显示 ${safeRows.length} 条，共 ${options.total ?? safeRows.length} 条。`, "truncation-note"));
  }
  return section;
}

function evidenceNode(item) {
  const row = node("div", "", "lineage-node");
  row.append(
    node("strong", item.evidence_id || "未知证据"),
    node("span", `${item.source || "来源不可用"} · ${item.status || "状态未知"}`),
  );
  return row;
}

function valueSignals(value, valueStatus, masked, retry) {
  const section = node("section", "", "value-signals");
  section.append(node("h3", "价值信号，不合并成黑盒总分"));
  if (masked || value?.value_limited) {
    section.append(node("p", "私密记忆不展示使用与结果信号。", "state-message"));
    return section;
  }
  if (valueStatus !== "ready" || !value) {
    section.append(node("p", "价值信号暂不可读", "state-message error-text"));
    const button = node("button", "重试价值信号", "secondary");
    button.type = "button";
    button.addEventListener("click", retry);
    section.append(button);
    return section;
  }
  const signals = value.signals || {};
  const entries = [
    ["选择轨迹命中", signals.trace_selected ?? signals.considered],
    ["预览选中", signals.preview_selected ?? signals.selected],
    ["编译快照包含", signals.compiled_snapshot_selected ?? signals.compiled],
    ["已投递", signals.delivered],
    ["确认接收", signals.acknowledged],
    ["正向 Outcome", value.outcomes?.positive],
    ["标记需复核", value.human_feedback?.challenged],
  ];
  const grid = node("div", "", "signal-grid");
  entries.forEach(([label, number]) => {
    const card = node("div", "", "signal-cell");
    card.append(node("span", label), node("strong", String(number ?? 0)));
    grid.append(card);
  });
  section.append(
    grid,
    node("p", "考虑口径：这里只统计持久化选择轨迹中明确选中的 Claim，不把页面曝光推断为被考虑。", "scope-note"),
  );
  return section;
}

function detailNavigation(navigation) {
  const nav = node("nav", "", "detail-navigation");
  nav.setAttribute("aria-label", "记忆顺序导航");
  const back = node("button", "返回列表", "secondary");
  back.type = "button";
  back.addEventListener("click", navigation.back);
  const previous = node("button", "上一条", "secondary");
  previous.type = "button";
  previous.disabled = !navigation.previous;
  if (navigation.previous) previous.addEventListener("click", navigation.previous);
  const next = node("button", "下一条", "secondary");
  next.type = "button";
  next.disabled = !navigation.next;
  if (navigation.next) next.addEventListener("click", navigation.next);
  nav.append(back, previous, next);
  return nav;
}

function historyNode(item, masked = false) {
  const row = node("div", "", "lineage-node");
  row.append(
    node("strong", STATUS_LABELS[item.current_status] || item.event_type || "未知事件"),
    node("span", `${formatTimestamp(item.occurred_at)} · ${masked ? "历史原因已隐藏" : item.reason || "无说明"}`),
  );
  return row;
}

async function renderClaimHistory(id, target, signal, isCurrent, masked, firstPage = null, firstError = null) {
  target.replaceChildren(node("h3", "版本历史"), liveMessage("正在读取版本历史……"));
  let page;
  try {
    if (firstError) throw firstError;
    page = firstPage || await api(`/api/v2/claims/${encodeURIComponent(id)}/history?limit=20`, { signal });
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    target.replaceChildren(node("h3", "版本历史"), node("p", "版本历史暂不可读", "state-message error-text"));
    const retry = node("button", "重试版本历史", "secondary");
    retry.type = "button";
    retry.addEventListener("click", () => renderClaimHistory(id, target, signal, isCurrent, masked));
    target.append(retry);
    return;
  }
  if (!isCurrent()) return;
  const list = node("div", "", "history-list");
  (page.items || []).forEach((item) => list.append(historyNode(item, masked)));
  if (!(page.items || []).length) list.append(node("p", "当前没有版本记录。", "state-message"));
  target.replaceChildren(node("h3", "版本历史"), list);
  let cursor = page.next_cursor || "";
  if (!page.has_more || !cursor) return;
  const more = node("button", "继续加载版本历史", "load-more");
  more.type = "button";
  more.addEventListener("click", async () => {
    more.disabled = true;
    more.textContent = "正在加载版本历史……";
    try {
      const next = await api(`/api/v2/claims/${encodeURIComponent(id)}/history?limit=20&cursor=${encodeURIComponent(cursor)}`, { signal });
      if (!isCurrent()) return;
      (next.items || []).forEach((item) => list.append(historyNode(item, masked)));
      cursor = next.next_cursor || "";
      if (!next.has_more || !cursor) more.remove();
      else {
        more.disabled = false;
        more.textContent = "继续加载版本历史";
      }
    } catch (error) {
      if (error?.name === "AbortError" || !isCurrent()) return;
      more.disabled = false;
      more.textContent = "版本历史加载失败，重试";
      more.title = explainApiError(error);
    }
  });
  target.append(more);
}

function livingSelfSection(detail) {
  if (detail.living_self_status === "unavailable") {
    return lineageSection("Living Self 版本引用", [], evidenceNode, { empty: "Living Self 引用暂不可读。" });
  }
  return lineageSection("Living Self 版本引用", detail.living_self_refs || [], (item) => {
    const row = node("div", "", "lineage-node");
    row.append(node("strong", item.section || "未分类"), node("span", `${item.item_id || "未知条目"} · ${item.status || "状态未知"}`));
    return row;
  });
}

async function showClaimDetail(id, target, signal, options) {
  const isCurrent = options.isCurrent;
  target.setAttribute("aria-busy", "true");
  target.replaceChildren(liveMessage("正在读取记忆详情……"));
  const historyTask = api(`/api/v2/claims/${encodeURIComponent(id)}/history?limit=20`, { signal })
    .then((value) => ({ value }), (error) => ({ error }));
  let detail;
  try {
    detail = await api(`/api/v2/claims/${encodeURIComponent(id)}`, { signal });
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    target.replaceChildren(retryPanel("记忆详情暂时不可读", error, "重试记忆详情", options.reload));
    target.setAttribute("aria-busy", "false");
    return;
  }
  if (!isCurrent()) return;
  if (options.forceMasked || detail.masked === true) {
    detail = { ...detail, masked: true, statement: PRIVATE_PLACEHOLDER };
  }
  const content = node("div", "", "claim-detail-panel");
  content.append(detailNavigation(options.navigation));
  const head = node("header", "", "claim-detail-head");
  head.append(
    statusChip(detail.status),
    node("h2", claimTitle(detail)),
    node("p", `${detail.masked === true ? "私密" : detail.claim_type || "未分类"} · revision ${detail.revision ?? "?"}`, "state-message"),
  );
  const actions = node("div", "", "honest-actions");
  (detail.allowed_actions || []).forEach((action) => {
    const button = node("button", ACTION_LABELS[action] || action, action === "reject" ? "secondary" : "");
    button.type = "button";
    button.addEventListener("click", () => actionDialog(detail, action, button, { refresh: options.refresh, isRouteCurrent: options.isRouteCurrent }));
    actions.append(button);
  });
  head.append(actions);
  content.append(
    head,
    valueSignals(detail.value, detail.value_status, detail.masked === true, options.reload),
    lineageSection("支持证据", detail.evidence || [], evidenceNode, {
      total: detail.evidence_total,
      truncated: detail.evidence_truncated === true,
    }),
    lineageSection("反证", detail.counter_evidence || [], evidenceNode, {
      total: detail.counter_evidence_total,
      truncated: detail.counter_evidence_truncated === true,
    }),
    livingSelfSection(detail),
  );
  const historySlot = node("section", "", "lineage-section history-section");
  content.append(historySlot);
  const timeline = Array.isArray(detail.value?.timeline) ? detail.value.timeline : [];
  const contextTimeline = timeline.filter((item) => item.stage !== "outcome");
  const outcomeTimeline = timeline.filter((item) => item.stage === "outcome");
  content.append(
    lineageSection("Context 使用", contextTimeline, (item) => {
      const row = node("div", "", "lineage-node");
      row.append(node("strong", CONTEXT_STAGE_LABELS[item.stage] || item.stage || "状态未知"), node("span", `${item.context_id || "未知 Context"} · ${formatTimestamp(item.occurred_at)}`));
      return row;
    }),
    lineageSection("Outcome 结果", outcomeTimeline, (item) => {
      const row = node("div", "", "lineage-node");
      row.append(node("strong", OUTCOME_LABELS[item.result] || item.result || "未评价"), node("span", `${item.outcome_id || "未知 Outcome"} · ${item.context_id || "未知 Context"} · ${formatTimestamp(item.occurred_at)}`));
      return row;
    }),
  );
  if (detail.value?.timeline_truncated === true) {
    content.append(node("p", `使用与结果时间线仅显示最近 ${timeline.length} 条，共 ${detail.value.timeline_total ?? timeline.length} 条。`, "truncation-note"));
  }
  target.replaceChildren(content);
  target.setAttribute("aria-busy", "false");
  const historyResult = await historyTask;
  if (!isCurrent()) return;
  if (historyResult.error) {
    await renderClaimHistory(id, historySlot, signal, isCurrent, detail.masked === true, null, historyResult.error);
  } else {
    await renderClaimHistory(id, historySlot, signal, isCurrent, detail.masked === true, historyResult.value);
  }
}

function createDetailLoader(routeSignal) {
  let controller = null;
  let generation = 0;
  routeSignal.addEventListener("abort", () => controller?.abort(), { once: true });
  return (id, target, options) => {
    controller?.abort();
    controller = new AbortController();
    if (routeSignal.aborted) controller.abort();
    const ownGeneration = ++generation;
    const isCurrent = () => ownGeneration === generation && !controller.signal.aborted && target.isConnected;
    const reload = () => {
      if (isCurrent()) createLoad();
    };
    const createLoad = () => {
      controller?.abort();
      controller = new AbortController();
      const reloadGeneration = ++generation;
      const reloadCurrent = () => reloadGeneration === generation && !controller.signal.aborted && target.isConnected;
      return showClaimDetail(id, target, controller.signal, { ...options, isCurrent: reloadCurrent, reload: createLoad, routeSignal });
    };
    return showClaimDetail(id, target, controller.signal, { ...options, isCurrent, reload, routeSignal });
  };
}

function claimFilters(tab, route, navigate) {
  const form = node("form", "", "workbench-filter");
  const query = route.params.get("q") || "";
  form.append(textField("q", "搜索理解，至少 2 个字符", query, 2));
  if (tab === "assets") {
    form.append(selectField("status", "状态", [["", "全部状态"], ...Object.entries(STATUS_LABELS)], route.params.get("status") || ""));
  }
  form.append(selectField("privacy", "隐私级别", [["", "全部隐私级别"], ["public", "公开"], ["context_safe", "可进入 Context"], ["restricted", "受限"], ["private", "私密"]], route.params.get("privacy") || ""));
  const submit = node("button", "应用筛选");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate("memories", { tab, ...Object.fromEntries(new FormData(form).entries()) });
  });
  return form;
}

function claimButton(list, claimId) {
  const row = [...list.querySelectorAll(".claim-row")].find((item) => item.dataset.claimId === claimId);
  return row?.querySelector("button") || null;
}

function activateClaimRow(list, claimId) {
  list.querySelectorAll(".claim-row").forEach((row) => {
    if (row.dataset.claimId === claimId) {
      row.classList.add("is-active");
      row.setAttribute("aria-current", "true");
    } else {
      row.classList.remove("is-active");
      row.removeAttribute("aria-current");
    }
  });
}

async function renderClaims(target, context, tab) {
  const { route, signal, isCurrent, navigate } = context;
  const layout = node("div", "", "memory-workbench");
  const queues = node("aside", "", "workbench-queues");
  const list = node("section", "", "workbench-list");
  list.setAttribute("aria-label", "记忆列表");
  const detail = node("aside", "", "workbench-detail");
  detail.setAttribute("aria-label", "记忆证据脉络");
  queues.append(node("h2", tab === "review" ? "审核队列" : "筛选"), claimFilters(tab, route, navigate));
  list.append(liveMessage("正在读取 Claim 权威层……"));
  detail.append(node("p", "选择一条记忆，查看证据、版本和真实使用。", "state-message"));
  layout.append(queues, list, detail);
  target.replaceChildren(layout);
  const query = new URLSearchParams({ limit: "20" });
  if (tab === "review") query.set("status", "candidate");
  for (const key of ["q", "status", "privacy"]) {
    const value = route.params.get(key);
    if (value && !(tab === "review" && key === "status")) query.set(key, value);
  }
  const refresh = async (claimId) => navigate("memories", routeChanges(route, { tab, claim: claimId }));
  const loadDetail = createDetailLoader(signal);
  try {
    const page = await api(`/api/v2/claims?${query.toString()}`, { signal });
    if (!isCurrent()) return;
    queues.prepend(node("p", `${page.total} 条符合当前条件`, "queue-count"));
    list.replaceChildren();
    const items = [...(page.items || [])];
    if (!items.length) {
      list.append(node("p", tab === "review" ? "没有待审核的记忆。" : "当前筛选没有记忆资产。", "state-panel empty-state"));
      return;
    }
    const openClaim = (claimId, trigger) => {
      activateClaimRow(list, claimId);
      const index = items.findIndex((item) => item.claim_id === claimId);
      const compact = window.matchMedia("(max-width: 900px)").matches;
      const openAt = (nextIndex) => {
        const nextItem = items[nextIndex];
        if (!nextItem) return;
        openClaim(nextItem.claim_id, claimButton(list, nextItem.claim_id) || trigger);
      };
      const navigation = {
        back: () => {
          if (compact) closeDialog();
          (claimButton(list, claimId) || trigger)?.focus();
        },
        previous: index > 0 ? () => openAt(index - 1) : null,
        next: index >= 0 && index < items.length - 1 ? () => openAt(index + 1) : null,
      };
      const renderInto = (container) => loadDetail(claimId, container, {
        refresh,
        navigation,
        isRouteCurrent: isCurrent,
        forceMasked: items[index]?.masked === true,
      });
      if (compact) openDialog("记忆详情", renderInto, trigger);
      else renderInto(detail);
    };
    items.forEach((item) => list.append(claimCard(item, openClaim)));
    let cursor = page.next_cursor || "";
    if (page.has_more && cursor) {
      const more = node("button", "继续加载", "load-more");
      more.type = "button";
      more.addEventListener("click", async () => {
        more.disabled = true;
        more.textContent = "正在加载……";
        query.set("cursor", cursor);
        try {
          const next = await api(`/api/v2/claims?${query.toString()}`, { signal });
          if (!isCurrent()) return;
          (next.items || []).forEach((item) => {
            items.push(item);
            list.insertBefore(claimCard(item, openClaim), more);
          });
          cursor = next.next_cursor || "";
          if (!next.has_more || !cursor) more.remove();
          else {
            more.disabled = false;
            more.textContent = "继续加载";
          }
        } catch (error) {
          if (error?.name === "AbortError" || !isCurrent()) return;
          more.disabled = false;
          more.textContent = "加载失败，重试";
          more.title = explainApiError(error);
        }
      });
      list.append(more);
    }
    const requested = route.params.get("claim");
    const initial = requested || (!window.matchMedia("(max-width: 900px)").matches ? items[0]?.claim_id : "");
    if (initial) openClaim(initial, claimButton(list, initial) || list);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    list.replaceChildren(retryPanel(
      "审核数据暂时不可读",
      error,
      "重试当前筛选",
      () => navigate("memories", routeChanges(route, { tab })),
    ));
  }
}

function scopeCopy(summary) {
  const scope = summary?.considered_scope || summary?.trace_scope || "";
  if (scope === "persisted_selected_claim_entries_only") {
    return "统计口径：仅统计持久化选择轨迹中明确选中的 Claim，不把候选曝光推断成被考虑。";
  }
  return "统计口径：接口未提供可验证的被考虑范围，因此不推断该信号。";
}

async function renderValue(target, context) {
  const { route, signal, isCurrent, navigate } = context;
  const cohort = route.params.get("cohort") || "";
  const form = node("form", "", "value-filter");
  form.append(selectField("cohort", "定向分析条件", COHORTS, cohort));
  const submit = node("button", "查看结果");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate("memories", { tab: "value", cohort: new FormData(form).get("cohort") });
  });
  const overview = node("section", "", "value-overview");
  const list = node("section", "", "workbench-list value-list");
  target.replaceChildren(form, overview, list);
  overview.append(liveMessage("正在读取价值概览……"));
  list.append(liveMessage("正在读取符合条件的 Claim……"));
  const query = new URLSearchParams({ limit: "20" });
  if (cohort) query.set("cohort", cohort);
  const summaryTask = api("/api/v2/memory-value/overview", { signal }).then((value) => ({ value }), (error) => ({ error }));
  const pageTask = api(`/api/v2/memory-value/claims?${query.toString()}`, { signal }).then((value) => ({ value }), (error) => ({ error }));
  const summaryResult = await summaryTask;
  if (!isCurrent()) return;
  if (summaryResult.error) {
    overview.replaceChildren(retryPanel("价值概览暂时不可读", summaryResult.error, "重试价值概览", () => navigate("memories", routeChanges(route, { tab: "value" }))));
  } else {
    const summary = summaryResult.value;
    const cards = node("div", "", "signal-grid");
    [["记忆总数", summary.claim_total], ["确认接收的 Context", summary.acknowledged_contexts], ["已记录 Outcome 的 Context", summary.evaluated_contexts]].forEach(([label, value]) => {
      const card = node("div", "", "signal-cell");
      card.append(node("span", label), node("strong", String(value ?? 0)));
      cards.append(card);
    });
    overview.replaceChildren(cards, node("p", scopeCopy(summary), "scope-note"), node("p", "这里不提供黑盒价值总分。Context 使用和 Outcome 结果分别呈现。", "state-message"));
  }
  const pageResult = await pageTask;
  if (!isCurrent()) return;
  if (pageResult.error) {
    list.replaceChildren(retryPanel("定向分析结果暂时不可读", pageResult.error, "重试当前分析条件", () => navigate("memories", routeChanges(route, { tab: "value" }))));
    return;
  }
  const page = pageResult.value;
  list.replaceChildren();
  if (!(page.items || []).length) list.append(node("p", "当前条件没有匹配的 Claim。", "state-panel empty-state"));
  const loadDetail = createDetailLoader(signal);
  const refresh = async (claimId) => navigate("memories", routeChanges(route, { tab: "value", cohort, claim: claimId }));
  const openValue = (id, trigger, item) => {
    openDialog("记忆价值脉络", (drawer) => loadDetail(id, drawer, {
      refresh,
      navigation: { back: closeDialog, previous: null, next: null },
      isRouteCurrent: isCurrent,
      forceMasked: item?.masked === true,
    }), trigger);
  };
  (page.items || []).forEach((item) => list.append(claimCard(item, openValue)));
  let cursor = page.next_cursor || "";
  if (page.has_more && cursor) {
    const more = node("button", "继续加载", "load-more");
    more.type = "button";
    more.addEventListener("click", async () => {
      more.disabled = true;
      query.set("cursor", cursor);
      try {
        const next = await api(`/api/v2/memory-value/claims?${query.toString()}`, { signal });
        if (!isCurrent()) return;
        (next.items || []).forEach((item) => list.insertBefore(claimCard(item, openValue), more));
        cursor = next.next_cursor || "";
        if (!next.has_more || !cursor) more.remove();
        else more.disabled = false;
      } catch (error) {
        if (error?.name === "AbortError" || !isCurrent()) return;
        more.disabled = false;
        more.textContent = "加载失败，重试";
        more.title = explainApiError(error);
      }
    });
    list.append(more);
  }
}

function coverageWarning(page, filters) {
  if (page.coverage_complete !== false) return null;
  const labels = { person: "人物", project: "项目", topic: "主题" };
  const incomplete = Object.keys(labels).filter((key) => filters[key] && page.coverage?.[key]?.complete !== true);
  return node("p", `${incomplete.map((key) => labels[key]).join("、") || "当前筛选维度"}的索引覆盖尚不完整。空结果不等于没有相关记忆。`, "coverage-warning");
}

async function showEvidenceDetail(id, trigger, signal) {
  const body = openDialog("原始证据", (target) => target.append(liveMessage("正在读取原始记录……")), trigger);
  try {
    const detail = await api(`/api/v2/memories/${encodeURIComponent(id)}`, { signal });
    if (!isCurrentDialogBody(body) || signal.aborted) return;
    const list = document.createElement("dl");
    list.className = "detail-list";
    appendFact(list, "时间", detail.timestamp);
    appendFact(list, "来源", detail.source);
    appendFact(list, "身份", detail.role);
    appendFact(list, "项目", detail.project);
    appendFact(list, "敏感级别", detail.sensitivity);
    appendFact(list, "记录", detail.content);
    body.replaceChildren(list);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrentDialogBody(body)) return;
    body.replaceChildren(node("p", explainApiError(error), "state-message error-text"));
  }
}

function evidenceCard(item, signal) {
  const article = node("article", "", "memory-card");
  const time = document.createElement("time");
  time.dateTime = item.timestamp || "";
  time.textContent = formatTimestamp(item.timestamp);
  const meta = node("div", "", "memory-meta");
  meta.append(
    time,
    node("span", item.source || "来源未知", "source-mark"),
    node("span", `身份：${item.role || "未知"}`),
    node("span", `项目：${item.project || "未归档"}`),
  );
  const detail = node("button", "查看原始证据", "text-button");
  detail.type = "button";
  detail.addEventListener("click", () => showEvidenceDetail(item.id, detail, signal));
  article.append(meta, node("div", "", "memory-copy"), detail);
  article.querySelector(".memory-copy").append(node("h2", item.project || item.role || "未命名记录"), node("p", item.summary || "无摘要"));
  return article;
}

async function renderEvidence(target, context) {
  const { route, signal, isCurrent, navigate } = context;
  const accepted = ["q", "source", "person", "project", "topic", "from", "to"];
  const filters = Object.fromEntries(accepted.map((key) => [key, route.params.get(key) || ""]));
  const form = node("form", "", "filter-form");
  form.append(textField("q", "搜索，至少 3 个字符", filters.q, 3), textField("source", "来源", filters.source), textField("project", "项目", filters.project), textField("person", "人物", filters.person), textField("topic", "主题", filters.topic), textField("from", "起始时间，含时区", filters.from), textField("to", "结束时间，含时区", filters.to));
  const submit = node("button", "应用筛选");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate("memories", { tab: "evidence", ...Object.fromEntries(new FormData(form).entries()) });
  });
  const results = node("div", "", "memory-stack");
  results.append(liveMessage("正在读取原始证据索引……"));
  target.replaceChildren(node("p", "原始证据是事实层，不等于系统已经采用的长期记忆。", "coverage-warning"), form, results);
  const query = new URLSearchParams({ limit: "20" });
  Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
  try {
    const page = await api(`/api/v2/memories?${query.toString()}`, { signal });
    if (!isCurrent()) return;
    results.replaceChildren();
    const warning = coverageWarning(page, filters);
    if (warning) results.append(warning);
    if (!(page.items || []).length) results.append(node("p", page.coverage_complete === false ? "当前已覆盖范围内没有找到结果，不能据此判断相关记忆不存在。" : "当前筛选没有找到原始证据。", "state-panel empty-state"));
    (page.items || []).forEach((item) => results.append(evidenceCard(item, signal)));
    let cursor = page.next_cursor || "";
    if (page.has_more && cursor) {
      const more = node("button", "沿时间继续加载", "load-more");
      more.type = "button";
      more.addEventListener("click", async () => {
        more.disabled = true;
        query.set("cursor", cursor);
        try {
          const next = await api(`/api/v2/memories?${query.toString()}`, { signal });
          if (!isCurrent()) return;
          (next.items || []).forEach((item) => results.insertBefore(evidenceCard(item, signal), more));
          cursor = next.next_cursor || "";
          if (!next.has_more || !cursor) more.remove();
          else more.disabled = false;
        } catch (error) {
          if (error?.name === "AbortError" || !isCurrent()) return;
          more.disabled = false;
          more.textContent = "加载失败，重试";
          more.title = explainApiError(error);
        }
      });
      results.append(more);
    }
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    results.replaceChildren(retryPanel(
      "原始证据索引暂时不可读",
      error,
      "重试当前筛选",
      () => navigate("memories", routeChanges(route, { tab: "evidence" })),
    ));
  }
}

export async function renderMemories(root, context) {
  const requested = context.route.params.get("tab") || "review";
  const active = TABS.some(([id]) => id === requested) ? requested : "review";
  const shell = node("div", "", "memory-workbench-shell");
  const content = node("div", "", "workbench-content");
  shell.append(workbenchHeader(active, context.navigate, context.route), content);
  root.replaceChildren(shell);
  root.setAttribute("aria-busy", "true");
  try {
    if (active === "review" || active === "assets") await renderClaims(content, context, active);
    else if (active === "value") await renderValue(content, context);
    else await renderEvidence(content, context);
  } finally {
    if (context.isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
