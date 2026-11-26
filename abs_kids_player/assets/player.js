(() => {
  const page = document.querySelector("[data-listening-player]");
  if (!page) return;

  const field = name => page.querySelector("[data-player-" + name + "]");
  const csrf = document.querySelector('meta[name="chapter-csrf-token"]').content;
  const queue = page.querySelector("[data-listening-queue]");
  const cover = page.querySelector("[data-book-cover]");
  const seek = field("seek");
  const chapters = field("chapters");
  const timer = field("timer");
  const speed = field("speed");
  const volume = field("volume");
  const toggle = field("toggle");
  const tabs = [...page.querySelectorAll('[role="tab"]')];
  const queuePanel = document.getElementById("listening-queue");
  const bookmarkPanel = document.getElementById("player-bookmarks");
  const volumeSteps = [...page.querySelectorAll("[data-volume-step]")];
  const showBookmarks = createPlayerBookmarks(bookmarkPanel, request, duration, setControlAvailability);
  let activeTab = location.hash === "#listening-queue" ? tabs[1] : tabs[0];
  let state = {openingKey: field("opening").dataset.playerOpening};
  let deviceState = state;
  const commands = createPlayerCommands(postCommand);
  let queuedOpening = "";
  let refreshing = false;
  let queueBusy = false;
  let queueRequest = 0;
  let queueMarkup = null;
  let requestedChapters = "";
  let bookmarkVersion = 0;

  function duration(value) {
    const seconds = Math.floor(Math.max(0, value || 0));
    const minutes = Math.floor(seconds / 60);
    const clock = String(seconds % 60).padStart(2, "0");
    return minutes >= 60
      ? Math.floor(minutes / 60) + ":" + String(minutes % 60).padStart(2, "0") + ":" + clock
      : minutes + ":" + clock;
  }

  async function request(path, body) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: {"X-Requested-With": "fetch"},
      signal: AbortSignal.timeout?.(7000),
      ...(body && {method: "POST", body}),
    });
    if (response.status === 403) throw new Error("Reload this page and sign in again.");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Chapter could not complete the request.");
    return result;
  }

  function progressLabel(progress) {
    if (!progress) return "";
    return progress.position == null ? progress.label : progress.label + " · " + duration(progress.position);
  }

  function showTab(selected) {
    activeTab = selected;
    for (const tab of tabs) {
      const active = tab === selected;
      tab.tabIndex = active ? 0 : -1;
      tab.setAttribute("aria-selected", String(active));
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
    }
    updatePanels();
    if (selected === tabs[1]) refreshQueue().catch(() => {});
  }

  function updatePanels() {
    const listening = state.hasSession && !state.openingKey;
    queuePanel.hidden = Boolean(state.openingKey || (listening && activeTab !== tabs[1]));
    if (listening) {
      queuePanel.setAttribute("role", "tabpanel");
      queuePanel.setAttribute("aria-labelledby", "tab-queue");
    } else {
      queuePanel.removeAttribute("role");
      queuePanel.setAttribute("aria-labelledby", "listening-queue-heading");
    }
    showBookmarks(listening && state.canBookmark ? state.sessionKey : "", bookmarkVersion,
      listening && activeTab === tabs[0]);
  }

  function renderTimer() {
    const sleep = state.sleeping ? "Sleep timer ended" : state.sleepUntil
      ? "Sleep in " + duration(state.sleepUntil - Date.now() / 1000)
      : state.sleepChapterEnd != null ? "Sleep at end of chapter" : "";
    field("sleep-status").textContent = sleep;
    // An older status file can have a deadline without recording the original preset.
    const preset = state.sleepPreset ?? (state.sleepUntil ? null : state.sleepChapterEnd != null ? -1 : 0);
    timer.value = preset == null ? "" : String(preset);
  }

  function renderTime() {
    if (!state.hasSession) return;
    const age = state.isPlaying && state.updatedAt ? Math.max(0, Date.now() / 1000 - state.updatedAt) : 0;
    const projected = state.currentTime + age * (state.speed / 100);
    const position = state.duration > 0 ? Math.min(projected, state.duration) : projected;
    const current = state.chapters?.find(chapter => chapter.start <= position && position < chapter.end);
    timer.querySelector('[value="-1"]').disabled = !current;
    chapters.value = current ? String(Math.round(current.start * 1000)) : "";
    if (!seek.matches(":active")) seek.value = Math.floor(position);
    seek.setAttribute("aria-valuetext", duration(seek.value));
    field("elapsed").textContent = duration(position);
    field("remaining").textContent = state.duration ? "−" + duration(state.duration - position) : "";
    renderTimer();
  }

  function setControlAvailability() {
    for (const control of field("session").querySelectorAll("button, input, select")) {
      if (control.getAttribute("role") !== "tab") control.disabled = !state.sessionKey;
    }
    const action = name => page.querySelector('[data-player-action="' + name + '"]');
    action("undo").disabled ||= !state.canUndo;
    chapters.disabled ||= !state.chapters?.length;
    seek.disabled ||= !state.duration;
    action("bookmark").disabled ||= !state.canBookmark;
    for (const step of volumeSteps) {
      step.disabled ||= Number(step.dataset.volumeStep) < 0 ? Number(volume.value) <= 0 : Number(volume.value) >= 100;
    }
    field("finish").disabled ||= !state.duration;
    renderTime();
  }

  function render(device) {
    deviceState = device;
    const player = commands.preview(device);
    const changedBook = player.sessionKey !== state.sessionKey;
    const focusedPanel = document.activeElement.closest("[data-player-session], [data-player-opening], [data-player-idle]");
    state = player;
    if (changedBook) {
      requestedChapters = "";
      bookmarkVersion = 0;
      showTab(location.hash === "#listening-queue" ? tabs[1] : tabs[0]);
    } else {
      updatePanels();
    }
    const opening = Boolean(player.openingKey);
    document.dispatchEvent(new CustomEvent("chapter:session", {detail: player.hasSession || opening}));
    field("opening").hidden = !opening;
    field("session").hidden = !player.hasSession || opening;
    field("idle").hidden = player.hasSession || opening;
    const moveFocus = focusedPanel?.hidden;
    if (!player.hasSession || opening) {
      if (moveFocus) (opening ? field("opening").querySelector("button") : field("idle").querySelector("a")).focus();
      return;
    }
    field("title").textContent = player.title;
    field("playing").textContent = player.isPlaying ? "Now playing" : "Paused";
    field("author").textContent = player.author;
    if (player.coverUrl !== (cover.getAttribute("src") || "")) {
      if (player.coverUrl) cover.src = player.coverUrl;
      else cover.removeAttribute("src");
      cover.hidden = !player.coverUrl;
    }
    field("output").textContent = [player.source, player.output].filter(Boolean).join(" · ");
    field("progress-status").textContent = progressLabel(player.progress);
    seek.max = Math.floor(player.duration);
    field("skip-back").textContent = player.skipBack + "s";
    field("skip-forward").textContent = player.skipForward + "s";
    const label = player.isPlaying ? "Pause" : "Play";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    toggle.firstElementChild.classList.toggle("icon-pause", player.isPlaying);
    toggle.firstElementChild.classList.toggle("icon-play", !player.isPlaying);
    if (!volume.matches(":active")) showVolume(player.muted ? 0 : player.volumePercent);
    field("mute-label").textContent = player.muted ? "Unmute" : "Mute";
    field("mute").classList.toggle("is-muted", Boolean(player.muted));
    speed.value = player.speed;
    const chapterData = JSON.stringify(player.chapters || []);
    if (chapterData !== requestedChapters) {
      const options = (player.chapters || []).map(chapter =>
        new Option(chapter.title, String(Math.round(chapter.start * 1000))));
      const placeholder = new Option(options.length ? "Choose a chapter" : "No chapter markers", "");
      placeholder.disabled = true;
      chapters.replaceChildren(placeholder, ...options);
      requestedChapters = chapterData;
    }
    setControlAvailability();
    if (moveFocus) toggle.focus();
  }

  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const player = await request("/player/state");
      // A queued play can reach the browser before the OLED publishes its first status.
      if (queuedOpening && !player.openingKey && player.sessionKey !== queuedOpening) {
        player.openingKey = queuedOpening;
      }
      render(player);
    } catch {
      // Keep the current book and controls during a short connection interruption.
    } finally {
      refreshing = false;
    }
  }

  async function confirmCommand(id, rejectedMessage = "Chapter could not apply this command.", stillNeeded = () => true) {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      if (!stillNeeded()) return;
      const {outcome, observedAt} = await request("/player/command?id=" + encodeURIComponent(id));
      if (outcome === "applied" || outcome === "cancelled") return observedAt || 0;
      if (outcome === "rejected") throw new Error(rejectedMessage);
      if (outcome === "superseded") throw new Error("The active book or command changed. Try again.");
      await new Promise(resolve => window.setTimeout(resolve, 400));
    }
    throw new Error("Chapter has yet to confirm the command.");
  }

  function postCommand(action, key, value) {
    const body = new URLSearchParams({csrf_token: csrf, session_key: key, action});
    if (value !== undefined) body.set("value", value);
    return request("/player/control", body);
  }

  async function send(action, value) {
    const key = action === "cancel_open" ? state.openingKey : state.sessionKey;
    if (!key) return;
    const {command, submitted} = commands.start(action, value, key);
    render(deviceState);
    field("feedback").textContent = "";
    try {
      const result = await submitted;
      if (!result || !commands.current(command)) return;
      const message = action === "speed" ? "Chapter could not change the playback speed." : undefined;
      command.confirmedAt = await confirmCommand(result.id, message, () => commands.current(command));
      if (!commands.current(command)) return;
      if (action === "bookmark" && state.sessionKey === key) {
        bookmarkVersion += 1;
        updatePanels();
      }
      const messages = {bookmark: "Bookmark saved.", finish: "Marked finished on Chapter.", stop: "Playback stopped."};
      if (messages[action]) field("feedback").textContent = messages[action];
    } catch (error) {
      if (commands.current(command)) {
        commands.discard(command);
        render(deviceState);
        field("feedback").textContent = error.message;
      }
    } finally {
      if (commands.current(command)) refresh();
    }
  }

  function showVolume(value) {
    volume.value = value;
    field("volume-label").textContent = volume.value + "%";
    volume.style.setProperty("--volume-fill", volume.value + "%");
    volume.setAttribute("aria-valuetext", volume.value + " percent");
  }

  async function refreshQueue(force = false) {
    if (document.hidden || (!force && (queueBusy || !queue.getClientRects().length))) return;
    const revision = ++queueRequest;
    const result = await request("/player/queue/status");
    if (revision === queueRequest && (!queueBusy || force) && result.markup !== queueMarkup) {
      if (!force && queue.contains(document.activeElement)) return;
      queueMarkup = result.markup;
      queue.innerHTML = queueMarkup;
    }
  }

  page.addEventListener("click", event => {
    const button = event.target.closest("[data-player-action]");
    const action = button?.dataset.playerAction;
    if (action === "seek") send(action, button.value);
    else if (action) send(action);
  });
  function selectTab(tab) {
    if (tab !== tabs[1] && location.hash === "#listening-queue") {
      history.replaceState(history.state, "", location.pathname + location.search);
    }
    showTab(tab);
  }
  tabs.forEach(tab => tab.addEventListener("click", () => selectTab(tab)));
  page.querySelector('[role="tablist"]').addEventListener("keydown", event => {
    const index = tabs.indexOf(event.target);
    const next = {ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length,
      Home: 0, End: tabs.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectTab(tabs[next]);
    tabs[next].focus();
  });
  toggle.addEventListener("click", () => send(state.isPlaying ? "pause" : "play"));
  field("mute").addEventListener("click", () => send(state.muted ? "unmute" : "mute"));
  speed.addEventListener("change", () => send("speed", speed.value));
  chapters.addEventListener("change", () => send("seek", chapters.value));
  timer.addEventListener("change", () => send("sleep_timer", timer.value));
  field("finish").addEventListener("click", () => {
    if (window.confirm("Mark this book finished in Audiobookshelf?")) send("finish");
  });
  seek.addEventListener("input", () => seek.setAttribute("aria-valuetext", duration(seek.value)));
  seek.addEventListener("change", () => send("seek", Math.round(Number(seek.value) * 1000)));
  volume.addEventListener("input", () => showVolume(volume.value));
  volume.addEventListener("change", () => send("volume", volume.value));
  for (const step of volumeSteps) {
    step.addEventListener("click", () => {
      showVolume(Number(volume.value) + Number(step.dataset.volumeStep));
      send("volume", volume.value);
    });
  }

  queue.addEventListener("submit", async event => {
    if (!event.target.matches("[data-listening-queue-form]")) return;
    event.preventDefault();
    if (queueBusy) return;
    const form = event.target;
    const button = form.querySelector("button");
    const body = new URLSearchParams(new FormData(form));
    const action = body.get("action");
    queueBusy = true;
    button.disabled = true;
    field("feedback").textContent = action === "play" ? "Opening book…" : "Updating the listening queue…";
    try {
      const result = await request("/player/queue", body);
      if (action === "play" && result.id) {
        queuedOpening = result.id;
        render({...deviceState, openingKey: result.id});
      }
      if (result.id) await confirmCommand(result.id);
      await refreshQueue(true);
      field("feedback").textContent = "";

      const replacement = [...queue.querySelectorAll("form")].find(candidate =>
        candidate.elements.action.value === action &&
        candidate.elements.book_id?.value === body.get("book_id")
      );
      (replacement?.querySelector("button:not(:disabled)") || document.querySelector("#listening-queue-heading")).focus();
    } catch (error) {
      field("feedback").textContent = error.message;
    } finally {
      if (action === "play") {
        queuedOpening = "";
        refresh();
      }
      button.disabled = false;
      queueBusy = false;
    }
  });

  document.addEventListener("visibilitychange", () => {
    refresh();
    refreshQueue().catch(() => {});
  });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#listening-queue") showTab(tabs[1]);
    if (location.hash === "#player-tools") activeTab.focus({preventScroll: true});
  });
  window.setInterval(refresh, 2500);
  window.setInterval(renderTime, 1000);
  window.setInterval(() => refreshQueue().catch(() => {}), 5000);
  showVolume(volume.value);
  setControlAvailability();
  refresh();
})();
