import { api } from "../api.js";
import { openDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

const LABELS = {
  status: "状态",
  status_label: "状态说明",
  version: "当前版本",
  attention: "需要关注",
  memory_index: "记忆索引",
  living_self: "自我档案",
  context_compile: "上下文生成",
  active: "已启用来源",
  last_collection: "最近采集",
  last_verified: "最近核验",
  event_stream: "事件连续性",
  index_integrity: "索引完整性",
};

const VALUES = {
  healthy: "连续性正常",
  ready: "可以使用",
  verified: "已经核验",
  unknown: "暂时未知",
  unavailable: "当前不可用",
  partial: "部分成功",
  attention: "需要关注",
  failed: "运行失败",
  sent: "已送达",
  skipped: "未请求",
};

const JOB_KINDS = {
  run: "运行完整记忆流程",
  health: "健康检查",
  backup_verify: "备份核验",
  profile_refresh: "刷新长期画像",
};

const JOB_STATUS = {
  queued: "等待开始",
  running: "正在运行",
  cancel_requested: "等待安全停止",
  success: "运行成功",
  attention: "完成，需要关注",
  failed: "运行失败",
  canceled: "已安全停止",
  interrupted: "运行被中断",
};

const ACTIONS = [
  ["run", "运行全流程", "采集并执行 Immortal 的真实记忆流程。已有编排器运行时，服务会拒绝重复启动。"],
  ["health", "检查健康", "检查最近运行、索引与关键产物，不会改写记忆正文。"],
  ["backup_verify", "核验备份", "读取并校验最近的便携备份，不会执行恢复。"],
  ["profile_refresh", "刷新画像", "重新生成长期画像、Nuwa 画像与质量报告。"],
];

const CLOUD_RECOVERY_REASON = {
  cloud_not_configured: "尚未发现飞书异地恢复配置或完成记录。",
  cloud_recovery_drill_pending: "已发现加密上传记录，但尚未完成远端下载后的恢复演练。",
  cloud_upload_receipt_invalid: "上传记录不可作为恢复依据，需要重新核对。",
  cloud_drill_receipt_missing_or_unsafe: "恢复演练记录不满足本机安全校验。",
  cloud_drill_receipt_invalid: "恢复演练证据格式不完整或已失效。",
  cloud_source_index_unreadable: "当前记忆索引不可安全读取，无法确认恢复点。",
  cloud_source_index_hash_mismatch: "当前记忆已变化，恢复演练对应的恢复点不再是最新。",
  cloud_vault_unsafe: "本机记忆库状态无法作为恢复依据。",
  cloud_recovery_validator_unavailable: "恢复证据校验组件当前不可用。",
  cloud_recovery_status_unavailable: "当前无法读取恢复证据，不把它当作已验证。",
};

const CLOUD_RECOVERY_BINDING = {
  matched: "与当前记忆一致",
  mismatch: "当前记忆已变化",
  missing: "尚未建立",
};

const AUTOMATION_PIPELINE_STATUS = {
  ok: "正常",
  partial: "部分成功",
  attention: "需要关注",
  failed: "运行失败",
  unknown: "证据不足",
};

const NOTIFICATION_STATUS = {
  sent: "已送达",
  skipped: "未请求",
  failed: "未送达",
  unknown: "不可核验",
};

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function flatten(value, prefix = "", result = []) {
  if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, child]) => {
      const label = LABELS[key] || key.replaceAll("_", " ");
      flatten(child, prefix ? `${prefix} · ${label}` : label, result);
    });
  } else {
    let content = value === null || value === undefined ? "未知" : String(value);
    if (VALUES[content]) content = VALUES[content];
    if (/^\d{4}-\d{2}-\d{2}T/.test(content)) content = formatTimestamp(content);
    result.push([prefix, content]);
  }
  return result.slice(0, 80);
}

