import { api, mutate, explainApiError } from "../api.js";
import { openDialog, closeDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

const STATES = {
  preview: "预览完成",
  compiled: "可使用",
  consumed: "已交给 Agent · 待记录结果",
  outcome_recorded: "结果已记录",
};
const MODES = ["auto", "advisor", "writer", "reviewer", "business", "project", "custom"];

function node(tag, value = "", className = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function control(label, name, tag = "input") {
  const wrapper = document.createElement("label");
  wrapper.className = "field-label";
  wrapper.append(node("span", label));
  const input = document.createElement(tag);
  input.name = name;
  input.required = true;
  wrapper.append(input);
  return { wrapper, input };
}

function pending(button, yes, wait, idle) {
  button.disabled = yes;
  button.classList.toggle("is-pending", yes);
  button.textContent = yes ? wait : idle;
}

function previewBody(preview, refresh) {
  const container = node("div", "", "context-preview");
  container.append(node("p", `预览有效至 ${formatTimestamp(preview.expires_at)}`, "status-chip"));
  const selections = node("div", "", "preview-sections");
  Object.entries(preview.sections || {}).forEach(([sectionName, items]) => {
    const section = node("section", "", "preview-section");
    section.append(node("h3", sectionName));
    (items || []).forEach((item) => {
      const label = document.createElement("label");
      label.className = "exclusion-row";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "excluded_item_ids";
      checkbox.value = item.id;
      label.append(checkbox, node("span", item.summary || item.title || item.id));
      section.append(label);
    });
    selections.append(section);
  });
  container.append(selections);
  const mode = document.createElement("select");
  MODES.filter((value) => value !== "auto").forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    mode.append(option);
  });
  if (preview.mode === "auto") {
    const label = node("label", "", "field-label");
    label.append(node("span", "这次以什么角色使用"), mode);
    container.append(label);
  }
  const compile = node("button", "确认并生成 Context");
  compile.type = "button";
  const feedback = node("p", "", "form-feedback");
  compile.addEventListener("click", async () => {
    pending(compile, true, "编译中……", "确认并生成 Context");
    feedback.textContent = "编译中";
    try {
      const excluded = [...container.querySelectorAll('input[name="excluded_item_ids"]:checked')].map((input) => input.value);
      const body = {
        preview_id: preview.preview_id,
        preview_hash: preview.preview_hash,
        excluded_item_ids: excluded,
        expected_version: preview.revision,
        reason: "用户确认预览并生成 Context",
      };
      if (preview.mode === "auto") body.resolved_mode = mode.value;
      const compiled = await mutate("/api/v2/contexts", body);
      feedback.textContent = "可使用";
      closeDialog();
      await showContext(compiled.context_id, compile, refresh);
    } catch (error) {
      feedback.textContent = `失败：${explainApiError(error)}`;
      pending(compile, false, "编译中……", "再次尝试");
    }
  });
  container.append(compile, feedback);
  return container;
}

function outcomeForm(detail, refresh) {
  const form = node("form", "", "action-form");
  const adopted = control("采用程度", "adopted", "select");
  [["yes", "完整采用"], ["partial", "部分采用"], ["no", "没有采用"], ["unknown", "无法判断"]].forEach(([value, label]) => adopted.input.append(new Option(label, value)));
  const result = control("结果", "result", "select");
  [["positive", "有效"], ["mixed", "有好有坏"], ["negative", "无效"], ["unknown", "尚不明确"]].forEach(([value, label]) => result.input.append(new Option(label, value)));
  const summary = control("发生了什么", "summary", "textarea");
  const reason = control("记录原因", "reason", "textarea");
  const submit = node("button", "记录结果");
  submit.type = "submit";
  const feedback = node("p", "", "form-feedback");
  form.append(adopted.wrapper, result.wrapper, summary.wrapper, reason.wrapper, submit, feedback);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    pending(submit, true, "正在记录……", "记录结果");
    try {
      await mutate(`/api/v2/contexts/${encodeURIComponent(detail.context_id)}/outcomes`, {
        adopted: adopted.input.value,
        result: result.input.value,
        summary: summary.input.value,
        expected_version: detail.revision,
        reason: reason.input.value,
      });
      feedback.textContent = "结果已记录";
      closeDialog();
      await refresh();
    } catch (error) {
      feedback.textContent = explainApiError(error);
      pending(submit, false, "正在记录……", "再次尝试");
    }
  });
  return form;
}

