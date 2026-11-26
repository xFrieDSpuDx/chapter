// Run with Playwright and Chromium installed: node --test tests/browser/player.test.cjs
const assert = require("node:assert/strict");
const {execFileSync} = require("node:child_process");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright");

function playerPage() {
  const fixture = [
    "from unittest.mock import patch",
    "from abs_kids_player.config import AppConfig",
    "from abs_kids_player.player_control import WebPlayerStatus",
    "from abs_kids_player.player_views import render_player_page",
    "book = WebPlayerStatus(title='Test book', session_key='test-session')",
    "with patch('abs_kids_player.admin_layout.load_web_player_status', return_value=book):",
    "    print(render_player_page(AppConfig(), book, '', 'test-token'))",
  ].join("\n");
  return execFileSync("python3", ["-B", "-c", fixture], {
    cwd: path.resolve(__dirname, "../.."), encoding: "utf8",
  });
}

test("phone player applies sleep and chapter selections directly and reflects device changes", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 320, height: 800}, hasTouch: true});
  const markup = playerPage();
  const commands = [];
  const state = {
    hasSession: true, sessionKey: "test-session", title: "Test book", currentTime: 1, duration: 120,
    speed: 100, volumePercent: 30, skipBack: 15, skipForward: 30, sleepPreset: 0,
    chapters: [{title: "First", start: 0, end: 60}, {title: "Second", start: 60, end: 120}],
  };
  await page.route("https://chapter.test/**", async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/player") return route.fulfill({contentType: "text/html", body: markup});
    if (pathname === "/player/state") return route.fulfill({json: state});
    if (pathname === "/player/command") return route.fulfill({json: {outcome: "applied"}});
    if (pathname === "/player/control") {
      const command = Object.fromEntries(new URLSearchParams(route.request().postData()));
      commands.push(command);
      if (command.action === "sleep_timer") {
        state.sleepPreset = Number(command.value);
        state.sleepUntil = state.sleepPreset > 0 ? Date.now() / 1000 + state.sleepPreset * 60 : null;
        state.sleepChapterEnd = state.sleepPreset === -1 ? 60 : null;
      }
      if (command.action === "seek") state.currentTime = Number(command.value) / 1000;
      return route.fulfill({status: 202, json: {id: String(commands.length)}});
    }
    return route.fulfill({status: 204});
  });
  await page.goto("https://chapter.test/player");
  const timer = page.getByRole("combobox", {name: "Sleep timer"});
  for (const [name, value] of [["15 min", "15"], ["30 min", "30"], ["End of chapter", "-1"], ["Off", "0"]]) {
    await timer.selectOption({label: name});
    await page.waitForFunction(value => document.querySelector("[data-player-timer]").value === value &&
      !document.querySelector("[data-player-session]").hasAttribute("aria-busy"), value);
    assert.deepEqual(commands.at(-1), {
      action: "sleep_timer", value, session_key: "test-session", csrf_token: "test-token",
    });
  }
  assert.equal(commands.length, 4);
  state.sleepPreset = 30;
  state.sleepUntil = Date.now() / 1000 + 90;
  await page.reload();
  await page.waitForFunction(() => document.querySelector("[data-player-timer]").value === "30");
  const chapter = page.getByRole("combobox", {name: "Chapter", exact: true});
  await chapter.selectOption({label: "Second"});
  await page.waitForFunction(() => !document.querySelector("[data-player-session]").hasAttribute("aria-busy"));
  assert.equal(commands.at(-1).action, "seek");
  assert.equal(commands.at(-1).value, "60000");
  state.currentTime = 12;
  await page.locator("[data-player-elapsed]").filter({hasText: "0:12"}).waitFor();
  assert.equal(await chapter.inputValue(), "0");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
});

