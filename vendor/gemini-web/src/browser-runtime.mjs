import { createServer } from "node:net";
import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
import { access, chmod, mkdir, readFile, writeFile, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import { CdpClient } from "./cdp-client.mjs";
import { browserProxyArgs } from "./browser-proxy.mjs";
import { paths } from "./state-store.mjs";
import { withFileLock } from "./operation-lock.mjs";
import { canonicalConversationUrl } from "./task-pool.mjs";
import { pageAdapter } from "./provider-adapters.mjs";
import { inferProviderFromUrl, isConversationUrl, isHomeUrl, normalizeProvider } from "./providers.mjs";

const execFileAsync = promisify(execFile);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const fail = (code, message) => Object.assign(new Error(message), { code });
const candidates = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
];

function appBundleFor(executable) {
  const marker = ".app/";
  const index = executable.indexOf(marker);
  return index < 0 ? null : executable.slice(0, index + ".app".length);
}

async function findLaunchedBrowserPid(executable, args, timeoutMs = 10_000) {
  const profileArg = args.find(value => value.startsWith("--user-data-dir="));
  if (!profileArg) return null;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const { stdout } = await execFileAsync("/bin/ps", ["-axo", "pid=,command="], { timeout: 2000 });
      const rows = stdout.split(/\r?\n/).map(row => row.trim());
      for (const row of rows) {
        const match = row.match(/^(\d+)\s+(.+)$/);
        if (!match || !match[2].includes(profileArg) || !match[2].includes(executable)) continue;
        if (/\s--type=/.test(match[2])) continue;
        return Number(match[1]);
      }
    } catch {}
    await delay(100);
  }
  return null;
}

async function processIdentity(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  try {
    const { stdout } = await execFileAsync("/bin/ps", ["-p", String(pid), "-o", "stat=", "-o", "lstart="], { timeout: 2000 });
    const match = stdout.trim().match(/^(\S+)\s+(.+)$/);
    return match && !match[1].startsWith("Z") ? match[2] : null;
  } catch { return null; }
}

async function jsonRequest(port, path, method = "GET") {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, { method, signal: AbortSignal.timeout(2000) });
  if (!response.ok) throw new Error(`Local browser request failed (${response.status}).`);
  return response.json();
}

function validSocket(value, port, kind) {
  try {
    const url = new URL(value);
    return url.protocol === "ws:" && ["127.0.0.1", "localhost"].includes(url.hostname) &&
      Number(url.port) === port && url.pathname.startsWith(`/devtools/${kind}/`);
  } catch { return false; }
}

async function allocateLoopbackPort() {
  const server = createServer();
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    return server.address().port;
  } finally { if (server.listening) await new Promise(resolve => server.close(resolve)); }
}

export async function launchBrowserProcess(exe, args, onEndpoint, {
  platform = process.platform, spawnProcess = spawn, runOpen = execFileAsync,
  findPid = findLaunchedBrowserPid,
} = {}) {
  const headed = !args.some(arg => arg === "--headless" || arg.startsWith("--headless="));
  const bundle = platform === "darwin" && headed ? appBundleFor(exe) : null;
  if (bundle) {
    // Launch a dedicated process without requesting foreground activation.
    try {
      await runOpen("/usr/bin/open", ["-n", "-g", "-a", bundle, "--args", ...args], { timeout: 10_000 });
    } catch {
      throw fail("BROWSER_START_FAILED", "LaunchServices failed to start the dedicated background browser.");
    }
    const pid = await findPid(exe, args);
    if (pid == null) throw fail("BROWSER_START_FAILED", "The dedicated background browser process did not start.");
    return pid;
  }
  const child = spawnProcess(exe, args, { detached: true, stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  child.stderr?.on("data", chunk => {
    stderr = (stderr + String(chunk)).slice(-8192);
    const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
    if (match && onEndpoint) { onEndpoint(match[1]); onEndpoint = null; }
  });
  await new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", () => reject(fail("BROWSER_START_FAILED", "The dedicated Chrome process failed to launch.")));
  });
  child.stderr?.unref();
  child.unref();
  return child.pid;
}