function appendFact(parent, label, value) {
  if (value === undefined || value === null || value === "") return;
  const row = document.createElement("div");
  row.className = "detail-row";
  row.append(node("dt", label), node("dd", String(value)));
  parent.append(row);
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

function cloudRecoveryState(cloud = {}) {
  if (cloud.status === "verified") return "已验证可恢复";
  if (cloud.reason_code === "cloud_not_configured") return "未配置";
  if (cloud.reason_code === "cloud_recovery_drill_pending") return "等待恢复演练";
  return "证据失效";
}

function cloudRecoveryCard(cloud = {}) {
  const card = document.createElement("section");
  card.className = "state-panel cloud-recovery-card";
  card.dataset.status = cloud.status || "unknown";
  const state = cloudRecoveryState(cloud);
  const details = document.createElement("dl");
  details.className = "detail-list";
  appendFact(details, "当前状态", state);
  appendFact(details, "云端位置", cloud.provider || "未配置");
  appendFact(
    details,
    "最近验证",
    cloud.last_verified_at ? formatTimestamp(cloud.last_verified_at) : "尚未验证",
  );
  appendFact(
    details,
    "验证方式",
    cloud.verification === "remote-download-sha256+decrypt-restore"
      ? "远端下载、校验、解密并恢复"
      : "尚未建立",
  );
  appendFact(
    details,
    "当前记忆绑定",
    CLOUD_RECOVERY_BINDING[cloud.source_binding] || "尚未建立",
  );
  const reason = CLOUD_RECOVERY_REASON[cloud.reason_code] || "";
  card.append(
    node("p", "OFFSITE RECOVERY · 异地恢复", "kicker"),
    node("h2", "飞书异地恢复"),
    node("p", state, cloud.status === "verified" ? "state-message" : "coverage-warning"),
    node("p", reason || "恢复点已通过远端下载和真实恢复演练核验。", "state-message"),
    details,
    node("p", `下一步：${cloud.action || "读取最新证据"}`, "state-message"),
  );
  return card;
}

function feedbackEvidenceValue(evidence = "", key) {
  const prefix = `${key}=`;
  const part = String(evidence)
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));
  return part ? part.slice(prefix.length) : "unknown";
}

function automationFeedbackCard(feedback = {}) {
  const card = document.createElement("section");
  const status = feedback.status || "unknown";
  const pipeline = feedbackEvidenceValue(feedback.evidence, "pipeline");
  const notification = feedbackEvidenceValue(feedback.evidence, "notification");
  const runStatus = feedbackEvidenceValue(feedback.evidence, "run_status");
  card.className = "state-panel automation-feedback-card";
  card.dataset.status = status;
  const details = document.createElement("dl");
  details.className = "detail-list";
  appendFact(details, "自动化反馈", VALUES[status] || status);
  appendFact(details, "最近报告", feedback.observed_at ? formatTimestamp(feedback.observed_at) : "尚未生成");
  appendFact(details, "主流程结果", AUTOMATION_PIPELINE_STATUS[pipeline] || "证据不足");
  appendFact(details, "主流程退出码", runStatus === "unknown" ? "未记录" : runStatus);
  appendFact(details, "通知投递", NOTIFICATION_STATUS[notification] || "不可核验");
  card.append(
    node("p", "AUTOMATION FEEDBACK · 自动化反馈", "kicker"),
    node("h2", "最后一次自动化结果"),
    node("p", feedback.detail || "尚未生成可复核的自动化反馈。", status === "healthy" ? "state-message" : "coverage-warning"),
    details,
    node("p", "采集、反馈和通知各自保留状态，不会把主流程成功误作整条自动化成功。", "state-message"),
  );
  return card;
}

function commandLabel(command = "") {
  const value = String(command);
  if (value.includes("backup-status")) return "核验备份";
  if (value.includes("profile-nuwa")) return "生成 Nuwa 画像";
  if (value.includes("profile") && !value.includes("profile-nuwa")) return "生成长期画像";
  if (value.includes("quality")) return "检查画像质量";
  if (value.includes(" health")) return "检查系统健康";
  if (value.includes(" run")) return "采集并整理记忆";
  return "执行受控阶段";
}

function enteredStageCount(job) {
  const commands = Array.isArray(job.commands) ? job.commands : [];
  if (!commands.length || job.status === "queued") return 0;
  if (job.status === "success" || job.status === "attention") return commands.length;
  const output = String(job.stdout || "");
  return commands.reduce((count, command) => count + (output.includes(`$ ${command}`) ? 1 : 0), 0);
}

function currentStage(job) {
  const commands = Array.isArray(job.commands) ? job.commands : [];
  const entered = enteredStageCount(job);
  if (job.status === "queued") return "等待服务调度";
  if (!commands.length) return "服务端尚未报告阶段";
  if (job.status === "success" || job.status === "attention") return "所有受控阶段已结束";
  if (!entered) return "正在准备受控命令";
  const current = commands[Math.min(entered, commands.length) - 1];
  return commandLabel(current);
}

function progressDescription(job) {
  const commands = Array.isArray(job.commands) ? job.commands : [];
  const entered = enteredStageCount(job);
  if (!commands.length) return "精确进度暂不可用";
  if (job.status === "success" || job.status === "attention") return `${commands.length}/${commands.length} 个阶段已执行`;
  if (job.status === "queued") return `0/${commands.length} 个阶段已进入`;
  return `${entered}/${commands.length} 个阶段已进入，服务端未提供完成百分比`;
}