test("phone accepts volume taps, speed changes and pause while receipts are outstanding", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 375, height: 800}, hasTouch: true});
  const markup = playerPage();
  const state = {hasSession: true, sessionKey: "first", title: "Book", duration: 900, currentTime: 60,
    isPlaying: true, volumePercent: 30, speed: 100, updatedAt: 100, chapters: []};
  const posts = [];
  let acknowledgeVolume = false;
  let rejectSpeed = false;
  await page.route("https://chapter.test/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/player") return route.fulfill({contentType: "text/html", body: markup});
    if (url.pathname === "/player/state") {
      return route.fulfill({json: state});
    }
    if (url.pathname === "/player/control") {
      const {action, value} = Object.fromEntries(new URLSearchParams(route.request().postData()));
      posts.push([action, value]);
      return route.fulfill({status: 202, json: {id: action + "-" + posts.length}});
    }
    if (url.pathname === "/player/command") {
      const id = url.searchParams.get("id");
      const outcome = id.startsWith("volume") && acknowledgeVolume ? "applied" :
        id.startsWith("speed") && rejectSpeed ? "rejected" : "queued";
      return route.fulfill({json: {outcome, observedAt: 110}});
    }
    return route.fulfill({json: {}});
  });
  await page.goto("https://chapter.test/player");
  const speed = page.getByRole("combobox", {name: "Playback speed"});
  const up = page.getByRole("button", {name: "Increase volume by one percent"});
  const down = page.getByRole("button", {name: "Decrease volume by one percent"});
  const label = page.locator("[data-player-volume-label]");
  for (const [label, value] of [["2×", "200"], ["1.25×", "125"], ["1.75×", "175"]]) {
    await Promise.all([
      page.waitForResponse(res => res.url().endsWith("/player/control") &&
        new URLSearchParams(res.request().postData()).get("value") === value),
      speed.selectOption({label}),
    ]);
  }
  await up.tap();
  await up.tap();
  await up.tap();
  assert.equal(await label.textContent(), "33%");
  await down.tap();
  assert.equal(await label.textContent(), "32%");
  await Promise.all([
    page.waitForRequest(req => req.url().includes("/player/command?id=pause-")),
    page.getByRole("button", {name: "Pause", exact: true}).tap(),
  ]);
  assert.deepEqual(posts.slice(0, 3), [["speed", "200"], ["speed", "125"], ["speed", "175"]]);
  assert.deepEqual(posts.slice(-2), [["volume", "32"], ["pause", undefined]]);
  await page.getByRole("tab", {name: "Session"}).tap();
  assert.equal(await page.getByRole("button", {name: "Stop playback"}).isEnabled(), true);
  assert.equal(await speed.inputValue(), "175");
  await page.waitForResponse(res => res.url().endsWith("/player/state"));
  assert.equal(await label.textContent(), "32%");
  acknowledgeVolume = true;
  rejectSpeed = true;
  await page.getByText("Chapter could not change the playback speed.", {exact: true}).waitFor();
  assert.equal(await speed.inputValue(), "100");
  assert.equal(await label.textContent(), "32%");
  // A newer device status takes precedence, including an adjustment from a physical dial.
  Object.assign(state, {volumePercent: 23, updatedAt: 115});
  await label.filter({hasText: "23%"}).waitFor();
});

