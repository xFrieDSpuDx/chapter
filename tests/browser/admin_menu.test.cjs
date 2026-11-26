const assert = require("node:assert/strict");
const {execFileSync} = require("node:child_process");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright");

test("Connections fills in Wi-Fi after the page opens and keeps the form being typed", async t => {
  const fixture = [
    "import json",
    "from abs_kids_player.config import AppConfig",
    "from abs_kids_player.connection_health import ConnectionHealth, ConnectionState",
    "from abs_kids_player.player_control import WebPlayerStatus",
    "from abs_kids_player.setup_views import render_setup_page, render_wifi_card",
    "from abs_kids_player.wifi import WifiStatus",
    "page = render_setup_page(config=AppConfig(), wifi=None, abs_status=ConnectionHealth(ConnectionState.NOT_CONFIGURED), player_status=WebPlayerStatus(), bt_status=(False, ''), csrf_token='fixture', active_tab='connections')",
    "print(json.dumps([page, render_wifi_card(WifiStatus(False), 'fixture')]))",
  ].join("\n");
  const [html, card] = JSON.parse(execFileSync("python3", ["-B", "-c", fixture], {
    cwd: path.resolve(__dirname, "../.."), encoding: "utf8",
  }));
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.route("https://chapter.test/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") return route.fulfill({contentType: "text/html", body: html});
    if (url.pathname === "/status/wifi") {
      const result = {label: "Not connected", connected: false, cardKey: "join"};
      if (url.searchParams.get("card") === "1") result.card = card;
      return route.fulfill({json: result});
    }
    return route.fulfill({json: {}});
  });
  await page.goto("https://chapter.test/?tab=connections");
  const ssid = page.getByLabel("Wi-Fi network name");
  await ssid.fill("Bedroom");
  await page.waitForResponse(response => response.url().endsWith("/status/wifi?card=1"));
  assert.equal(await ssid.inputValue(), "Bedroom");
  assert.equal(await ssid.evaluate(input => document.activeElement === input), true);
});

test("OLED screen-off settings fit narrow phones and submit overnight hours", async t => {
  const fixture = [
    "from abs_kids_player.config import AppConfig",
    "from abs_kids_player.setup_views import render_settings_page",
    "print(render_settings_page(AppConfig(), 'display', 'test-token'))",
  ].join("\n");
  const html = execFileSync("python3", ["-B", "-c", fixture], {
    cwd: path.resolve(__dirname, "../.."), encoding: "utf8",
  });
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 320, height: 640}, hasTouch: true});
  await page.route("https://chapter.test/**", route => route.fulfill({contentType: "text/html", body: html}));
  await page.goto("https://chapter.test/settings/display");
  const idle = page.getByLabel("Turn screen off after last dial use");
  const start = page.getByLabel("Screen off from");
  const end = page.getByLabel("Screen off until");
  assert.equal(await idle.inputValue(), "0");
  for (const width of [320, 390]) {
    await page.setViewportSize({width, height: 640});
    for (const field of [idle, start, end]) {
      const bounds = await field.boundingBox();
      assert.ok(bounds && bounds.width > 0 && bounds.x >= 0 && bounds.x + bounds.width <= width);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  }
  await idle.selectOption("1");
  await start.fill("22:00");
  await end.fill("06:30");
  const submitted = page.waitForRequest(request => request.method() === "POST");
  await page.getByRole("button", {name: "Save display settings"}).tap();
  const fields = new URLSearchParams((await submitted).postData());
  assert.deepEqual(["screen_off_minutes", "screen_off_start", "screen_off_end"].map(key => fields.get(key)),
    ["1", "22:00", "06:30"]);
});

test("the mobile menu follows visitor access and keeps appearance choices in sync across layouts", async t => {
  const fixture = [
    "import json",
    "from abs_kids_player.admin_layout import render_admin_page",
    "from abs_kids_player.theme_views import render_themes_page",
    "print(json.dumps({'/settings/themes': render_themes_page(), '/library': render_admin_page('Library', 'library', '<h1>Library</h1>', guest_sections=('library',))}))",
  ].join("\n");
  const pages = JSON.parse(execFileSync("python3", ["-B", "-c", fixture], {
    cwd: path.resolve(__dirname, "../.."), encoding: "utf8",
  }));
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport: {width: 390, height: 844}, hasTouch: true});
  await page.route("https://chapter.test/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pages[pathname]) return route.fulfill({contentType: "text/html", body: pages[pathname]});
    return route.fulfill({json: {}});
  });
  await page.goto("https://chapter.test/settings/themes");
  const menu = page.getByRole("button", {name: "Main menu"});
  const nav = page.getByRole("navigation", {name: "Player administration"});
  const palette = page.getByRole("combobox", {name: "Colour palette"});
  const mode = page.getByRole("combobox", {name: "Appearance", exact: true});
  assert.equal(await nav.isVisible(), false);
  await menu.tap();
  assert.deepEqual(await nav.getByRole("link").allTextContents(),
    ["Overview", "Library", "Downloads", "Settings", "Connections", "Podcasts", "System"]);
  assert.equal(await menu.getAttribute("aria-expanded"), "true");
  await mode.selectOption("dark");
  await palette.selectOption("fjord");
  assert.equal(await page.getByRole("radio", {name: "Fjord", exact: true}).isChecked(), true);
  await page.keyboard.press("Escape");
  assert.equal(await nav.isVisible(), false);
  assert.equal(await menu.evaluate(el => el === document.activeElement), true);
  await page.getByRole("radio", {name: "Juniper", exact: true}).check();
  await menu.tap();
  assert.equal(await palette.inputValue(), "juniper");
  await nav.getByRole("link", {name: "Library"}).click();
  await page.waitForURL("**/library");
  assert.equal(await nav.isVisible(), false);
  await menu.tap();
  assert.deepEqual(await nav.getByRole("link").allTextContents(), ["Library", "Log in"]);
  assert.equal(await palette.inputValue(), "juniper");
  assert.equal(await mode.inputValue(), "dark");
  await page.touchscreen.tap(3, 150);
  assert.equal(await nav.isVisible(), false);
  await menu.tap();
  await page.setViewportSize({width: 1280, height: 900});
  await menu.waitFor({state: "hidden"});
  await nav.waitFor();
  assert.equal(await page.locator(".admin-header [data-admin-theme]").isVisible(), true);
  await page.setViewportSize({width: 320, height: 568});
  await menu.waitFor();
  assert.equal(await nav.isVisible(), false);
  await menu.tap();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await palette.selectOption("midnight-brass");
  assert.equal(await page.locator("html").getAttribute("data-palette"), "midnight-brass");
});