/** Task mutations hold their slot lock; browser lifecycle holds the browser lock. */
export class PersistentBrowserRuntime {
  constructor({ root = paths.root, profile = paths.profile, request = jsonRequest,
    identity = processIdentity, proxy = browserProxyArgs, launch, connect, pause = delay, allocatePort = allocateLoopbackPort,
    platform = process.platform } = {}) {
    this.root = root;
    this.profile = profile;
    this.file = join(root, "browser-runtime.json");
    this.request = request;
    this.identity = identity;
    this.proxy = proxy;
    this.pause = pause;
    this.allocatePort = allocatePort;
    this.platform = platform;
    this.launch = launch ?? launchBrowserProcess;
    this.connect = connect ?? (async url => {
      const client = new CdpClient(url);
      await client.connect();
      return client;
    });
  }

  taskRuntime(slot) {
    if (![1, 2, 3].includes(slot)) throw fail("INVALID_INPUT", "Invalid task slot.");
    const scoped = Object.assign(Object.create(Object.getPrototypeOf(this)), this);
    scoped.slotId = slot;
    scoped.parentRuntime = this;
    scoped.file = join(this.root, `task-${slot}.json`);
    return scoped;
  }

  async read() {
    let source;
    try { source = await readFile(this.file, "utf8"); }
    catch (error) { if (error.code === "ENOENT") return null; throw error; }
    try {
      const state = JSON.parse(source);
      if (![1, 2, 3].includes(state.version) || state.profile !== this.profile || !Number.isInteger(state.pid) ||
          !state.processIdentity || !Number.isInteger(state.port) || state.port < 1 || state.port > 65535 ||
          !validSocket(state.browserSocket, state.port, "browser")) throw new Error();
      return state;
    } catch { throw fail("BROWSER_RUNTIME_CORRUPT", "Browser runtime metadata is invalid; refusing to control an unidentified browser."); }
  }

  async write(state) {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    await chmod(this.root, 0o700);
    const temporary = `${this.file}.${process.pid}.${crypto.randomUUID()}.tmp`;
    state.version = 3;
    await writeFile(temporary, JSON.stringify(state) + "\n", { mode: 0o600, flag: "wx" });
    await rename(temporary, this.file);
  }

  async activePort() {
    try {
      const [port, path] = (await readFile(join(this.profile, "DevToolsActivePort"), "utf8")).trim().split(/\r?\n/);
      const value = Number(port);
      if (Number.isInteger(value) && value > 0 && value <= 65535 && path?.startsWith("/devtools/browser/")) return { port: value, path };
    } catch {}
    return null;
  }

  async owned(state) {
    return state != null && await this.identity(state.pid) === state.processIdentity;
  }

  async verify(state) {
    if (!await this.owned(state)) throw fail("BROWSER_OWNERSHIP", "The recorded browser process identity no longer matches.");
    const version = await this.request(state.port, "/json/version");
    if (version.webSocketDebuggerUrl !== state.browserSocket) throw fail("BROWSER_OWNERSHIP", "The local debugging endpoint belongs to a different browser.");
    return version;
  }

  async findBrowser() {
    for (const executable of candidates) {
      try { await access(executable); return executable; } catch {}
    }
    return null;
  }

  async assertProfileAvailable() {
    if (await this.read()) throw fail("BROWSER_OWNERSHIP", "The resident browser must be stopped before opening a visible login window.");
    const active = await this.activePort();
    if (active) {
      let live = false;
      try { await this.request(active.port, "/json/version"); live = true; } catch {}
      if (live) throw fail("BROWSER_OWNERSHIP", "An unidentified browser is using the dedicated profile; refusing to attach a login window.");
    }
  }