async function showContext(id, trigger, refresh) {
  const body = openDialog("Context 详情", (target) => target.append(node("p", "正在读取 Context……", "state-message")), trigger);
  try {
    const detail = await api(`/api/v2/contexts/${encodeURIComponent(id)}`);
    const content = node("div", "", "context-detail");
    content.append(node("p", STATES[detail.lifecycle_status] || detail.lifecycle_status || "状态未知", "status-chip"), node("h3", detail.task || "未命名任务"));
    if (detail.lifecycle_status === "preview") content.append(previewBody(detail, refresh));
    if (detail.context_markdown) {
      const markdown = node("pre", detail.context_markdown, "context-markdown");
      markdown.setAttribute("aria-label", "实际交给 Agent 的 Context");
      content.append(markdown);
    }
    if (detail.lifecycle_status === "compiled") {
      const consume = node("button", "标记为已交给 Agent");
      consume.type = "button";
      const feedback = node("p", "", "form-feedback");
      consume.addEventListener("click", async () => {
        pending(consume, true, "正在记录……", "标记为已交给 Agent");
        try {
          await mutate(`/api/v2/contexts/${encodeURIComponent(detail.context_id)}/consume`, { expected_version: detail.revision, reason: "Context 已交给 Agent" });
          closeDialog();
          await refresh();
        } catch (error) {
          feedback.textContent = explainApiError(error);
          pending(consume, false, "正在记录……", "再次尝试");
        }
      });
      content.append(consume, feedback);
    }
    if (detail.lifecycle_status === "consumed") content.append(node("h3", "待记录结果"), outcomeForm(detail, refresh));
    if (detail.outcome) content.append(node("p", `${detail.outcome.result}：${detail.outcome.summary || "无摘要"}`, "outcome-summary"));
    body.replaceChildren(content);
  } catch (error) {
    body.replaceChildren(node("p", error.message || "Context 读取失败", "error-text"));
  }
}

function newPreview(trigger, refresh) {
  openDialog("准备新的 Context", (body) => {
    const form = node("form", "", "action-form");
    const task = control("这次要完成什么", "task", "textarea");
    const mode = control("使用方式", "mode", "select");
    MODES.forEach((value) => mode.input.append(new Option(value, value)));
    const reason = control("为什么需要这份 Context", "reason", "textarea");
    const submit = node("button", "生成预览");
    submit.type = "submit";
    const feedback = node("p", "准备中", "form-feedback");
    form.append(task.wrapper, mode.wrapper, reason.wrapper, submit, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      pending(submit, true, "正在生成……", "生成预览");
      try {
        const preview = await mutate("/api/v2/contexts/preview", { task: task.input.value, mode: mode.input.value, expected_version: 0, reason: reason.input.value });
        feedback.textContent = "预览完成";
        body.replaceChildren(previewBody(preview, refresh));
      } catch (error) {
        feedback.textContent = `失败：${explainApiError(error)}`;
        pending(submit, false, "正在生成……", "再次尝试");
      }
    });
    body.append(form);
  }, trigger);
}

export async function renderUse(root, { signal, isCurrent, navigate }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(node("p", "正在读取 Context 生命周期……", "state-message"));
  const refresh = () => navigate("use");
  try {
    const page = await api("/api/v2/contexts?limit=20", { signal });
    if (!isCurrent()) return;
    const fragment = document.createDocumentFragment();
    const heading = node("header", "", "view-heading");
    heading.append(node("p", "CONTEXT USE · 按任务使用记忆", "kicker"), node("h1", "需要时带上过去，用完后留下结果。"), node("p", "每份 Context 先预览，再确认，再交给 Agent，最后把真实结果写回。", "lede"));
    fragment.append(heading);
    const create = node("button", "准备新的 Context");
    create.type = "button";
    create.addEventListener("click", () => newPreview(create, refresh));
    fragment.append(create);
    const list = node("div", "", "context-list");
    (page.items || []).forEach((item) => {
      const card = node("article", "", "context-card");
      card.append(node("p", STATES[item.lifecycle_status] || item.lifecycle_status || "状态未知", "status-chip"), node("h2", item.task || "未命名任务"), node("small", `${formatTimestamp(item.updated_at)} · ${item.mode || "模式未知"}`));
      const open = node("button", "查看并继续", "text-button");
      open.type = "button";
      open.addEventListener("click", () => showContext(item.context_id || item.preview_id, open, refresh));
      card.append(open);
      list.append(card);
    });
    if (!list.children.length) list.append(node("p", "还没有 Context。你可以先准备一份预览。", "state-message"));
    fragment.append(list);
    root.replaceChildren(fragment);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    root.replaceChildren(node("p", `失败：${error.message || "Context 当前不可读"}`, "state-panel error-state"));
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
