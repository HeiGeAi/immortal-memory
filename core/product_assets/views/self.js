import { api, mutate, explainApiError } from "../api.js";
import { openDialog, closeDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

const SECTION_LABELS = {
  identity_commitments: "身份与承诺",
  values: "价值取向",
  expression_dna: "表达方式",
  mental_models: "思考模型",
  decision_heuristics: "决策习惯",
  anti_patterns: "应避免的模式",
  tensions: "仍在拉扯的部分",
  honest_boundaries: "诚实边界",
};

function node(tag, value = "", className = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function field(label, name, value = "", tag = "input") {
  const wrapper = document.createElement("label");
  wrapper.className = "field-label";
  wrapper.append(node("span", label));
  const control = document.createElement(tag);
  control.name = name;
  control.value = value;
  control.required = true;
  wrapper.append(control);
  return wrapper;
}

function setButton(button, pending, pendingText, idleText) {
  button.disabled = pending;
  button.classList.toggle("is-pending", pending);
  button.textContent = pending ? pendingText : idleText;
}

async function correctItem(detail, selfVersion, trigger, refresh) {
  const claimRefs = Array.isArray(detail.claim_refs) ? detail.claim_refs : [];
  const body = openDialog("纠正这条自我理解", (target) => {
    if (!claimRefs.length) {
      target.append(node("p", "这条理解缺少可验证的 Claim 版本，当前不能安全纠正。", "coverage-warning"));
      return;
    }
    const form = document.createElement("form");
    form.className = "action-form";
    const claimLabel = document.createElement("label");
    claimLabel.className = "field-label";
    claimLabel.append(node("span", "要纠正的依据"));
    const select = document.createElement("select");
    select.name = "claim";
    claimRefs.forEach((claim) => {
      const option = document.createElement("option");
      option.value = claim.claim_id;
      option.dataset.revision = String(claim.revision);
      option.textContent = claim.statement || claim.claim_id;
      select.append(option);
    });
    claimLabel.append(select);
    const statement = field("正确的说法", "statement", detail.summary || "", "textarea");
    const reason = field("为什么要纠正", "reason", "", "textarea");
    const submit = node("button", "确认纠正");
    submit.type = "submit";
    const feedback = node("p", "", "form-feedback");
    form.append(claimLabel, statement, reason, submit, feedback);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selected = select.selectedOptions[0];
      setButton(submit, true, "正在保存……", "确认纠正");
      try {
        await mutate(`/api/v2/self/items/${encodeURIComponent(detail.item_id)}/actions`, {
          action: "correct",
          claim_id: select.value,
          expected_self_version: selfVersion,
          expected_version: Number(selected.dataset.revision),
          statement: statement.querySelector("textarea").value,
          reason: reason.querySelector("textarea").value,
        });
        feedback.textContent = "纠正已经保存，正在重新读取自我档案。";
        closeDialog();
        await refresh();
      } catch (error) {
        feedback.textContent = explainApiError(error);
        setButton(submit, false, "正在保存……", "再次尝试");
      }
    });
    target.append(form);
  }, trigger);
  return body;
}

async function showItem(itemId, selfVersion, trigger, refresh) {
  const body = openDialog("自我理解与依据", (target) => target.append(node("p", "正在读取依据……", "state-message")), trigger);
  try {
    const detail = await api(`/api/v2/self/items/${encodeURIComponent(itemId)}`);
    const list = document.createElement("dl");
    list.className = "detail-list";
    [["内容", detail.summary], ["可信度", detail.confidence], ["适用范围", (detail.scope || []).join("、")], ["失效条件", (detail.failure_conditions || []).join("、")], ["证据", (detail.evidence_ids || []).join("、")], ["反证", (detail.counter_evidence_ids || []).join("、")]].forEach(([label, value]) => {
      if (value === undefined || value === null || value === "") return;
      const row = document.createElement("div");
      row.className = "detail-row";
      row.append(node("dt", label), node("dd", String(value)));
      list.append(row);
    });
    const correct = node("button", "纠正这条理解", "secondary");
    correct.type = "button";
    correct.disabled = !Array.isArray(detail.claim_refs) || !detail.claim_refs.length;
    if (correct.disabled) correct.title = "没有可验证的 Claim 版本，暂时不能纠正";
    correct.addEventListener("click", () => correctItem(detail, selfVersion, correct, refresh));
    body.replaceChildren(list, correct);
  } catch (error) {
    body.replaceChildren(node("p", error.message || "自我理解读取失败", "error-text"));
  }
}

