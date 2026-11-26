(() => {
  const page = document.querySelector("[data-download-page]");
  if (!page) return;

  const queue = page.querySelector("[data-download-queue]");
  const space = document.querySelector("[data-download-space]");
  const connection = page.querySelector("[data-library-connection]");
  const activeForms = new Map();
  const lastButtonMarkup = new WeakMap();
  const pollInterval = queue ? 2000 : 3000;
  let revision = 0;
  let polling = false;
  let timer;
  let failures = 0;
  let spaceFollowups = 0;
  let pollController;
  let departing = false;

  async function request(url, options, controller = new AbortController()) {
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal, ...options });
      const isJson = response.headers.get("Content-Type")?.includes("application/json");
      const data = isJson ? await response.json() : null;
      return { ok: response.ok, status: response.status, data };
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function updateCard(card, state, csrf) {
    const label = card.querySelector("[data-download-label]");
    if (label.textContent !== state.label) label.textContent = state.label;
    label.hidden = !state.label;

    const progress = card.querySelector("[data-download-progress]");
    if (progress.innerHTML !== state.progress) progress.innerHTML = state.progress;
    progress.hidden = !state.progress;
    const listening = card.querySelector("[data-listening-progress]");
    if (listening) listening.hidden = !progress.hidden;

    const buttons = card.querySelector("[data-download-buttons]");
    if (buttons && lastButtonMarkup.get(buttons) !== state.buttons) {
      const focused = buttons.contains(document.activeElement) ? document.activeElement.value : null;
      buttons.innerHTML = state.buttons;
      const title = card.querySelector("[data-book-details]").dataset.bookTitle;
      for (const button of buttons.children) button.setAttribute("aria-label", button.title + ", " + title);
      lastButtonMarkup.set(buttons, state.buttons);
      if (focused) {
        const target = Array.from(buttons.children).find(button => button.value === focused);
        (target || buttons.firstElementChild)?.focus({ preventScroll: true });
      }
    }
    if (csrf) {
      for (const field of card.querySelectorAll('input[name="csrf_token"]')) field.value = csrf;
    }
  }

  function updateQueue(markup, csrf) {
    const list = queue.querySelector("[data-download-queue-list]");
    const template = document.createElement("template");
    template.innerHTML = markup;
    const incoming = Array.from(template.content.children);
    const previous = new Map(Array.from(list.children, card => [card.dataset.downloadId, card]));
    const next = incoming.map(card => {
      const existing = previous.get(card.dataset.downloadId);
      if (!existing) return card;
      previous.delete(card.dataset.downloadId);
      updateCard(existing, cardState(card), csrf);
      return existing;
    });
    for (const card of previous.values()) card.remove();
    next.forEach((card, index) => {
      if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
    });
    list.hidden = !next.length;
    queue.querySelector("[data-download-queue-empty]").hidden = !!next.length;
  }

  function queueFeedback(card, pending) {
    updateCard(card, pending);
    for (const button of card.querySelectorAll("[data-download-buttons] button")) button.setAttribute("aria-disabled", "true");
    card.querySelector("[data-download-form]").setAttribute("aria-busy", "true");
  }

  function cardsFor(id) {
    return Array.from(page.querySelectorAll("[data-download-id]")).filter(card => card.dataset.downloadId === id);
  }

  function cardState(card) {
    return {
      label: card.querySelector("[data-download-label]").textContent,
      progress: card.querySelector("[data-download-progress]").innerHTML,
      buttons: card.querySelector("[data-download-buttons]")?.innerHTML || "",
    };
  }

  function schedulePoll(delay = pollInterval) {
    window.clearTimeout(timer);
    if (!departing && !document.hidden) timer = window.setTimeout(refreshStatus, delay);
  }

  function stopPolling() {
    window.clearTimeout(timer);
    pollController?.abort();
  }

  async function refreshStatus() {
    if (departing || document.hidden) return;
    if (polling || activeForms.size) {
      schedulePoll();
      return;
    }
    const results = page.querySelector("[data-library-results]");
    const cards = Array.from(results.querySelectorAll("[data-download-id]"));
    const params = new URLSearchParams(window.location.search);
    params.set("section", page.dataset.librarySection);
    params.delete("id");
    if (queue) params.set("queue_revision", queue.dataset.queueRevision);
    const pending = results.dataset.cataloguePending === "1";
    if (pending) params.set("catalogue_pending", "1");
    for (const card of cards) params.append("id", card.dataset.downloadId);
    if (!queue && !cards.length && !pending && !["downloaded", "hide_downloaded"].some(key => params.get(key) === "1")) {
      return;
    }

    const currentRevision = revision;
    polling = true;
    pollController = new AbortController();
    let delay = pollInterval;
    try {
      const { ok, data: status } = await request("/library/download/status?" + params, {
        headers: {"X-Requested-With": "fetch"},
      }, pollController);
      if (revision !== currentRevision || departing || document.hidden) return;
      if (!ok || !status) throw new Error();
      failures = 0;
      connection.hidden = true;
      if (queue && typeof status.queue === "string") {
        updateQueue(status.queue, status.csrf);
        queue.dataset.queueRevision = status.queueRevision || "";
      }
      for (const card of page.querySelectorAll("[data-download-id]")) {
        const state = status.cards[card.dataset.downloadId];
        if (state) updateCard(card, state, status.csrf);
      }
      if (status.player) page.dispatchEvent(new CustomEvent("chapter:player", {detail: status.player}));
      if (space && typeof status.space === "string" && space.textContent !== status.space) {
        space.textContent = status.space;
      }
      if (queue && status.queueHeading) {
        const heading = queue.querySelector("[data-download-queue-heading]");
        if (heading.textContent !== status.queueHeading) heading.textContent = status.queueHeading;
      }
      if (!status.spaceUpdating) spaceFollowups = 0;
      else if (spaceFollowups < 5) {
        spaceFollowups += 1;
        delay = 400;
      }
      const inventoryChanged = status.inventory && results.dataset.downloadInventory !== status.inventory;
      if (inventoryChanged || (pending && status.cataloguePending === false)) {
        page.dispatchEvent(new Event("chapter:refresh-library"));
      }
    } catch {
      if (departing || document.hidden) return;
      failures = Math.min(failures + 1, 4);
      delay = Math.min(30000, pollInterval * 2 ** failures);
      if (connection.hidden) connection.textContent = "Live updates paused. Reconnecting to Chapter…";
      connection.hidden = false;
    } finally {
      polling = false;
      pollController = null;
      schedulePoll(delay);
    }
  }

  function showError(card, text) {
    const message = card.querySelector("[data-download-error]");
    message.textContent = text;
    message.hidden = !text;
  }

  page.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-download-form]") || !event.submitter) return;
    event.preventDefault();
    const card = form.closest("[data-download-id]");
    const id = card.dataset.downloadId;
    if (activeForms.has(id)) return;

    const data = new URLSearchParams(new FormData(form));
    const action = event.submitter.value;
    data.set("action", action);
    const previous = cardState(card);
    const pending = {
      ...previous,
      label: action === "download" ? "Adding to queue…" : action === "remove" ? "Removing…" : "Cancelling…",
      progress: action === "download"
        ? '<span class="download-waiting" role="progressbar" aria-label="Adding to queue"></span>'
        : previous.progress,
    };
    activeForms.set(id, pending);
    const announce = page.querySelector("[data-download-announcement]");
    const title = card.querySelector("[data-book-details]").dataset.bookTitle;
    announce.textContent = title + ". " + pending.label;
    revision += 1;
    for (const matching of cardsFor(id)) {
      showError(matching, "");
      queueFeedback(matching, pending);
    }
    let state = previous;
    let error = "Could not confirm the change. Check the download status and try again.";
    try {
      const response = await request(form.getAttribute("action"), {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
        body: data,
      });
      if (response.status === 403) {
        error = "This page has expired. Wait for the status to update, then try again.";
      } else if (response.ok && response.data?.card) {
        state = response.data.card;
        error = "";
      } else {
        error = response.data?.error || error;
      }
    } catch {
      // Refresh once in case the player received the request before the connection dropped.
    } finally {
      activeForms.delete(id);
      announce.textContent = title + ". " + (error || state.label ||
        (action === "remove" ? "Download removed." : action === "cancel" ? "Download cancelled." : "Download saved."));
      for (const matching of cardsFor(id)) {
        updateCard(matching, state);
        for (const button of matching.querySelectorAll("[data-download-buttons] button")) button.removeAttribute("aria-disabled");
        matching.querySelector("[data-download-form]").removeAttribute("aria-busy");
        showError(matching, error);
      }
      if (action === "remove") spaceFollowups = 0;
      if (!activeForms.size) schedulePoll(0);
    }
  });

  page.addEventListener("chapter:library-results", () => {
    revision += 1;
    for (const [id, pending] of activeForms) {
      for (const card of cardsFor(id)) queueFeedback(card, pending);
    }
    schedulePoll();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else schedulePoll(0);
  });
  document.addEventListener("click", event => {
    const link = event.target.closest(".admin-nav a, a.app-brand");
    if (link && !link.target && !event.defaultPrevented && event.button === 0 &&
        !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      departing = true;
      stopPolling();
    }
  });
  window.addEventListener("pagehide", () => {
    departing = true;
    stopPolling();
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      departing = false;
      schedulePoll(0);
    }
  });
  schedulePoll(queue ? 0 : pollInterval);
})();
