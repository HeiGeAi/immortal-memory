import { api } from "../api.js";
import { openDialog } from "../dialog.js";
import { formatTimestamp } from "../format.js";

function node(tag, value = "", className = "") {
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = value;
  return result;
}

function coverageWarning(page, filters) {
  if (page.coverage_complete !== false) return null;
  const labels = { person: "人物", project: "项目", topic: "主题" };
  const incomplete = Object.keys(labels).filter(
    (key) => filters[key] && page.coverage?.[key]?.complete !== true
  );
  const dimensions = incomplete.map((key) => labels[key]).join("、") || "当前筛选维度";
  return node(
    "p",
    `${dimensions}的索引覆盖尚不完整。以下结果只代表已覆盖范围，空结果不等于没有相关记忆。`,
    "coverage-warning",
  );
}

function labeledInput(name, label, value, type = "text") {
  const wrapper = document.createElement("label");
  wrapper.className = "field-label";
  wrapper.append(node("span", label));
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.value = value || "";
  if (name === "q") input.minLength = 3;
  wrapper.append(input);
  return wrapper;
}

function appendFact(parent, label, value) {
  if (value === undefined || value === null || value === "") return;
  const row = document.createElement("div");
  row.className = "detail-row";
  row.append(node("dt", label), node("dd", String(value)));
  parent.append(row);
}

async function showDetail(id, trigger) {
  const body = openDialog("记忆证据", (target) => target.append(node("p", "正在读取原始记录……", "state-message")), trigger);
  try {
    const detail = await api(`/api/v2/memories/${encodeURIComponent(id)}`);
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
    body.replaceChildren(node("p", error.message || "详情读取失败", "state-message error-text"));
  }
}

function memoryCard(item) {
  const article = document.createElement("article");
  article.className = "memory-card";
  const time = document.createElement("time");
  time.dateTime = item.timestamp || "";
  time.textContent = formatTimestamp(item.timestamp);
  const source = node("span", item.source || "来源未知", "source-mark");
  const meta = document.createElement("div");
  meta.className = "memory-meta";
  meta.append(time, source);
  article.append(meta, node("h2", item.project || item.role || "未命名记录"), node("p", item.summary || "无摘要"));
  const detail = document.createElement("button");
  detail.type = "button";
  detail.className = "text-button";
  detail.textContent = "查看证据与详情";
  detail.addEventListener("click", () => showDetail(item.id, detail));
  article.append(detail);
  return article;
}

export async function renderMemories(root, { route, signal, isCurrent, navigate }) {
  const accepted = ["q", "source", "person", "project", "topic", "from", "to"];
  const filters = Object.fromEntries(accepted.map((key) => [key, route.params.get(key) || ""]));
  const shell = document.createElement("div");
  const heading = document.createElement("header");
  heading.className = "view-heading";
  heading.append(node("p", "ARCHIVE INDEX · 记忆索引", "kicker"), node("h1", "记忆不是列表，是有出处的时间。"), node("p", "筛选只交给真实索引执行。每条结果都可以回到来源、时间与完整记录。", "lede"));
  const form = document.createElement("form");
  form.className = "filter-form";
  form.append(
    labeledInput("q", "搜索，至少 3 个字符", filters.q),
    labeledInput("source", "来源", filters.source),
    labeledInput("project", "项目", filters.project),
    labeledInput("person", "人物", filters.person),
    labeledInput("topic", "主题", filters.topic),
    labeledInput("from", "起始时间，含时区", filters.from),
    labeledInput("to", "结束时间，含时区", filters.to),
  );
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "应用筛选";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    navigate("memories", values);
  });
  const results = document.createElement("div");
  results.className = "memory-stack";
  shell.append(heading, form, results);
  root.replaceChildren(shell);
  root.setAttribute("aria-busy", "true");
  results.append(node("p", "正在沿时间轴读取……", "state-message"));

  const query = new URLSearchParams({ limit: "20" });
  Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
  try {
    const first = await api(`/api/v2/memories?${query.toString()}`, { signal });
    if (!isCurrent()) return;
    results.replaceChildren();
    const warning = coverageWarning(first, filters);
    if (warning) results.append(warning);
    const items = Array.isArray(first.items) ? first.items : [];
    if (!items.length) {
      const message = first.coverage_complete === false
        ? "当前已覆盖范围内没有找到结果，不能据此判断相关记忆不存在。"
        : "当前筛选没有找到记忆。换一个范围，档案本身不会被改动。";
      results.append(node("p", message, "state-panel empty-state"));
      return;
    }
    items.forEach((item) => results.append(memoryCard(item)));
    let cursor = first.next_cursor || "";
    if (first.has_more && cursor) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "load-more";
      more.textContent = "沿时间继续加载";
      more.addEventListener("click", async () => {
        more.disabled = true;
        more.textContent = "正在加载……";
        query.set("cursor", cursor);
        try {
          const page = await api(`/api/v2/memories?${query.toString()}`, { signal });
          if (!isCurrent()) return;
          (page.items || []).forEach((item) => results.insertBefore(memoryCard(item), more));
          cursor = page.next_cursor || "";
          if (!page.has_more || !cursor) more.remove();
          else { more.disabled = false; more.textContent = "沿时间继续加载"; }
        } catch (error) {
          more.disabled = false;
          more.textContent = "加载失败，重试";
          more.title = error.message || "加载失败";
        }
      });
      results.append(more);
    }
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrent()) return;
    const failure = node("div", "", "state-panel error-state");
    failure.append(node("h2", "记忆索引暂时不可读"), node("p", error.message || "未知错误"));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重试当前筛选";
    retry.addEventListener("click", () => navigate("memories", filters));
    failure.append(retry);
    results.replaceChildren(failure);
  } finally {
    if (isCurrent()) root.setAttribute("aria-busy", "false");
  }
}