  async start(signal, { mode = null } = {}) {
    if (this.slotId) {
      return withFileLock(join(this.root, "locks", "browser"), { signal }, async () => {
        await this.parentRuntime.recoverPending();
        const port = await this.parentRuntime.start(signal, { mode });
        const parent = await this.parentRuntime.read();
        const prior = await this.read();
        if (!prior || prior.browserSocket !== parent.browserSocket) {
          if (prior?.inFlight || prior?.quarantined) await this.recoverPending();
          const state = { ...parent, targetId: null };
          delete state.inFlight;
          delete state.quarantined;
          await this.write(state);
        }
        return port;
      });
    }
    if (signal?.aborted) throw fail("CANCELLED", "Browser request cancelled before startup.");
    const proxyArgs = this.proxy();
    let state = await this.read();
    if (state) {
      if (await this.owned(state)) {
        await this.verify(state);
        if (state.proxyFingerprint !== JSON.stringify(proxyArgs)) {
          throw fail("BROWSER_CONFIG_CHANGED", "Browser proxy configuration changed. Stop the resident browser explicitly before starting it with the new configuration.");
        }
        if (mode === "headed" && state.browserMode !== "headed") {
          // The bridge calls this upgrade while holding every task slot and the
          // browser lock. Unresolved pages still block the transition.
          const tasks = await Promise.all([1, 2, 3].map(slot => this.taskRuntime(slot).read()));
          if (state.inFlight || state.quarantined || tasks.some(task => task?.browserSocket === state.browserSocket && (task.inFlight || task.quarantined))) {
            throw fail("OUTCOME_UNKNOWN", "Recover existing browser requests before changing the browser mode.");
          }
          await this.shutdown();
          return this.start(signal, { mode });
        }
        if (state.version < 3) await this.write(state);
        return state.port;
      }
      if (state.inFlight) await this.recoverPending();
      // A dead/reused PID cannot authorize controlling whatever now uses its old port.
      await rm(this.file, { force: true });
      state = null;
    }
    const oldPort = await this.activePort();
    if (oldPort) {
      let live = false;
      try { await this.request(oldPort.port, "/json/version"); live = true; } catch {}
      if (live) throw fail("BROWSER_OWNERSHIP", "A live browser exists without matching ownership metadata. Refusing to attach to or close it.");
    }
    await mkdir(this.profile, { recursive: true, mode: 0o700 });
    await chmod(this.profile, 0o700);
    await rm(join(this.profile, "DevToolsActivePort"), { force: true });
    const executable = await this.findBrowser();
    if (!executable) throw fail("BROWSER_NOT_FOUND", "No compatible Chrome browser was found.");
    const browserMode = mode === "headed" ? "headed" : "headless";
    const deferWindow = browserMode === "headed" && this.platform === "darwin";
    const debugPort = browserMode === "headed" ? await this.allocatePort() : 0;
    const args = [`--user-data-dir=${this.profile}`, ...proxyArgs,
      "--remote-debugging-address=127.0.0.1", `--remote-debugging-port=${debugPort}`,
      "--no-first-run", "--no-default-browser-check", "--disable-quic", "--disable-background-networking",
      ...(browserMode === "headless" ? ["--headless=new"] : ["--start-minimized"]),
      ...(deferWindow ? ["--no-startup-window"] : []),
      "--window-size=1440,1000", ...(deferWindow ? [] : ["about:blank"])];
    let launchEndpoint = null;
    const pid = await this.launch(executable, args, endpoint => { launchEndpoint = endpoint; });
    const processId = await this.identity(pid);
    if (!processId) throw fail("BROWSER_START_FAILED", "The dedicated Chrome process did not start.");
    const deadline = Date.now() + 30_000;
    try {
      while (Date.now() < deadline) {
        if (signal?.aborted) throw fail("CANCELLED", "Browser startup cancelled.");
        let active = launchEndpoint && debugPort > 0 && validSocket(launchEndpoint, debugPort, "browser")
          ? { port: debugPort, path: new URL(launchEndpoint).pathname } : await this.activePort();
        // Chrome writes DevToolsActivePort for port=0. Headed LaunchServices
        // launches use a fixed port and have no stderr endpoint callback.
        if (!active && debugPort > 0) {
          try {
            const version = await this.request(debugPort, "/json/version");
            if (validSocket(version.webSocketDebuggerUrl, debugPort, "browser")) {
              active = { port: debugPort, path: new URL(version.webSocketDebuggerUrl).pathname };
            }
          } catch {}
        }
        if (active) {
          try {
            const version = await this.request(active.port, "/json/version");
            if (validSocket(version.webSocketDebuggerUrl, active.port, "browser") &&
                new URL(version.webSocketDebuggerUrl).pathname === active.path) {
              state = { version: 2, poolVersion: 1, profile: this.profile, pid, processIdentity: processId,
                port: active.port, browserSocket: version.webSocketDebuggerUrl, browserMode,
                background: browserMode === "headed",
                proxyFingerprint: JSON.stringify(proxyArgs), targetId: null, createdAt: new Date().toISOString() };
              await this.verify(state);
              await this.write(state);
              return state.port;
            }
          } catch (error) { if (error.code === "BROWSER_OWNERSHIP") throw error; }
        }
        if (await this.identity(pid) !== processId) throw fail("BROWSER_START_FAILED", "The dedicated Chrome process exited during startup.");
        await this.pause(100);
      }
      throw fail("BROWSER_TIMEOUT", "The dedicated browser did not become ready.");
    } catch (error) {
      if (await this.identity(pid) === processId) {
        try { process.kill(-pid, "SIGTERM"); } catch {}
      }
      throw error;
    }
  }

