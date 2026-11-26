const assert = require("node:assert/strict");
const test = require("node:test");
const {chromium} = require("playwright");
const {startAppliance} = require("./appliance_fixture.cjs");

async function openChapter(t, width) {
  const {url, password} = await startAppliance(t);
  const browser = await chromium.launch({executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
  t.after(() => browser.close());
  const page = await browser.newPage({baseURL: url, viewport: {width, height: 760}});
  return {page, password};
}

async function signIn(page, password) {
  await page.goto("/owner");
  await page.getByLabel("Username", {exact: true}).fill("reader");
  await page.getByLabel("Owner password").fill(password);
  await page.getByRole("button", {name: "Log in"}).press("Enter");
  await page.waitForURL("**/?tab=overview");
}

async function checkAccessibility(page) {
  await page.addScriptTag({path: require.resolve("axe-core/axe.min.js")});
  const issues = await page.evaluate(async () => {
    const report = await axe.run(document, {runOnly: {
      type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"],
    }});
    return report.violations.map(rule => [rule.id, rule.nodes.map(node => node.target)]);
  });
  assert.deepEqual(issues, [], page.url());
}

async function assertFocused(locator) {
  const focus = await locator.evaluate(element => element === document.activeElement
    ? "expected" : document.activeElement.outerHTML.slice(0, 180));
  assert.equal(focus, "expected");
}

test("a keyboard listener can browse, open details, play and stop without losing their place", async t => {
  const {page, password} = await openChapter(t, 320);
  await page.goto("/library");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await assertFocused(page.getByRole("main"));
  assert.equal(await page.getByRole("button", {name: /Play on Chapter/}).count(), 0);
  const details = page.getByRole("link", {name: "Book details, The River", exact: true});
  await details.press("Enter");
  await page.getByText("A journey downstream.").waitFor();
  await checkAccessibility(page);
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({state: "hidden"});
  await assertFocused(details);

  await signIn(page, password);
  await checkAccessibility(page);
  await page.goto("/library");
  await page.getByLabel("Browse by", {exact: true}).selectOption("authors");
  await page.getByRole("link", {name: /Alex Green/}).press("Enter");
  const author = page.getByRole("heading", {name: "Alex Green"});
  await author.waitFor();
  await assertFocused(author);
  const play = page.getByRole("button", {name: "Play on Chapter, The River", exact: true});
  // White artwork gives the lowest contrast for a white icon on a translucent dark button.
  const iconContrast = await play.evaluate(button => {
    const pixel = document.createElement("canvas").getContext("2d");
    function paint(colour) {
      pixel.fillStyle = colour;
      pixel.fillRect(0, 0, 1, 1);
      const rgb = pixel.getImageData(0, 0, 1, 1).data.slice(0, 3);
      return [...rgb].reduce((sum, byte, index) => {
        const s = byte / 255;
        return sum + [0.2126, 0.7152, 0.0722][index] * (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4);
      }, 0);
    }
    paint("white");
    const background = paint(getComputedStyle(button).backgroundColor);
    const icon = paint(getComputedStyle(button.firstElementChild).color);
    return (icon + 0.05) / (background + 0.05);
  });
  assert.ok(iconContrast >= 3, "Cover play icon contrast on white artwork: " + iconContrast);
  await play.press("Enter");
  await page.getByRole("button", {name: "Pause on Chapter, The River", exact: true}).waitFor();
  await page.getByRole("button", {name: "Main menu"}).press("Enter");
  await page.getByRole("navigation").getByRole("link", {name: "Player", exact: true}).press("Enter");
  const volume = page.getByRole("slider", {name: "Volume", exact: true});
  await volume.waitFor();
  await checkAccessibility(page);
  const before = Number(await volume.inputValue());
  await volume.press("ArrowRight");
  await assertFocused(volume);
  assert.equal(await volume.getAttribute("aria-valuetext"), (before + 1) + " percent");
  await page.getByRole("button", {name: "Pause", exact: true}).press("Space");
  const resume = page.getByRole("button", {name: "Play", exact: true});
  await resume.waitFor();
  await assertFocused(resume);
  await resume.press("Space");
  await page.getByRole("button", {name: "Pause", exact: true}).waitFor();
  await page.getByRole("tab", {name: "Bookmarks"}).press("End");
  await assertFocused(page.getByRole("tab", {name: "Session"}));
  await page.keyboard.press("Tab");
  await assertFocused(page.getByRole("button", {name: "Stop playback"}));
  await page.keyboard.press("Enter");
  const library = page.getByRole("link", {name: "Choose a book in the Library"});
  await library.waitFor();
  await assertFocused(library);
});

test("the download action keeps the book name and keyboard focus through a status poll", async t => {
  const {page, password} = await openChapter(t, 1100);
  await signIn(page, password);
  await page.goto("/library");
  const search = page.getByRole("searchbox", {name: "Search"});
  await search.fill("river");
  await page.getByText("1 book matches", {exact: true}).waitFor();
  await assertFocused(search);
  const announcement = page.locator("[data-download-announcement]");
  await page.getByRole("button", {name: "Download, The River", exact: true}).press("Enter");
  const cancel = page.getByRole("button", {name: "Cancel download, The River", exact: true});
  await cancel.waitFor();
  assert.match(await announcement.textContent(), /The River.*Queued/i);
  const polled = page.waitForResponse(response => response.url().includes("/library/download/status?"));
  await polled;
  await assertFocused(cancel);
  await cancel.press("Enter");
  await page.waitForFunction(() => /The River.*cancelled/i.test(document.querySelector("[data-download-announcement]").textContent));
});

test("the signed-in Overview sends its CSRF token when changing volume", async t => {
  const {page, password} = await openChapter(t, 1100);
  await signIn(page, password);
  await page.goto("/library");
  await page.getByRole("button", {name: "Play on Chapter, The River", exact: true}).press("Enter");
  await page.getByRole("button", {name: "Pause on Chapter, The River", exact: true}).waitFor();
  await page.goto("/?tab=overview");

  const volume = page.getByRole("slider", {name: "Volume", exact: true});
  const before = Number(await volume.inputValue());
  const token = await page.locator('meta[name="chapter-csrf-token"]').getAttribute("content");
  assert.ok(token);
  const submitted = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/player" && response.request().method() === "POST");
  await volume.press("ArrowRight");
  const response = await submitted;
  const fields = new URLSearchParams(response.request().postData());
  assert.equal(fields.get("csrf_token"), token);
  assert.equal(fields.get("action"), "volume");
  assert.equal(fields.get("volume"), String(before + 1));
  assert.equal(response.status(), 202);
  await page.getByText("Applied.", {exact: true}).waitFor();
  await page.reload();
  assert.equal(await volume.inputValue(), String(before + 1));
});
