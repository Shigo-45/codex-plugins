import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { PersistentBrowserRuntime } from "../src/browser-runtime.mjs";

if (process.platform !== "darwin") throw new Error("The background window smoke test requires macOS.");

const swift = `
import AppKit
import Foundation
import Darwin
let observer = NSWorkspace.shared.notificationCenter.addObserver(
  forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
) { notification in
  if let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
    print("ACT \\(app.processIdentifier)")
    fflush(stdout)
  }
}
print("READY")
fflush(stdout)
RunLoop.current.run(until: Date(timeIntervalSinceNow: 90))
withExtendedLifetime(observer) {}
`;

const root = await mkdtemp(join(tmpdir(), "gemini-background-smoke-"));
const runtime = new PersistentBrowserRuntime({ root, profile: join(root, "profile"), proxy: () => ["--no-proxy-server"] });
const watcher = spawn("/usr/bin/swift", ["-e", swift], { stdio: ["ignore", "pipe", "pipe"] });
let events = "", watcherError = "", watcherFailure, first, second, browser, state, startupAttempted = false;
watcher.stdout.on("data", chunk => { events += String(chunk); });
watcher.stderr.on("data", chunk => { watcherError += String(chunk); });
watcher.on("error", error => { watcherFailure = error; });

async function waitForWatcher() {
  for (let attempt = 0; attempt < 150; attempt++) {
    if (events.split("\n").includes("READY")) return;
    if (watcherFailure) throw watcherFailure;
    if (watcher.exitCode != null) throw new Error(`Focus observer exited: ${watcherError.trim()}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("Focus observer did not start.");
}

async function windowState(targetId) {
  return (await browser.send("Browser.getWindowForTarget", { targetId })).bounds?.windowState;
}

try {
  await waitForWatcher();
  startupAttempted = true;
  const port = await runtime.start(undefined, { mode: "headed" });
  state = await runtime.read();
  const firstTask = runtime.taskRuntime(1);
  await firstTask.start(undefined, { mode: "headed" });
  first = await firstTask.openTarget(port);
  browser = await runtime.connect(state.browserSocket);
  assert.equal(await windowState(first.target.id), "minimized");

  const secondTask = runtime.taskRuntime(2);
  await secondTask.start(undefined, { mode: "headed" });
  second = await secondTask.openTarget(port);
  assert.notEqual(first.target.id, second.target.id);
  assert.equal(await windowState(second.target.id), "minimized");

  const html = '<textarea id="prompt"></textarea><button id="send">Send</button><div id="output"></div>' +
    '<script>document.querySelector("#send").onclick=()=>document.querySelector("#output").textContent=document.querySelector("#prompt").value</script>';
  await second.client.send("Page.navigate", { url: `data:text/html,${encodeURIComponent(html)}` });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await second.client.evaluate('Boolean(document.querySelector("#prompt"))').catch(() => false)) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  await second.client.evaluate('document.querySelector("#prompt").focus()');
  await second.client.send("Input.insertText", { text: "background smoke" });
  const point = await second.client.evaluate('(() => { const r = document.querySelector("#send").getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()');
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await second.client.send("Input.dispatchMouseEvent", { type, ...point, button: "left", clickCount: 1 });
  }
  assert.equal(await second.client.evaluate('document.querySelector("#output").textContent'), "background smoke");
  assert.equal(await windowState(second.target.id), "minimized");
  await new Promise(resolve => setTimeout(resolve, 250));
  assert.equal(events.split("\n").includes(`ACT ${state.pid}`), false);
} finally {
  first?.client.close();
  second?.client.close();
  browser?.close();
  let stopped = !startupAttempted;
  try {
    if (state) { await runtime.shutdown(); stopped = true; }
  } catch (error) {
    console.error(`Temporary browser cleanup failed: ${error.message}`);
    process.exitCode = 1;
  }
  await new Promise(resolve => setTimeout(resolve, 250));
  if (state && events.split("\n").includes(`ACT ${state.pid}`)) {
    console.error("The temporary browser became the foreground application.");
    process.exitCode = 1;
  }
  watcher.kill();
  if (stopped) await rm(root, { recursive: true, force: true });
  else console.error(`Temporary browser profile retained at ${root}`);
}

if (process.exitCode !== 1) {
  console.log(JSON.stringify({ passed: true, targets: 2, windowState: "minimized", foregroundActivations: 0, inputAndClick: true }));
}
