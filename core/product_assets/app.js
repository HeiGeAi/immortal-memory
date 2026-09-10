import { createRouter } from "./router.js";
import { closeDialog } from "./dialog.js";
import { mutate } from "./api.js";
import { renderHome } from "./views/home.js";
import { renderMemories } from "./views/memories.js";
import { renderSystem } from "./views/system.js";
import { renderSelf } from "./views/self.js";
import { renderJudgments } from "./views/judgments.js";
import { renderUse } from "./views/contexts.js";
import { renderTrust } from "./views/trust.js";

const root = document.getElementById("view");
const routeStatus = document.getElementById("route-status");
const globalHealth = document.getElementById("global-health");
const labels = { home: "首页", memories: "记忆", self: "我", judgments: "判断", use: "使用", trust: "信任", system: "系统" };
const updateHealth = (label, status = "unknown") => {
  globalHealth.textContent = label || "连续性未知";
  globalHealth.dataset.status = status || "unknown";
};

const router = createRouter({
  home: (context) => renderHome(root, { ...context, updateHealth }),
  memories: (context) => renderMemories(root, context),
  self: (context) => renderSelf(root, context),
  judgments: (context) => renderJudgments(root, { ...context, mutate }),
  use: (context) => renderUse(root, context),
  trust: (context) => renderTrust(root, context),
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