test("Player cancels a knob selection and stops a paused book using its own session ID", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 375, height: 800}});
  const markup = playerPage();
  const commands = [];
  let state = {hasSession: false, openingKey: "a".repeat(32)};
  await page.route("https://chapter.test/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/player") return route.fulfill({contentType: "text/html", body: markup});
    if (path === "/player/state") return route.fulfill({json: state});
    if (path === "/player/control") {
      commands.push(Object.fromEntries(new URLSearchParams(route.request().postData())));
      state = {hasSession: false};
      return route.fulfill({status: 202, json: {id: "ack"}});
    }
    if (path === "/player/command") return route.fulfill({json: {outcome: "applied"}});
    return route.fulfill({status: 204});
  });
  await page.goto("https://chapter.test/player");
  await page.getByRole("button", {name: "Cancel opening"}).click();
  await page.locator("[data-player-idle]").waitFor();
  state = {hasSession: true, sessionKey: "b".repeat(32), isPlaying: false, title: "Paused book",
           currentTime: 30, duration: 300, speed: 100, chapters: []};
  await page.reload();
  await page.getByRole("heading", {name: "Paused book"}).waitFor();
  await page.getByRole("tab", {name: "Session"}).click();
  await page.getByRole("button", {name: "Stop playback"}).click();
  await page.locator("[data-player-idle]").waitFor();
  assert.deepEqual(commands, [
    {action: "cancel_open", session_key: "a".repeat(32), csrf_token: "test-token"},
    {action: "stop", session_key: "b".repeat(32), csrf_token: "test-token"},
  ]);
});

test("mute and unmute keep the volume slider in place in every listening tab", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const markup = playerPage();
  const page = await browser.newPage({hasTouch: true, viewport: {width: 320, height: 840}});
  const state = {hasSession: true, sessionKey: "session", title: "A book", currentTime: 15,
    duration: 600, isPlaying: true, speed: 100, volumePercent: 100, muted: false, canBookmark: true, chapters: []};
  const actions = [];
  await page.route("https://chapter.test/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/player") return route.fulfill({contentType: "text/html", body: markup});
    if (url.pathname === "/player/state") return route.fulfill({json: state});
    if (url.pathname === "/player/bookmarks") return route.fulfill({json: {bookmarks: [{title: "The pier", time: 7}]}});
    if (url.pathname === "/player/queue/status") return route.fulfill({json: {markup: "<p>Next book</p>"}});
    if (url.pathname === "/player/command") return route.fulfill({json: {outcome: "applied"}});
    if (url.pathname === "/player/control") {
      const command = Object.fromEntries(new URLSearchParams(route.request().postData()));
      actions.push(command);
      if (["mute", "unmute"].includes(command.action)) {
        state.muted = command.action === "mute";
        state.isPlaying = !state.muted;
      } else if (command.action === "volume") state.volumePercent = Number(command.value);
      return route.fulfill({status: 202, json: {id: "ack"}});
    }
    return route.fulfill({status: 204});
  });
  await page.goto("https://chapter.test/player#listening-queue");
  await page.getByRole("tab", {name: "Queue", selected: true}).waitFor();
  await page.getByText("Next book", {exact: true}).waitFor();
  const volume = page.getByRole("slider", {name: "Volume", exact: true});
  // Document coordinates exclude scrolling performed by the browser to reach a control.
  const bounds = () => volume.evaluate(el => {
    const b = el.getBoundingClientRect();
    return {left: b.left + scrollX, width: b.width, height: b.height, top: b.top};
  });
  for (const width of [320, 768, 1280]) {
    await page.setViewportSize({width, height: 950});
    const spacing = await page.locator(".player-volume").evaluate(strip => {
      const band = strip.getBoundingClientRect();
      const pane = document.querySelector("main").getBoundingClientRect();
      const choices = document.querySelector(".player-choices").getBoundingClientRect();
      return [band.left - pane.left, pane.right - band.right, choices.left - band.left, band.right - choices.right];
    });
    assert.ok(spacing.slice(0, 2).every(gap => Math.abs(gap) < 1), "Volume background should reach both edges");
    assert.ok(spacing.slice(2).every(gap => gap >= 16), "The other controls should keep their margins");
    for (const name of ["Bookmarks", "Queue", "Session"]) {
      await page.getByRole("tab", {name, exact: true}).click();
      await volume.scrollIntoViewIfNeeded();
      const before = await bounds();
      assert.ok(before.height >= 48 && before.width >= 64);
      for (const [label, level] of [["Mute", "0%"], ["Unmute", "100%"]]) {
        await page.getByRole("button", {name: label, exact: true}).click();
        await page.locator("[data-player-volume-label]").getByText(level, {exact: true}).waitFor();
        assert.deepEqual(await bounds(), before, width + "px " + name + " " + label);
      }
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  }
  await page.getByRole("button", {name: "Decrease volume by one percent"}).click();
  await page.getByRole("button", {name: "Increase volume by one percent"}).click();
  assert.deepEqual(actions.slice(-2).map(c => [c.action, c.value]), [["volume", "99"], ["volume", "100"]]);
  await page.getByRole("tab", {name: "Session"}).press("Home");
  const marks = page.getByRole("tabpanel", {name: "Bookmarks"});
  await marks.getByRole("button", {name: /The pier/}).click();
  assert.deepEqual(actions.at(-1), {action: "seek", value: "7000", session_key: "session", csrf_token: "test-token"});
});