  async createBackgroundTarget(state, firstWindow) {
    const browser = await this.connect(state.browserSocket);
    let targetId;
    try {
      ({ targetId } = await browser.send("Target.createTarget", { url: "about:blank", background: true }));
      if (typeof targetId !== "string" || !targetId) throw fail("BROWSER_START_FAILED", "The background browser target was not created.");
      if (this.platform === "darwin" && firstWindow) {
        let window;
        for (let attempt = 0; attempt < 40; attempt++) {
          try {
            const current = await browser.send("Browser.getWindowForTarget", { targetId });
            if (Number.isInteger(current.windowId) && current.bounds?.windowState) { window = current; break; }
          } catch {}
          await this.pause(50);
        }
        if (!window) throw fail("BROWSER_START_FAILED", "The dedicated browser window is unavailable.");
        if (window.bounds?.windowState !== "minimized") {
          await browser.send("Browser.setWindowBounds", { windowId: window.windowId, bounds: { windowState: "minimized" } });
          let minimized = false;
          for (let attempt = 0; attempt < 40; attempt++) {
            try {
              const current = await browser.send("Browser.getWindowForTarget", { targetId });
              if (current.bounds?.windowState === "minimized") { minimized = true; break; }
            } catch {}
            await this.pause(50);
          }
          if (!minimized) throw fail("BROWSER_START_FAILED", "The dedicated browser window did not minimize.");
        }
      }
      let target;
      for (let attempt = 0; attempt < 40; attempt++) {
        const targets = await this.request(state.port, "/json/list");
        target = targets.find(item => item.type === "page" && item.id === targetId);
        if (target) break;
        await this.pause(50);
      }
      if (!target) throw fail("BROWSER_START_FAILED", "The background browser target is unavailable.");
      if (!validSocket(target.webSocketDebuggerUrl, state.port, "page")) throw fail("BROWSER_OWNERSHIP", "Unexpected page debugging endpoint.");
      return target;
    } catch (error) {
      if (targetId) await browser.send("Target.closeTarget", { targetId }).catch(() => {});
      throw error;
    } finally { browser.close(); }
  }

  async openTarget(port) {
    const state = await this.read();
    if (!state || state.port !== port) throw fail("BROWSER_OWNERSHIP", "No matching owned browser runtime exists.");
    await this.verify(state);
    const targets = await this.request(port, "/json/list");
    let target = targets.find(item => item.type === "page" && item.id === state.targetId);
    if (!target && state.targetId == null && !this.slotId) {
      const preserved = new Set((await this.recoveryHistory()).filter(item => item.browserSocket === state.browserSocket).map(item => item.targetId));
      target = targets.find(item => item.type === "page" && item.url === "about:blank" && !preserved.has(item.id));
    }
    let created = false;
    if (!target) {
      target = state.browserMode === "headed"
        ? await this.createBackgroundTarget(state, !targets.some(item => item.type === "page"))
        : await this.request(port, `/json/new?${encodeURIComponent("about:blank")}`, "PUT");
      created = true;
    }
    let client;
    try {
      if (!validSocket(target.webSocketDebuggerUrl, port, "page")) throw fail("BROWSER_OWNERSHIP", "Unexpected page debugging endpoint.");
      state.targetId = target.id;
      if (state.inFlight) state.inFlight.targetId = target.id;
      await this.write(state);
      client = await this.connect(target.webSocketDebuggerUrl);
      await client.send("Page.enable");
      await client.send("Runtime.enable");
      // Keep the dedicated headed AI Studio window in the background. CDP
      // input and page evaluation work without stealing the user's foreground
      // application; the login flow remains the explicit visible exception.
      if (state?.browserMode !== "headed") await client.send("Page.bringToFront");
      await client.send("Page.setWebLifecycleState", { state: "active" });
      // Keep the resident headless task page visible to the renderer. Otherwise
      // background throttling can stall hydration and page-side polling.
      await client.send("Emulation.setFocusEmulationEnabled", { enabled: true });
    } catch (error) {
      client?.close();
      if (created) await this.request(port, `/json/close/${encodeURIComponent(target.id)}`).catch(() => {});
      state.targetId = null;
      if (state.inFlight?.targetId === target.id) state.inFlight.targetId = null;
      await this.write(state).catch(() => {});
      throw error;
    }
    return { client, target };
  }

