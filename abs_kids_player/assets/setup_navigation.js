(() => {
  const dashboard = document.querySelector("[data-dashboard]");
  const panels = [...dashboard.querySelectorAll("[data-tab-panel]")];
  const navigation = [...document.querySelectorAll(".admin-nav [data-tab-target]")];
  const storageKey = "chapter-admin-tab";

  function activeTab(name) {
    return panels.some((panel) => panel.dataset.tabPanel === name) ? name : "overview";
  }

  function showTab(name, focusHeading = false) {
    navigation.forEach((link) => {
      if (link.dataset.tabTarget === name) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== name;
      if (!panel.hidden && focusHeading) {
        const anchor = document.getElementById(window.location.hash.slice(1));
        (anchor && panel.contains(anchor) ? anchor : panel.querySelector("h1")).focus();
      }
    });
    try { window.localStorage.setItem(storageKey, name); } catch {}
  }

  panels.forEach((panel) => { panel.querySelector("h1").tabIndex = -1; });
  let initialTab = panels.find((panel) => !panel.hidden)?.dataset.tabPanel;
  if (dashboard.dataset.restoreTab === "1") {
    try { initialTab = window.localStorage.getItem(storageKey); } catch {}
  }
  initialTab = activeTab(initialTab);
  showTab(initialTab);
  window.history.replaceState({ chapterTab: initialTab }, "");

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-tab-target]");
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (window.location.href !== link.href) {
      window.history.pushState({ chapterTab: link.dataset.tabTarget }, "", link.href);
    }
    showTab(activeTab(link.dataset.tabTarget), true);
  });
  window.addEventListener("popstate", (event) => {
    const tab = event.state?.chapterTab || new URLSearchParams(window.location.search).get("tab");
    showTab(activeTab(tab), true);
  });
})();
