const {spawn} = require("node:child_process");
const {randomUUID} = require("node:crypto");
const {once} = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {createInterface} = require("node:readline");

async function startAppliance(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "chapter-browser-"));
  const password = randomUUID();
  const root = path.resolve(__dirname, "../..");
  const child = spawn(process.env.PYTHON || "python3", ["-B", path.join(__dirname, "appliance_fixture.py"), directory], {
    cwd: root, env: {...process.env, HOME: directory, PYTHONPATH: root, CHAPTER_TEST_PASSWORD: password},
    stdio: ["ignore", "pipe", "pipe"],
  });
  let errors = "";
  child.stderr.on("data", chunk => { errors = (errors + chunk).slice(-4000); });
  const lines = createInterface({input: child.stdout});
  const exited = once(child, "exit");
  t.after(async () => {
    child.kill();
    await exited;
    lines.close();
    await fs.rm(directory, {recursive: true, force: true});
  });
  const url = await Promise.race([
    once(lines, "line").then(([line]) => line),
    exited.then(() => { throw Error("Fixture stopped before startup: " + errors); }),
  ]);
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) throw Error("Unexpected fixture address: " + url);
  return {url, password};
}
module.exports = {startAppliance};