test("a second cover click cancels the exact play request before its receipt returns", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage();
  const root = path.resolve(__dirname, "../..");
  const fixture = [
    "from abs_kids_player.config import AppConfig",
    "from abs_kids_player.download_views import render_library_row",
    "from abs_kids_player.models import Book",
    "from abs_kids_player.setup_views import render_csrf_input",
    "config = AppConfig(server_url='https://books.test', token='token', library_id='lib')",
    "print(render_library_row(config, Book('one', 'First book', '', 600, ''), render_csrf_input('csrf'), download_status=('available', 0, 0)))",
  ].join("\n");
  const tile = execFileSync("python3", ["-B", "-c", fixture], {cwd: root, encoding: "utf8"});
  let release;
  const receipt = new Promise(resolve => { release = resolve; });
  const commands = [];
  await page.route("https://chapter.test/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/library") {
      return route.fulfill({contentType: "text/html", body: '<ul data-download-page>' + tile + '</ul>'});
    }
    if (url.pathname === "/player") {
      await receipt;
      return route.fulfill({status: 202, json: {id: "a".repeat(32)}});
    }
    if (url.pathname === "/player/control") {
      commands.push(Object.fromEntries(new URLSearchParams(route.request().postData())));
      return route.fulfill({status: 202, json: {id: "cancel"}});
    }
    if (url.pathname === "/player/command") {
      const outcome = url.searchParams.get("id") === "cancel" ? "applied" : "cancelled";
      return route.fulfill({json: {outcome}});
    }
    return route.fulfill({status: 204});
  });
  await page.goto("https://chapter.test/library");
  await page.addScriptTag({path: path.join(root, "abs_kids_player/assets/play_on_chapter.js")});
  await page.getByRole("button", {name: "Play on Chapter"}).click();
  await page.getByRole("button", {name: "Cancel opening on Chapter"}).click();
  assert.deepEqual(commands, []);
  release();
  await page.getByRole("button", {name: "Play on Chapter"}).waitFor();
  assert.deepEqual(commands, [{action: "cancel_open", session_key: "a".repeat(32), csrf_token: "csrf"}]);
});

test("System identifies a permanent local install and hides the online update button", async t => {
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage();
  const fixture = [
    "from abs_kids_player.setup_views import SETUP_SCRIPT, render_system_card",
    "print('<meta name=\"chapter-csrf-token\" content=\"test\">' + render_system_card() + '<script>' + SETUP_SCRIPT + '</script>')",
  ].join("\n");
  const markup = execFileSync("python3", ["-B", "-c", fixture], {
    cwd: path.resolve(__dirname, "../.."), encoding: "utf8",
  });
  await page.route("https://chapter.test/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/system") return route.fulfill({contentType: "text/html", body: markup});
    if (pathname === "/system/update/status") {
      return route.fulfill({json: {phase: "local", updateAvailable: false, message: "Installed from a local package."}});
    }
    return route.fulfill({status: 204});
  });
  await page.goto("https://chapter.test/system");
  await page.getByText("Local package installed", {exact: true}).waitFor();
  assert.equal(await page.locator("[data-system-update-trigger]").isHidden(), true);
});
