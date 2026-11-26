// Only the current request may populate the list after a book change or a new save.
function createPlayerBookmarks(panel, request, formatTime, updateAvailability) {
  const list = panel.querySelector("[data-player-bookmark-list]");
  const status = panel.querySelector("[data-player-bookmark-status]");
  const retry = panel.querySelector("[data-player-retry-bookmarks]");
  let current = null;

  function row(bookmark) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.playerAction = "seek";
    button.value = Math.round(bookmark.time * 1000);
    const name = document.createElement("span");
    name.textContent = bookmark.title || "Saved position";
    const position = document.createElement("span");
    position.className = "muted";
    position.textContent = formatTime(bookmark.time);
    button.append(name, position);
    const item = document.createElement("li");
    item.append(button);
    return item;
  }

  async function load(job) {
    status.textContent = "Loading bookmarks…";
    retry.hidden = true;
    try {
      for (let attempt = 0; attempt < 8 && current === job; attempt++) {
        const params = new URLSearchParams({session_key: job.key, version: job.version});
        const result = await request("/player/bookmarks?" + params);
        if (current !== job) return;
        if (!result.pending) {
          list.replaceChildren(...result.bookmarks.map(row));
          status.textContent = result.bookmarks.length ? "" : "No saved bookmarks for this book.";
          updateAvailability();
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 600));
      }
      if (current === job) throw new Error("Bookmarks are unavailable.");
    } catch (error) {
      if (current !== job) return;
      status.textContent = error.message;
      retry.hidden = false;
    }
  }

  retry.addEventListener("click", () => { if (current?.key) load(current); });
  return function show(key, version, visible) {
    if (current && current.key === key && current.version === version) return;
    current = null;
    list.replaceChildren();
    retry.hidden = true;
    status.textContent = key ? "" : "Bookmarks are unavailable for this book.";
    if (!key || visible) {
      current = {key, version};
      if (key) load(current);
    }
  };
}
