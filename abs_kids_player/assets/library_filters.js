(() => {
  const page = document.querySelector("[data-download-page]");
  if (!page) return;

  const form = page.querySelector(".library-filters");
  const field = name => form.elements.namedItem(name);
  const feedback = page.querySelector("[data-library-feedback]");
  let timer, loading;
  form.dataset.live = "";

  function showError(message) {
    feedback.textContent = message;
    feedback.hidden = !message;
  }

  function restoreFilters(params, results) {
    field("view").value = results.dataset.view;
    field("group_view").value = results.dataset.view;
    field("group").value = results.dataset.group;
    field("q").value = params.get("q") || "";
    for (const name of ["downloaded", "hide_downloaded", "hide_finished"]) {
      field(name).checked = params.get(name) === "1";
    }
    if (field("downloaded").checked) field("hide_downloaded").checked = false;
  }

  async function loadResults(params, historyMethod, {restore = false, focus = false} = {}) {
    loading?.abort();
    const controller = new AbortController();
    loading = controller;
    showError("");
    const url = "/library?" + params;
    try {
      const response = await fetch(url, {
        cache: "no-store",
        signal: controller.signal,
        headers: {"X-Chapter-Fragment": "results", "X-Requested-With": "fetch"},
      });
      if (!response.ok) throw new Error();
      const template = document.createElement("template");
      template.innerHTML = await response.text();
      if (loading !== controller) return;
      const results = template.content.querySelector("[data-library-results]");
      if (!results) throw new Error();
      const previous = page.querySelector("[data-library-results]");
      const focusInResults = previous.contains(document.activeElement);
      previous.replaceWith(results);
      document.querySelector("[data-library-total]").textContent = results.dataset.total;
      if (restore) restoreFilters(params, results);
      else {
        field("group").value = results.dataset.group;
        field("group_view").value = results.dataset.view;
      }
      const pageChanged = params.has("page") && params.get("page") !== results.dataset.page;
      if (pageChanged) params.set("page", results.dataset.page);
      if (historyMethod || pageChanged) history[historyMethod || "replaceState"](null, "", "/library?" + params);
      if (focusInResults) results.querySelector("#browse-books").focus({preventScroll: !focus});
      page.dispatchEvent(new Event("chapter:library-results"));
    } catch (error) {
      if (error.name === "AbortError") return;
      showError("Could not update the library. Try again or reload the page.");
    } finally {
      if (loading === controller) loading = null;
    }
  }

  function applyFilters(historyMethod) {
    window.clearTimeout(timer);
    timer = null;
    const params = new URLSearchParams(new FormData(form));
    if (!params.get("group") || params.get("group_view") !== params.get("view")) {
      params.delete("group");
      params.delete("group_view");
    }
    loadResults(params, historyMethod);
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    applyFilters("pushState");
  });
  form.addEventListener("change", event => {
    const name = event.target.name;
    if (name === "q") return; // The input event handles typing and the clear button.
    if (name === "downloaded" && field(name).checked) field("hide_downloaded").checked = false;
    if (name === "hide_downloaded" && field(name).checked) field("downloaded").checked = false;
    applyFilters("pushState");
  });
  field("q").addEventListener("input", () => {
    loading?.abort();
    window.clearTimeout(timer);
    timer = window.setTimeout(() => applyFilters("replaceState"), 280);
  });
  page.addEventListener("click", event => {
    const link = event.target.closest("a[data-library-link]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    window.clearTimeout(timer);
    timer = null;
    loadResults(new URL(link.href).searchParams, "pushState", {restore: true, focus: true});
  });
  window.addEventListener("popstate", () => {
    window.clearTimeout(timer);
    timer = null;
    loadResults(new URLSearchParams(location.search), null, {restore: true});
  });
  page.addEventListener("chapter:refresh-library", () => {
    if (!loading && !timer) loadResults(new URLSearchParams(location.search), null);
  });

  const dialog = page.querySelector("[data-book-dialog]");
  const title = dialog.querySelector("#book-dialog-title");
  const content = dialog.querySelector("[data-book-dialog-content]");
  let detailsRequest;
  dialog.addEventListener("close", () => detailsRequest?.abort());
  dialog.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-listening-queue-form]")) return;
    event.preventDefault();
    const button = form.querySelector("button");
    const feedback = form.parentElement.querySelector("[data-book-queue-feedback]");
    if (button.disabled) return;
    button.disabled = true;
    feedback.textContent = "Adding to the listening queue…";
    try {
      const response = await fetch(form.getAttribute("action"), {
        method: "POST",
        headers: {"X-Requested-With": "fetch"},
        signal: AbortSignal.timeout?.(7000),
        body: new URLSearchParams(new FormData(form)),
      });
      if (response.status === 403) throw new Error("Reload this page and sign in again.");
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not save the listening queue.");
      button.textContent = "Queued";
      feedback.textContent = "Added to the listening queue.";
    } catch (error) {
      button.disabled = false;
      feedback.textContent = error.message;
    }
  });
  page.addEventListener("click", async event => {
    const link = event.target.closest("[data-book-details]");
    if (!link || !dialog.showModal || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    const controller = new AbortController();
    detailsRequest?.abort();
    detailsRequest = controller;
    title.textContent = link.dataset.bookTitle || "Book details";
    content.innerHTML = '<p role="status">Loading book details…</p>';
    dialog.showModal();
    content.scrollTop = 0;
    const deadline = Date.now() + 12000;
    let showingDetails = false;
    try {
      while (!controller.signal.aborted) {
        const response = await fetch(link.href, {
          signal: controller.signal,
          headers: {"X-Chapter-Fragment": "details", "X-Requested-With": "fetch"},
        });
        if (!response.ok) throw new Error();
        const template = document.createElement("template");
        template.innerHTML = await response.text();
        if (controller.signal.aborted) return;
        const panel = template.content.querySelector("[data-book-title]");
        if (!panel) throw new Error();
        const pending = panel.dataset.detailsPending === "1";
        if (!pending || !showingDetails) {
          title.textContent = panel.dataset.bookTitle;
          content.replaceChildren(panel);
          showingDetails = true;
        }
        if (!pending) return;
        if (Date.now() >= deadline) {
          const synopsis = content.querySelector(".book-synopsis");
          if (synopsis) synopsis.textContent = "Synopsis is temporarily unavailable.";
          return;
        }
        await new Promise(resolve => window.setTimeout(resolve, 750));
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        content.innerHTML = '<p role="status">Could not load this book.</p>';
      }
    }
  });
})();
