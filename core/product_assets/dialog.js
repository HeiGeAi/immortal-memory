let previousFocus = null;
let keyHandler = null;

function focusable(drawer) {
  return [...drawer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((node) => !node.disabled && !node.hidden);
}

export function closeDialog() {
  const drawer = document.getElementById("drawer");
  if (drawer.getAttribute("aria-hidden") === "true") return;
  drawer.setAttribute("aria-hidden", "true");
  drawer.replaceChildren();
  document.querySelector(".shell")?.removeAttribute("inert");
  if (keyHandler) document.removeEventListener("keydown", keyHandler);
  previousFocus?.focus();
  previousFocus = null;
  keyHandler = null;
}

export function openDialog(title, renderBody, trigger = document.activeElement) {
  const drawer = document.getElementById("drawer");
  previousFocus = trigger;
  const heading = document.createElement("h2");
  heading.id = "drawer-title";
  heading.textContent = title;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "icon-button";
  close.setAttribute("aria-label", "关闭详情");
  close.textContent = "关闭";
  close.addEventListener("click", closeDialog);
  const head = document.createElement("header");
  head.className = "drawer-head";
  head.append(heading, close);
  const body = document.createElement("div");
  body.className = "drawer-body";
  drawer.replaceChildren(head, body);
  drawer.setAttribute("aria-modal", "true");
  drawer.setAttribute("aria-labelledby", heading.id);
  drawer.setAttribute("aria-hidden", "false");
  document.querySelector(".shell")?.setAttribute("inert", "");
  renderBody(body);
  keyHandler = (event) => {
    if (event.key === "Escape") return closeDialog();
    if (event.key !== "Tab") return;
    const nodes = focusable(drawer);
    if (!nodes.length) return event.preventDefault();
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  document.addEventListener("keydown", keyHandler);
  (focusable(drawer)[0] || drawer).focus();
  return body;
}
