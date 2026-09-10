const KNOWN_VIEWS = new Set(["home", "memories", "self", "judgments", "use", "trust", "system"]);

export function currentRoute() {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("view") || "home";
  return { view: KNOWN_VIEWS.has(requested) ? requested : "home", params };
}

export function createRouter(renderers, onRoute) {
  let activeController = null;
  let generation = 0;

  async function render() {
    activeController?.abort();
    activeController = new AbortController();
    const route = currentRoute();
    const ownGeneration = ++generation;
    onRoute(route);
    const renderer = renderers[route.view];
    try {
      await renderer({
        route,
        signal: activeController.signal,
        isCurrent: () => ownGeneration === generation && !activeController.signal.aborted,
        navigate,
      });
    } catch (error) {
      if (error?.name !== "AbortError" && ownGeneration === generation) throw error;
    }
  }

  function navigate(view, changes = {}) {
    const params = new URLSearchParams();
    params.set("view", view);
    Object.entries(changes).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    });
    history.pushState({}, "", `/?${params.toString()}`);
    render();
  }

  window.addEventListener("popstate", render);
  return { start: render, navigate };
}