function openJob(jobId, trigger) {
  const body = openDialog("运行记录", (target) => target.append(node("p", "正在读取任务和脱敏日志……", "state-message")), trigger);
  Promise.all([
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}`),
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs?limit=8000`),
  ]).then(([job, logs]) => {
    const fragment = document.createDocumentFragment();
    fragment.append(node("p", `${JOB_KINDS[job.kind] || job.kind || "任务"} · ${JOB_STATUS[job.status] || job.status || "状态未知"}`, "kicker"));
    const details = document.createElement("dl");
    details.className = "detail-list";
    appendFact(details, "任务编号", job.id);
    appendFact(details, "当前阶段", currentStage(job));
    appendFact(details, "真实进度", progressDescription(job));
    appendFact(details, "创建时间", formatTimestamp(job.created_at));
    appendFact(details, "开始时间", formatTimestamp(job.started_at));
    appendFact(details, "结束时间", formatTimestamp(job.finished_at));
    appendFact(details, "耗时", Number.isFinite(job.elapsed_seconds) ? `${job.elapsed_seconds} 秒` : "尚未结束");
    appendFact(details, "结果说明", job.summary);
    appendFact(details, "失败原因", job.error || job.error_code);
    fragment.append(details, node("h3", "脱敏运行日志"));
    const log = node("pre", logs.text || "服务端尚未产生运行日志。", "context-markdown");
    fragment.append(log);
    body.replaceChildren(fragment);
  }).catch((error) => {
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h3", "运行记录暂时不可读"), node("p", error.message || "未知错误"));
    body.replaceChildren(failure);
  });
}

function jobCard(job) {
  const card = document.createElement("article");
  card.className = "context-card";
  const status = JOB_STATUS[job.status] || job.status || "状态未知";
  card.append(node("p", `${status} · ${formatTimestamp(job.created_at)}`, "kicker"));
  card.append(node("h2", JOB_KINDS[job.kind] || job.kind || "未知任务"));
  card.append(node("p", `阶段：${currentStage(job)}`));
  card.append(node("p", `进度：${progressDescription(job)}`, "state-message"));
  if (job.summary) card.append(node("p", job.summary, "state-message"));
  if (job.error || job.error_code) card.append(node("p", `失败原因：${job.error || job.error_code}`, "error-text"));
  const actions = document.createElement("div");
  actions.className = "honest-actions";
  const detail = document.createElement("button");
  detail.type = "button";
  detail.className = "secondary";
  detail.textContent = "查看运行记录";
  detail.addEventListener("click", () => openJob(job.id, detail));
  actions.append(detail);
  card.append(actions);
  return card;
}

function renderJobs(target, payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const fragment = document.createDocumentFragment();
  const header = document.createElement("header");
  header.className = "view-heading";
  header.append(node("p", "LIVE JOBS · 真实运行", "kicker"), node("h2", "当前与最近运行"), node("p", "阶段进入度来自服务端任务记录，不代表完成百分比。精确阶段不可用时，面板会明确说明。", "state-message"));
  fragment.append(header);
  if (!items.length) {
    fragment.append(node("p", "还没有由本机控制中心记录的任务。", "state-panel"));
  } else {
    const list = document.createElement("div");
    list.className = "context-list";
    items.slice(0, 8).forEach((job) => list.append(jobCard(job)));
    fragment.append(list);
  }
  target.replaceChildren(fragment);
}

async function reloadJobs(target, { isCurrent } = {}) {
  target.setAttribute("aria-busy", "true");
  try {
    const jobs = await api("/api/v1/jobs");
    if (isCurrent && !isCurrent()) return;
    renderJobs(target, jobs);
  } catch (error) {
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h3", "运行状态暂时不可读"), node("p", error.message || "未知错误"));
    target.replaceChildren(failure);
  } finally {
    target.setAttribute("aria-busy", "false");
  }
}

function openAction(kind, description, jobsTarget, trigger, isCurrent) {
  const title = JOB_KINDS[kind] || kind;
  openDialog(title, (body) => {
    body.append(node("p", description, "state-message"));
    body.append(node("p", "提交后只表示服务端已接收任务。最终结果以运行记录为准。", "coverage-warning"));
    const feedback = node("p", "", "form-feedback");
    feedback.setAttribute("role", "status");
    const actions = document.createElement("div");
    actions.className = "honest-actions";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.textContent = "确认提交";
    submit.addEventListener("click", async () => {
      submit.disabled = true;
      submit.className = "is-pending";
      submit.textContent = "正在提交……";
      feedback.textContent = "";
      try {
        const job = await api("/api/v1/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind, params: {} }),
        });
        submit.className = "is-success";
        submit.textContent = "服务端已接收";
        feedback.textContent = `任务 ${job.id} 当前状态：${JOB_STATUS[job.status] || job.status || "未知"}。`;
        if (isCurrent()) await reloadJobs(jobsTarget, { isCurrent });
      } catch (error) {
        const outcomeUnknown = !error.status || error.status >= 500;
        if (outcomeUnknown) {
          submit.disabled = true;
          submit.className = "is-failure";
          submit.textContent = "提交结果未知，请先刷新";
          feedback.textContent = "连接中断，任务可能已经建立。为避免重复运行，请关闭窗口并读取最新状态。";
          if (isCurrent()) await reloadJobs(jobsTarget, { isCurrent });
        } else {
          submit.disabled = false;
          submit.className = "is-failure";
          submit.textContent = "服务端未接收，可重试";
          feedback.textContent = error.message || "服务端拒绝了这次操作";
        }
      }
    });
    actions.append(submit);
    body.append(actions, feedback);
  }, trigger);
}