  async recoveryHistory() {
    try { return JSON.parse(await readFile(join(this.root, "recovered-requests.json"), "utf8")); }
    catch (error) { if (error.code === "ENOENT") return []; throw error; }
  }

  async saveRecovery(record) {
    return withFileLock(join(this.root, "locks", "recovery-state"), {}, async () => {
      const history = await this.recoveryHistory();
      const next = [...history.filter(item => item.requestId !== record.requestId), record];
      const file = join(this.root, "recovered-requests.json");
      const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify(next) + "\n", { mode: 0o600, flag: "wx" });
      await rename(temporary, file);
    });
  }

  async inspectRequest(state, pending) {
    const provider = normalizeProvider(pending.provider ?? "gemini");
    const record = { ...pending, provider, targetId: pending.targetId ?? state?.targetId ?? null,
      browserSocket: pending.browserSocket ?? state?.browserSocket,
      recoveredAt: new Date().toISOString(),
      outcome: ["COMPLETED", "FAILED", "UPLOAD_ABANDONED"].includes(pending.outcome) ? pending.outcome : "OUTCOME_UNKNOWN",
      reason: "browser-gone" };
    if (!state || record.browserSocket !== state.browserSocket || !await this.owned(state)) return record;
    let client;
    try {
      await this.verify(state);
      const targets = await this.request(state.port, "/json/list");
      const target = targets.find(item => item.type === "page" && item.id === record.targetId);
      if (!target) return { ...record, reason: "target-gone" };
      if (!validSocket(target.webSocketDebuggerUrl, state.port, "page")) throw fail("BROWSER_OWNERSHIP", "Unexpected recovery page endpoint.");
      client = await this.connect(target.webSocketDebuggerUrl);
      const adapter = pageAdapter(provider);
      let expectedModel = pending.model;
      if (expectedModel == null && provider === "aistudio") {
        try { expectedModel = new URL(pending.conversationUrl).searchParams.get("model") ?? undefined; } catch {}
      }
      const inspectionArgs = expectedModel == null ? [pending.before ?? {}] : [pending.before ?? {}, expectedModel];
      const page = await client.call(adapter.readGenerationState.toString(), inspectionArgs, 5000);
      if (!page || !page.snapshot) throw new Error("Recovery page state unavailable.");
      // Persist metadata only. Prompts and answers remain on the original page.
      if (isConversationUrl(provider, page.url ?? "")) record.conversationUrl = page.url;
      record.partialChars = page.snapshot.text?.length ?? 0;
      if (page.billing) record.billing = page.billing;
      record.reason = "page-inspected";
      if (page.stopVisible) record.outcome = "DETACHED_GENERATING";
      else if (pending.phase === "PRE_SUBMIT") record.outcome = "FAILED";
      else if (pending.phase === "UPLOADING") record.outcome = "UPLOAD_ABANDONED";
      else if (page.failure) { record.outcome = "FAILED"; record.failure = page.failure.kind; }
      else if (pending.before && page.isNew && (provider === "aistudio" ? page.complete === true :
        page.snapshot.completedCount > pending.before.completedCount)) record.outcome = "COMPLETED";
      else record.outcome = "SETTLED_UNKNOWN";
      return record;
    } catch (error) {
      if (error.code === "BROWSER_OWNERSHIP") throw error;
      return { ...record, reason: "page-unavailable" };
    } finally { client?.close(); }
  }

  // Caller owns this slot (or the legacy browser lock). Active tasks in other
  // slots keep running; their files and pages are never touched here.
  async recoverPending(requestId = null) {
    const state = await this.read();
    const pending = state?.inFlight ?? state?.quarantined;
    if (requestId && pending?.requestId !== requestId) {
      const prior = (await this.recoveryHistory()).find(item => item.requestId === requestId);
      if (!prior) throw fail("INVALID_INPUT", "No recovery record exists for this request_id.");
      const record = await this.inspectRequest(state, prior);
      await this.saveRecovery(record);
      return record;
    }
    if (!pending) return null;
    const record = await this.inspectRequest(state, pending);
    // Save evidence before releasing the task slot. Keep the old page intact;
    // uncertain or still-generating requests get their own preserved target.
    await this.saveRecovery(record);
    if (state.targetId === record.targetId) state.targetId = null;
    delete state.inFlight;
    delete state.quarantined;
    if (this.slotId && (record.outcome === "DETACHED_GENERATING" ||
        record.outcome === "OUTCOME_UNKNOWN" && record.reason === "page-unavailable")) {
      state.quarantined = record;
    }
    await this.write(state);
    return record;
  }

  async assertNoPending() {
    const state = await this.read();
    if (state?.inFlight) throw Object.assign(fail("OUTCOME_UNKNOWN", "Inspect the pending request with recover before continuing."),
      { phase: state.inFlight.phase, conversationUrl: state.inFlight.conversationUrl });
  }

  async assertConversationAvailable(destination, provider = null) {
    const resolvedProvider = normalizeProvider(provider ?? inferProviderFromUrl(destination) ?? "gemini");
    if (isHomeUrl(resolvedProvider, destination)) return;
    const record = (await this.recoveryHistory()).findLast(item => canonicalConversationUrl(item.conversationUrl) === canonicalConversationUrl(destination));
    if (record && ["OUTCOME_UNKNOWN", "DETACHED_GENERATING", "SETTLED_UNKNOWN"].includes(record.outcome)) {
      throw Object.assign(fail("OUTCOME_UNKNOWN", "This conversation has an unresolved turn. Inspect its request_id or start an independent conversation."),
        { phase: record.phase, conversationUrl: destination, requestId: record.requestId, provider: record.provider ?? resolvedProvider });
    }
  }

  async markRequest(request) {
    const state = await this.read();
    if (!state) throw fail("BROWSER_OWNERSHIP", "No owned runtime exists for this request.");
    await this.verify(state);
    const prior = state.inFlight?.requestId === request.requestId ? state.inFlight : null;
    state.inFlight = { ...prior, ...request, slotId: this.slotId ?? null, targetId: state.targetId, ownerPid: process.pid,
      startedAt: prior?.startedAt ?? new Date().toISOString() };
    await this.write(state);
  }

  async clearRequest(requestId) {
    const state = await this.read();
    if (state?.inFlight && state.inFlight.requestId === requestId) {
      delete state.inFlight;
      await this.write(state);
    }
  }

  async shutdown() {
    const state = await this.read();
    if (!state) return { stopped: false, reason: "not-running" };
    if (!await this.owned(state)) {
      await rm(this.file, { force: true });
      return { stopped: false, reason: "recorded-process-gone" };
    }
    await this.verify(state);
    const client = await this.connect(state.browserSocket);
    try { await client.send("Browser.close", {}, 5000); }
    finally { client.close(); }
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline && await this.owned(state)) await this.pause(100);
    if (await this.owned(state)) throw fail("BROWSER_STOP_TIMEOUT", "The owned browser has not exited; runtime metadata was retained.");
    await rm(this.file, { force: true });
    return { stopped: true, pid: state.pid };
  }

  async status() {
    const executable = await this.findBrowser();
    const state = await this.read();
    let connected = false;
    let error = null;
    if (state) {
      try { await this.verify(state); connected = true; } catch (e) { error = e.code ?? "BROWSER_DISCONNECTED"; }
    }
    const taskStates = this.slotId ? [] : await Promise.all([1, 2, 3].map(slot => this.taskRuntime(slot).read()));
    const withProvider = record => record == null ? null : { ...record, provider: record.provider ?? "gemini" };
    const recovery = await this.recoveryHistory();
    const tasks = taskStates.flatMap((task, index) => connected && task?.browserSocket === state?.browserSocket && (task?.inFlight || task?.quarantined)
      ? [{ slotId: index + 1, ...withProvider(task.inFlight ?? task.quarantined) }] : []);
    return { browserInstalled: executable != null, executable, connected,
      runtimeMode: state?.browserMode === "headed" ? "headed-resident" : "headless-resident", defaultModel: "4 Argon",
      pid: state?.pid ?? null, targetId: taskStates[0]?.browserSocket === state?.browserSocket ? taskStates[0]?.targetId ?? null : state?.targetId ?? null,
      targetIds: taskStates.filter(task => task?.browserSocket === state?.browserSocket).map(task => task?.targetId).filter(Boolean),
      pendingRequest: withProvider(state?.inFlight),
      lastRecovery: withProvider(recovery.at(-1)),
      recoverySupported: true, concurrencySupported: true, maxConcurrentRequests: 3,
      activeRequests: tasks, activeRequestCount: tasks.length,
      ownershipVerified: connected,
      configurationMatches: state == null || state.proxyFingerprint === JSON.stringify(this.proxy()), error };
  }
}
