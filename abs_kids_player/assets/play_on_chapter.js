(() => {
  const library = document.querySelector("[data-download-page]");
  if (!library) return;

  const pending = new Map();
  const wait = delay => new Promise(resolve => window.setTimeout(resolve, delay));

  let lastPlayer = {};

  function nameButton(button, label) {
    const title = button.closest("[data-download-id]").querySelector("[data-book-details]").dataset.bookTitle;
    button.title = label;
    button.setAttribute("aria-label", label + ", " + title);
  }

  function updateButtons(player) {
    lastPlayer = player;
    for (const form of library.querySelectorAll("[data-play-on-chapter]")) {
      const button = form.querySelector("button");
      const itemId = form.elements.namedItem("book_id").value;
      if (pending.has(itemId)) continue;
      const playing = player.isPlaying && player.itemId === itemId;
      const label = playing ? "Pause on Chapter" : "Play on Chapter";
      nameButton(button, label);
      button.firstElementChild.className = "cover-icon " + (playing ? "icon-pause" : "icon-play");
      form.elements.namedItem("action").value = playing ? "pause_book" : "play_book";
    }
  }

  library.addEventListener("chapter:player", event => updateButtons(event.detail));

  function showFeedback(itemId, message, operation) {
    const tile = library.querySelector(`[data-download-id="${CSS.escape(itemId)}"]:has([data-play-on-chapter])`);
    if (!tile) return;
    const button = tile.querySelector("[data-play-on-chapter] button");
    const busy = Boolean(operation);
    button.disabled = busy && !operation.canCancel;
    if (operation?.canCancel) {
      nameButton(button, "Cancel opening on Chapter");
      button.firstElementChild.className = "cover-icon icon-cancel";
    }
    if (busy) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
    tile.querySelector("[data-play-feedback]").textContent = message;
  }

  library.addEventListener("chapter:library-results", () => {
    for (const [itemId, operation] of pending) showFeedback(itemId, operation.message, operation);
  });

  async function readCommand(id) {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const response = await fetch("/player/command?id=" + encodeURIComponent(id), {
        cache: "no-store",
        signal: AbortSignal.timeout?.(8000),
        headers: {"X-Requested-With": "fetch"},
      });
      if (!response.ok) break;
      const result = await response.json();
      if (result.outcome !== "queued") return result.outcome;
      await wait(500);
    }
    return "unconfirmed";
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", body, signal: AbortSignal.timeout?.(8000),
      headers: {"X-Requested-With": "fetch"},
    });
    if (response.status === 403) throw new Error("Reload this page and try again.");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Chapter could not apply this control.");
    return result.id;
  }

  library.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-play-on-chapter]")) return;
    event.preventDefault();
    const fields = new URLSearchParams(new FormData(form));
    const itemId = fields.get("book_id");
    const previous = pending.get(itemId);
    if (previous && !previous.canCancel) return;
    const pausing = fields.get("action") === "pause_book";
    const operation = {
      canCancel: !previous && !pausing,
      message: previous ? "Cancelling…" : pausing ? "Pausing…" : "Opening book…",
    };
    pending.set(itemId, operation);
    showFeedback(itemId, operation.message, operation);
    let message = "";
    try {
      // A second click can arrive even before the first POST has returned its request ID.
      operation.receipt = previous
        ? previous.receipt.then(id => post("/player/control", new URLSearchParams({
            csrf_token: fields.get("csrf_token"), action: "cancel_open", session_key: id,
          })))
        : post(form.getAttribute("action"), fields);
      const outcome = await readCommand(await operation.receipt);
      if (pending.get(itemId) !== operation) return;
      if (outcome === "applied" && !previous) {
        lastPlayer = {itemId, isPlaying: !pausing};
        document.dispatchEvent(new CustomEvent("chapter:session", {detail: true}));
      } else if (!["applied", "cancelled", "superseded"].includes(outcome)) {
        message = outcome === "rejected" ? "Chapter could not apply this control." : "Playback unconfirmed.";
      }
    } catch (error) {
      message = error.message || "Could not confirm playback on Chapter.";
    } finally {
      if (pending.get(itemId) === operation) {
        pending.delete(itemId);
        showFeedback(itemId, message);
        updateButtons(lastPlayer);
      }
    }
  });
})();
