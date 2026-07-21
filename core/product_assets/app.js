import { createRouter } from "./router.js";
import { closeDialog } from "./dialog.js";
import { renderHome } from "./views/home.js";
import { renderMemories } from "./views/memories.js";
import { renderSystem } from "./views/system.js";

const root = document.getElementById("view");
const routeStatus = document.getElementById("route-status");
const globalHealth = document.getElementById("global-health");
const labels = { home: "首页", memories: "记忆", self: "我", judgments: "判断", use: "使用", trust: "信任", system: "系统" };
const updateHealth = (label, status = "unknown") => {
  globalHealth.textContent = label || "连续性未知";
  globalHealth.dataset.status = status || "unknown";
};

function unavailable(view) {
  return async () => {
    const panel = document.createElement("section");
    panel.className = "state-panel unavailable-state";
    const kicker = document.createElement("p");
    kicker.className = "kicker";
    kicker.textContent = "CONNECTION PENDING · 尚未接入";
    const title = document.createElement("h1");
    title.textContent = `${labels[view]}模块尚未接入`;
    const copy = document.createElement("p");
    copy.textContent = "这个入口保留产品结构，但当前版本没有可验证的数据链路或操作，因此不会展示模拟按钮。";
    panel.append(kicker, title, copy);
    root.replaceChildren(panel);
    root.setAttribute("aria-busy", "false");
  };
}

const router = createRouter({
  home: (context) => renderHome(root, { ...context, updateHealth }),
  memories: (context) => renderMemories(root, context),
  self: unavailable("self"),
  judgments: unavailable("judgments"),
  use: unavailable("use"),
  trust: unavailable("trust"),
  system: (context) => renderSystem(root, { ...context, updateHealth }),
}, ({ view }) => {
  closeDialog();
  document.querySelectorAll(".nav-link").forEach((link) => {
    const active = link.dataset.view === view;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  routeStatus.textContent = labels[view];
  document.title = `${labels[view]} · Immortal Memory`;
});

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    router.navigate(link.dataset.view);
  });
});

router.start().catch((error) => {
  root.setAttribute("aria-busy", "false");
  const message = document.createElement("p");
  message.className = "state-panel error-state";
  message.textContent = error.message || "页面加载失败";
  root.replaceChildren(message);
});