async function showVersions(currentVersion, trigger, refresh) {
  const body = openDialog("自我档案版本", (target) => target.append(node("p", "正在读取版本……", "state-message")), trigger);
  try {
    const page = await api("/api/v2/self/versions?limit=20");
    const list = node("div", "", "version-list");
    (page.items || []).forEach((version) => {
      const card = node("article", "", "version-card");
      card.append(node("h3", formatTimestamp(version.confirmed_at || version.generated_at)), node("p", `版本 ${version.version_id}`));
      if (version.version_id !== currentVersion) {
        const restore = node("button", "恢复到这个版本", "secondary");
        restore.type = "button";
        restore.addEventListener("click", () => {
          openDialog("确认恢复自我档案", (target) => {
            const form = node("form", "", "action-form");
            form.append(node("p", `将恢复到 ${formatTimestamp(version.confirmed_at || version.generated_at)}。恢复会产生一个新版本，不会删除之后的历史。`));
            const reason = field("为什么要恢复", "reason", "", "textarea");
            const submit = node("button", "确认恢复");
            submit.type = "submit";
            const feedback = node("p", "", "form-feedback");
            form.append(reason, submit, feedback);
            form.addEventListener("submit", async (event) => {
              event.preventDefault();
              setButton(submit, true, "正在恢复……", "确认恢复");
              try {
                await mutate(`/api/v2/self/versions/${encodeURIComponent(version.version_id)}/restore`, {
                  expected_version: currentVersion,
                  reason: reason.querySelector("textarea").value,
                });
                closeDialog();
                await refresh();
              } catch (error) {
                feedback.textContent = explainApiError(error);
                setButton(submit, false, "正在恢复……", "再次尝试");
              }
            });
            target.append(form);
          }, restore);
        });
        card.append(restore);
      } else card.append(node("p", "当前版本", "status-chip"));
      list.append(card);
    });
    body.replaceChildren(list);
  } catch (error) {
    body.replaceChildren(node("p", error.message || "版本读取失败", "error-text"));
  }
}

export async function renderSelf(root, { signal, isCurrent, navigate }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(node("p", "正在读取持续形成的自我理解……", "state-message"));
  const refresh = () => navigate("self");
  try {
    const data = await api("/api/v2/self", { signal });
    if (!isCurrent()) return;
    const fragment = document.createDocumentFragment();
    const heading = node("header", "", "view-heading");
    heading.append(node("p", "LIVING SELF · 持续形成的我", "kicker"), node("h1", "这里不是定论，是可以纠正的理解。"), node("p", "每一条理解都保留证据、反证、适用范围与版本。系统不会把局部样本写成永恒人格。", "lede"));
    fragment.append(heading);
    if (data.truncated) fragment.append(node("p", `当前展示 ${data.returned || 50} 条，共 ${data.total} 条。其余内容未被当作不存在。`, "coverage-warning"));
    const toolbar = node("div", "", "honest-actions");
    const versions = node("button", "查看版本与恢复", "secondary");
    versions.type = "button";
    versions.addEventListener("click", () => showVersions(data.version_id, versions, refresh));
    toolbar.append(versions, node("span", `当前版本：${data.version_id}`, "status-chip"));
    fragment.append(toolbar);
    const sections = node("div", "", "self-sections");
    Object.entries(data.sections || {}).forEach(([key, items]) => {
      const section = node("section", "", "self-section");
      section.append(node("h2", SECTION_LABELS[key] || key));
      if (!Array.isArray(items) || !items.length) section.append(node("p", "当前没有足够证据形成内容。", "state-message"));
      (items || []).forEach((item) => {
        const card = node("article", "", "self-card");
        card.append(node("h3", item.title || "未命名理解"), node("p", item.summary || "无摘要"), node("small", `可信度 ${item.confidence ?? "未知"} · ${item.status || "状态未知"}`));
        const detail = node("button", "查看依据并纠正", "text-button");
        detail.type = "button";
        detail.addEventListener("click", () => showItem(item.item_id, data.version_id, detail, refresh));
        card.append(detail);
        section.append(card);
      });
      sections.append(section);
    });
    fragment.append(sections);
    root.replaceChildren(fragment);
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    const panel = node("div", "", "state-panel error-state");
    panel.append(node("h2", "自我档案暂时不可读"), node("p", error.message || "未知错误"));
    root.replaceChildren(panel);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
