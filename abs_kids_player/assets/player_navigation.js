(() => {
  const link = document.querySelector("[data-player-navigation]");
  if (!link) return;

  function show(available) {
    link.hidden = !available;
  }

  document.addEventListener("chapter:session", event => show(event.detail));
  if (document.querySelector("[data-listening-player], [data-now-playing]")) return;

  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch("/player/status", {cache: "no-store", signal: AbortSignal.timeout?.(4000)});
      if (response.ok) {
        const state = await response.json();
        show(state.hasSession || state.openingKey);
      }
    } catch {
      // Keep the last known state during a brief connection interruption.
    }
  }

  document.addEventListener("visibilitychange", refresh);
  window.setInterval(refresh, 6000);
})();