function actionPanel(jobsTarget, isCurrent, { actionsAvailable = true, actionReason = "" } = {}) {
  const section = document.createElement("section");
  section.className = "state-panel";
  section.append(node("p", "CONTROLLED ACTIONS · 受控操作", "kicker"), node("h2", "让系统实际运行"), node("p", "这里仅提供后端固定白名单中的四项操作。每次执行都会建立真实任务记录，并保留脱敏日志。", "state-message"));
  const actions = document.createElement("div");
  actions.className = "honest-actions";
  if (!actionsAvailable) {
    section.append(node("p", actionReason || "当前服务不允许执行受控命令。", "coverage-warning"));
  } else {
    ACTIONS.forEach(([kind, label, description]) => {
      const button = document.createElement("button");
      button.type = "button";
      if (kind !== "run") button.className = "secondary";
      button.textContent = label;
      button.addEventListener("click", () => openAction(kind, description, jobsTarget, button, isCurrent));
      actions.append(button);
    });
  }
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "secondary";
  refresh.textContent = "读取最新状态";
  refresh.addEventListener("click", () => reloadJobs(jobsTarget, { isCurrent }));
  actions.append(refresh);
  section.append(actions);
  return section;
}

export async function renderSystem(root, { signal, isCurrent, navigate, updateHealth }) {
  root.setAttribute("aria-busy", "true");
  root.replaceChildren(node("p", "正在核对系统依据与真实运行……", "state-message"));
  const [systemResult, jobsResult] = await Promise.allSettled([
    api("/api/v2/system", { signal }),
    api("/api/v1/jobs", { signal }),
  ]);
  if (!isCurrent()) return;

  const fragment = document.createDocumentFragment();
  const heading = document.createElement("header");
  heading.className = "view-heading";
  heading.append(node("p", "SYSTEM EVIDENCE · 系统依据", "kicker"), node("h1", "健康不是绿灯，是可以复核的运行过程。"), node("p", "看到任务是否真的开始、走到哪里、为什么失败，再用独立依据复核能力、来源、备份与诊断。", "lede"));
  fragment.append(heading);

  const jobsTarget = document.createElement("section");
  jobsTarget.setAttribute("aria-label", "真实运行记录");
  const capability = systemResult.status === "fulfilled" ? systemResult.value?.capabilities : null;
  fragment.append(actionPanel(jobsTarget, isCurrent, {
    actionsAvailable: capability?.actions_available !== false,
    actionReason: capability?.action_reason || "",
  }), jobsTarget);
  if (jobsResult.status === "fulfilled") renderJobs(jobsTarget, jobsResult.value);
  else {
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "运行状态暂时不可读"), node("p", jobsResult.reason?.message || "未知错误"));
    jobsTarget.replaceChildren(failure);
  }

  if (systemResult.status === "fulfilled") {
    const data = systemResult.value;
    updateHealth(data.health?.status_label || data.health?.status || "连续性未知", data.health?.status);
    const evidenceHeading = document.createElement("header");
    evidenceHeading.className = "view-heading";
    evidenceHeading.append(node("p", "VERIFIABLE EVIDENCE · 可复核依据", "kicker"), node("h2", "五类依据彼此独立"));
    fragment.append(evidenceHeading);
    fragment.append(automationFeedbackCard(data.health?.feedback || {}));
    fragment.append(cloudRecoveryCard(data.backups?.cloud_recovery || {}));
    const grid = document.createElement("div");
    grid.className = "system-grid";
    const backupEvidence = { ...(data.backups || {}) };
    delete backupEvidence.cloud_recovery;
    [["健康", data.health], ["能力", data.capabilities], ["来源", data.sources], ["备份", backupEvidence], ["诊断", data.diagnostics]].forEach(([title, value]) => grid.append(systemSection(title, value)));
    fragment.append(grid);
  } else {
    updateHealth("连续性未知", "unknown");
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "系统依据暂时不可读"), node("p", systemResult.reason?.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重新核对全部依据";
    retry.addEventListener("click", () => navigate("system"));
    failure.append(retry);
    fragment.append(failure);
  }

  root.replaceChildren(fragment);
  root.setAttribute("aria-busy", "false");
}
