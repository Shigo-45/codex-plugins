import { ensureReasoningEffort } from "./reasoning-effort.mjs";
import { spawn } from "node:child_process";
import { AsyncLocalStorage } from "node:async_hooks";
import { WebTaskPool } from "./task-pool.mjs";

import { browserProxyArgs } from "./browser-proxy.mjs";
import { PersistentBrowserRuntime } from "./browser-runtime.mjs";
import { validateAttachments } from "./gemini-upload.mjs";
import { pageAdapter } from "./provider-adapters.mjs";
import {
  canonicalModel,
  conversationDestination,
  DEFAULT_PROVIDER,
  isConversationUrl,
  modelId,
  modelLabel,
  normalizeProvider,
  providerConfig,
  providerStatuses,
} from "./providers.mjs";
import {
  findLegacyConversation,
  getConversation,
  paths,
  saveConversation,
} from "./state-store.mjs";
import { buildGeminiPrompt, canonicalizeYoutubeUrl } from "./youtube.mjs";

const GEMINI_HOME = providerConfig("gemini").home;
const AISTUDIO_HOME = providerConfig("aistudio").home;
const ANSWER_TIMEOUT_MS = 3 * 60_000;
const NO_RESPONSE_TIMEOUT_MS = 90_000;
const STALLED_RESPONSE_TIMEOUT_MS = 45_000;
const LOGIN_TIMEOUT_MS = 10 * 60_000;
const MAX_PROMPT_CHARS = 16_000;

export const BRIDGE_PHASES = {
  UPLOADING: "UPLOADING",
  GENERATING: "GENERATING",
  PRE_SUBMIT: "PRE_SUBMIT",
  SUBMITTED: "SUBMITTED",
};

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class GeminiBridgeError extends Error {
  constructor(
    code,
    message,
    {
      billing = null,
      conversationId = null,
      conversationUrl = null,
      model = null,
      partialChars = 0,
      phase = BRIDGE_PHASES.PRE_SUBMIT,
      provider = DEFAULT_PROVIDER,
      retrySafe = false,
      quota = null,
      upload = null,
    } = {},
  ) {
    super(message);
    this.name = "GeminiBridgeError";
    this.code = code;
    this.billing = billing;
    this.conversationId = conversationId;
    this.conversationUrl = conversationUrl;
    this.model = model;
    this.partialChars = partialChars;
    this.phase = phase;
    try { this.provider = normalizeProvider(provider); } catch { this.provider = DEFAULT_PROVIDER; }
    this.retrySafe = retrySafe;
    this.retryable = retrySafe;
    this.quota = quota;
    this.upload = upload;
  }
}

function errorOptions(context, overrides = {}) {
  return {
    billing: context.billing ?? null,
    conversationId: context.conversationId ?? null,
    conversationUrl: context.conversationUrl ?? null,
    model: context.model ?? null,
    phase: context.phase ?? BRIDGE_PHASES.PRE_SUBMIT,
    provider: context.provider ?? DEFAULT_PROVIDER,
    upload: context.upload ?? null,
    ...overrides,
  };
}

function normalizeBridgeError(error, context = {}) {
  if (error instanceof GeminiBridgeError) {
    if (error.conversationId == null) error.conversationId = context.conversationId ?? null;
    if (error.conversationUrl == null) error.conversationUrl = context.conversationUrl ?? null;
    if (error.billing == null) error.billing = context.billing ?? null;
    if (error.model == null) error.model = context.model ?? null;
    if (context.provider != null) error.provider = normalizeProvider(context.provider);
    return error;
  }
  const message = error instanceof Error ? error.message : String(error);
  const phase = context.phase ?? BRIDGE_PHASES.PRE_SUBMIT;
  const provider = normalizeProvider(context.provider ?? error?.provider ?? DEFAULT_PROVIDER);
  const label = providerConfig(provider).label;
  if (/取消|cancelled|aborted/i.test(message) || error?.code === "CANCELLED") {
    return new GeminiBridgeError("CANCELLED", `${label} analysis was cancelled.`, errorOptions(context));
  }
  if (error?.code === "LOCK_TIMEOUT") {
    return new GeminiBridgeError(
      "BRIDGE_BUSY",
      "Timed out waiting for another Gemini Web Bridge operation to finish.",
      errorOptions(context),
    );
  }
  if (["AUTHORIZATION_REQUIRED", "CONVERSATION_NOT_FOUND", "INVALID_INPUT",
    "BROWSER_OWNERSHIP", "BROWSER_CONFIG_CHANGED", "BROWSER_RUNTIME_CORRUPT",
    "BROWSER_START_FAILED", "BROWSER_STOP_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_MISMATCH",
    "PROVIDER_MISMATCH", "BILLING_UNVERIFIED", "PAID_KEY_ACTIVE", "API_KEY_REQUIRED",
    "PERMISSION_DENIED", "ACCESS_DENIED", "MODEL_ERROR",
    "RATE_LIMITED", "WEB_QUOTA_EXHAUSTED", "UPLOAD_FAILED", "UPLOAD_UNSUPPORTED", "OUTCOME_UNKNOWN"].includes(error?.code)) {
    return new GeminiBridgeError(error.code, message, errorOptions(context, {
      billing: error.billing ?? context.billing ?? null,
      phase: error.phase ?? context.phase,
      conversationUrl: error.conversationUrl ?? context.conversationUrl,
      quota: error.quota ?? null,
    }));
  }
  if (/连接已关闭|调试连接尚未建立|fetch failed|ECONN|WebSocket|socket/i.test(message)) {
    const submitted = phase !== BRIDGE_PHASES.PRE_SUBMIT;
    return new GeminiBridgeError(
      submitted ? "OUTCOME_UNKNOWN" : "BROWSER_DISCONNECTED",
      submitted
        ? "The browser connection closed after submission may have started; the outcome is unknown."
        : "The browser connection closed before submission.",
      errorOptions(context, { retrySafe: !submitted }),
    );
  }
  if (/超时|timed out/i.test(message)) {
    const submitted = phase !== BRIDGE_PHASES.PRE_SUBMIT;
    return new GeminiBridgeError(
      submitted ? "OUTCOME_UNKNOWN" : "BROWSER_TIMEOUT",
      submitted ? "A browser call timed out after submission may have started; the outcome is unknown." : message,
      errorOptions(context, { retrySafe: !submitted }),
    );
  }
  if (/没有开始生成|did not start generating/i.test(message) && phase !== BRIDGE_PHASES.PRE_SUBMIT) {
    return new GeminiBridgeError(
      "OUTCOME_UNKNOWN",
      `${label} accepted the page interaction but generation did not become observable; the outcome is unknown.`,
      errorOptions(context),
    );
  }
  if (/找不到 Gemini 输入框|找不到可用的 Gemini 发送按钮/i.test(message)) {
    return new GeminiBridgeError(
      "UI_CHANGED",
      `${label} may have changed its page structure; the input or send control was not found.`,
      errorOptions(context),
    );
  }
  return new GeminiBridgeError("UNEXPECTED", message, errorOptions(context));
}

export function shouldRetryBridgeError(error, attempt, aborted = false) {
  return attempt === 0 && error?.retrySafe === true && !aborted;
}

function isSpecificConversationUrl(value, provider = DEFAULT_PROVIDER) {
  try { return isConversationUrl(provider, value); } catch { return false; }
}

function cleanPrompt(value) {
  const prompt = String(value).trim();
  if (prompt.length === 0) throw new GeminiBridgeError("INVALID_INPUT", "Prompt cannot be empty.");
  if (prompt.length > MAX_PROMPT_CHARS) {
    throw new GeminiBridgeError(
      "INVALID_INPUT",
      `Prompt is too long; keep it within ${MAX_PROMPT_CHARS} characters.`,
    );
  }
  return prompt;
}

export class GeminiBrowserBridge {
  constructor({ runtime = new PersistentBrowserRuntime() } = {}) {
    this.baseRuntime = runtime;
    this.runtimeContext = new AsyncLocalStorage();
    this.pool = new WebTaskPool(runtime);
  }

  get runtime() { return this.runtimeContext.getStore() ?? this.baseRuntime; }

  async browserStatus() {
    const providers = providerStatuses();
    return { ...await this.runtime.status(), defaultProvider: DEFAULT_PROVIDER,
      providers, supportedModels: [...providers.gemini.supportedModels], quotaFallbackSupported: true, reasoningEffortSupported: true,
      attachments: { enabled: true, maxFiles: 10, maxFileBytes: 100_000_000,
        providers: { aistudio: { enabled: true }, gemini: { enabled: true } } } };
  }

  async recoverRequest({ requestId = null, signal } = {}) {
    return this.pool.exclusive(signal, async () => {
      const runtimes = [this.baseRuntime, ...[1, 2, 3].map(slot => this.baseRuntime.taskRuntime(slot))];
      if (requestId) {
        for (const runtime of runtimes) {
          const state = await runtime.read();
          if ((state?.inFlight ?? state?.quarantined)?.requestId === requestId) return runtime.recoverPending(requestId);
        }
        const record = (await this.baseRuntime.recoveryHistory()).find(item => item.requestId === requestId);
        const runtime = record?.slotId ? this.baseRuntime.taskRuntime(record.slotId) : this.baseRuntime;
        return runtime.recoverPending(requestId);
      }
      const records = [];
      for (const runtime of runtimes) { const record = await runtime.recoverPending(); if (record) records.push(record); }
      return records.length > 1 ? records : records[0] ?? null;
    });
  }

  async findBrowser() { return this.runtime.findBrowser(); }
  async shutdownBrowser() { return this.runtime.shutdown(); }

  async stopBrowser(signal) {
    return this.pool.exclusive(signal, async () => {
      for (const slot of [1, 2, 3]) await this.baseRuntime.taskRuntime(slot).recoverPending();
      return this.baseRuntime.shutdown();
    });
  }

  async closeOwnedBrowserIfIdle(signal) {
    return this.pool.exclusive(signal, async () => {
      const state = await this.baseRuntime.read();
      if (!state) return { stopped: false, reason: "not-running" };
      const tasks = await Promise.all([1, 2, 3].map(slot => this.baseRuntime.taskRuntime(slot).read()));
      if (state.inFlight || state.quarantined || tasks.some(task => task?.inFlight || task?.quarantined)) {
        return { stopped: false, reason: "active-or-unresolved" };
      }
      return this.baseRuntime.shutdown();
    });
  }

  async spawnBrowser({ headless, signal }) {
    if (!headless) throw new GeminiBridgeError("INVALID_INPUT", "Use the explicit login operation for visible windows.");
    return this.runtime.start(signal);
  }

  async startBrowser(signal) {
    return this.spawnBrowser({ headless: true, signal });
  }

  async openTarget(port) {
    return this.runtime.openTarget(port);
  }

  async waitForComposer(client, onProgress, signal, expectedUrl, provider = DEFAULT_PROVIDER) {
    const adapter = pageAdapter(provider);
    const config = providerConfig(provider);
    const deadline = Date.now() + 60_000;
    const expected = new URL(expectedUrl);
    const expectedPath = expected.pathname.replace(/\/$/, "");
    let readyChecks = 0;
    while (Date.now() < deadline) {
      if (signal?.aborted) throw new GeminiBridgeError("CANCELLED", "Operation cancelled.", { provider });
      const page = await client.call(adapter.inspect.toString());
      if (page?.signedOut === true || /accounts\.google\.com/i.test(page?.url ?? "")) {
        throw new GeminiBridgeError(
          "LOGIN_REQUIRED",
          `Sign in to ${config.label} in the dedicated browser window, close it, and retry.`,
          { provider },
        );
      }
      let pageUrl = null;
      try { pageUrl = new URL(page.url); } catch {}
      if (
        page?.composerReady &&
        page.signedOut !== true &&
        pageUrl != null &&
        pageUrl.origin === config.origin &&
        pageUrl.pathname.replace(/\/$/, "") === expectedPath
      ) {
        readyChecks += 1;
        if (readyChecks >= 4) return page;
      } else {
        readyChecks = 0;
      }
      await delay(750);
    }
    throw new GeminiBridgeError(
      "COMPOSER_TIMEOUT",
      `Timed out waiting for the ${config.label} input box.`,
      { provider, retrySafe: true },
    );
  }

  async verifyLogin(onProgress, signal, provider = DEFAULT_PROVIDER) {
    const config = providerConfig(provider);
    let client = null;
    let port = null;
    try {
      await this.runtime.recoverPending();
      port = await this.startBrowser(signal);
      await this.runtime.assertNoPending();
      ({ client } = await this.openTarget(port));
      await client.send("Page.navigate", { url: config.home });
      await onProgress?.(`Verifying the ${config.label} login.`, 90);
      await this.waitForComposer(client, onProgress, signal, config.home, provider);
      return true;
    } finally {
      client?.close();
    }
  }

  async spawnLoginBrowser(signal, provider = DEFAULT_PROVIDER) {
    const config = providerConfig(provider);
    // Shut down any existing headless browser that may hold the profile lock.
    await this.shutdownBrowser();
    await this.runtime.assertProfileAvailable();
    const executable = await this.findBrowser();
    if (executable == null) {
      throw new GeminiBridgeError(
        "BROWSER_NOT_FOUND",
        "No compatible browser was found. Install Google Chrome before using Gemini Web Bridge.",
      );
    }
    // Launch Chrome WITHOUT --remote-debugging-port and --remote-debugging-address.
    // Google blocks OAuth sign-in when it detects CDP remote-debugging flags, showing
    // "This browser or app may not be secure".  A clean launch avoids this entirely.
    const args = [
      `--user-data-dir=${paths.profile}`,
      ...browserProxyArgs(),
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-quic",
      "--window-size=1440,1000",
      "--new-window",
      config.home,
    ];
    return spawn(executable, args, { detached: true, stdio: "ignore" });
  }

  async launchHumanLogin({ signal, wait = true, provider = DEFAULT_PROVIDER } = {}, onProgress) {
    provider = normalizeProvider(provider);
    const config = providerConfig(provider);
    return this.pool.exclusive(signal, async () => {
        for (const slot of [1, 2, 3]) await this.baseRuntime.taskRuntime(slot).recoverPending();
        const child = await this.spawnLoginBrowser(signal, provider);
        await onProgress?.(
          `Sign in to ${config.label} in the visible browser, then fully Quit (Cmd+Q) that dedicated browser when done.`,
          10,
        );
        if (!wait) {
          child.unref();
          return { loginVerified: false, message: `${config.label} login window opened.`, profile: paths.profile, provider };
        }

        // On macOS, closing the last Chrome window does NOT exit the process
        // (it stays in the Dock).  We poll for two conditions:
        //   1. The child process exited on its own (user pressed Cmd-Q), OR
        //   2. The child is still alive but has no visible windows (user clicked ✕).
        // In case 2 we send SIGTERM so the profile is released cleanly.
        let exited = false;
        child.on("exit", () => { 
          exited = true; 
        });

        const startedAt = Date.now();
        const deadline = startedAt + LOGIN_TIMEOUT_MS;
        while (Date.now() < deadline) {
          if (signal?.aborted) {
            if (!exited) child.kill();
            throw new GeminiBridgeError("CANCELLED", "Login was cancelled.");
          }
          if (exited) break;
          await delay(750);
        }

        if (!exited) {
          child.kill();
          // Wait briefly for the kill to take effect.
          const killDeadline = Date.now() + 3_000;
          while (!exited && Date.now() < killDeadline) await delay(200);
        }
        child.unref();

        if (Date.now() >= deadline && !exited) {
          throw new GeminiBridgeError(
            "LOGIN_TIMEOUT",
            "The Gemini login window remained open for more than 10 minutes.",
          );
        }

        await delay(500);
        const loginVerified = await this.verifyLogin(onProgress, signal, provider);
        return {
          loginVerified,
          message: `${config.label} login verified.`,
          profile: paths.profile,
          provider,
        };
      },
    );
  }

  async waitForAnswer(client, before, onProgress, signal, context) {
    const provider = normalizeProvider(context.provider ?? DEFAULT_PROVIDER);
    const config = providerConfig(provider);
    const adapter = pageAdapter(provider);
    const stableTarget = provider === "aistudio" ? 6 : 4;
    const startedAt = Date.now();
    const noResponseTimeout = context.hasAttachments ? 180_000 : NO_RESPONSE_TIMEOUT_MS;
    const deadline = startedAt + (context.hasAttachments ? 300_000 : ANSWER_TIMEOUT_MS);
    let lastChangedAt = startedAt;
    let lastText = "";
    let lastUrl = context.conversationUrl;
    let lastBilling = context.billing ?? null;
    let stableChecks = 0;
    let ticks = 0;
    while (Date.now() < deadline) {
      if (signal?.aborted) {
        throw new GeminiBridgeError("CANCELLED", "Generation was cancelled.", {
          ...context,
          provider,
          conversationUrl: lastUrl,
          partialChars: lastText.length,
          phase: lastText.length > 0 ? BRIDGE_PHASES.GENERATING : BRIDGE_PHASES.SUBMITTED,
        });
      }
      const state = await client.call(adapter.readGenerationState.toString(), [before, context.model]);
      lastUrl = state.url ?? lastUrl;
      if (state.billing) lastBilling = state.billing;
      const phase = lastText.length > 0 ? BRIDGE_PHASES.GENERATING : BRIDGE_PHASES.SUBMITTED;
      if (state.failure?.kind === "RATE_LIMITED") {
        throw new GeminiBridgeError("RATE_LIMITED", `${config.label} reached the account usage limit. ${state.failure.text ?? ""}`, {
          ...context,
          provider,
          billing: lastBilling,
          conversationUrl: lastUrl,
          partialChars: lastText.length,
          phase,
          quota: { scope: "model", exhausted_models: context.model ? [context.model] : [], evidence: [{ model: context.model ?? null, text: state.failure.text ?? "" }] },
        });
      }
      if (state.failure?.kind === "INTERACTION_REQUIRED") {
        throw new GeminiBridgeError(
          "INTERACTION_REQUIRED",
          `${config.label} requires manual verification in the dedicated browser.`,
          { ...context, provider, billing: lastBilling, conversationUrl: lastUrl, partialChars: lastText.length, phase },
        );
      }
      if (state.failure?.kind === "MODEL_MISMATCH") {
        throw new GeminiBridgeError(
          "MODEL_MISMATCH",
          `${config.label} no longer showed the verified ${modelLabel(resolvedProvider, context.model)} model; the result was not accepted.`,
          { ...context, provider, billing: lastBilling, conversationUrl: lastUrl, partialChars: lastText.length, phase },
        );
      }
      if (["BILLING_UNVERIFIED", "PAID_KEY_ACTIVE", "API_KEY_REQUIRED"].includes(state.failure?.kind)) {
        throw new GeminiBridgeError(
          state.failure.kind,
          `${config.label} result was not accepted: ${state.failure.text ?? "subscription/API-key billing evidence changed."}`,
          { ...context, provider, billing: lastBilling, conversationUrl: lastUrl, partialChars: lastText.length, phase },
        );
      }
      if (["PERMISSION_DENIED", "ACCESS_DENIED", "MODEL_UNAVAILABLE", "MODEL_ERROR"].includes(state.failure?.kind)) {
        const code = state.failure.kind === "ACCESS_DENIED" ? "ACCESS_DENIED" : state.failure.kind;
        throw new GeminiBridgeError(
          code,
          `${config.label} reported ${code} after submission: ${state.failure.text ?? "model error"}`,
          {
            ...context,
            provider,
            billing: lastBilling,
            conversationUrl: lastUrl,
            partialChars: lastText.length,
            phase,
          },
        );
      }
      if (state.failure?.kind === "TRANSIENT") {
        throw new GeminiBridgeError("GEMINI_TRANSIENT", `${config.label} displayed a generation error.`, {
          ...context,
          provider,
          billing: lastBilling,
          conversationUrl: lastUrl,
          partialChars: lastText.length,
          phase,
        });
      }
      if (state.isNew && state.snapshot.text === lastText) {
        stableChecks += 1;
      } else if (state.isNew) {
        stableChecks = 0;
        lastText = state.snapshot.text;
        lastChangedAt = Date.now();
      } else {
        stableChecks = 0;
      }
      ticks += 1;
      if (ticks % 8 === 0) {
        await onProgress?.(`${config.label} is generating; received ${lastText.length} characters.`, 50);
      }
      const completeByControls = provider === "aistudio" ? state.complete === true :
        state.isNew && !state.stopVisible && state.snapshot.completedCount > (before?.completedCount ?? 0);
      if (lastText.length > 0 && stableChecks >= stableTarget && completeByControls) {
        return { answer: lastText, billing: state.billing ?? context.billing ?? null, conversationUrl: lastUrl, provider };
      }
      if (!state.isNew && Date.now() - startedAt >= noResponseTimeout) {
        throw new GeminiBridgeError("NO_RESPONSE", `${config.label} did not return an answer within ${noResponseTimeout / 1000} seconds.`, {
          ...context,
          provider,
          billing: lastBilling,
          conversationUrl: lastUrl,
          phase: BRIDGE_PHASES.SUBMITTED,
        });
      }
      if (lastText.length > 0 && Date.now() - lastChangedAt >= STALLED_RESPONSE_TIMEOUT_MS) {
        throw new GeminiBridgeError(
          "RESPONSE_STALLED",
          `${config.label} stopped generating after ${lastText.length} characters without completing.`,
          {
            ...context,
            provider,
            billing: lastBilling,
            conversationUrl: lastUrl,
            partialChars: lastText.length,
            phase: BRIDGE_PHASES.GENERATING,
          },
        );
      }
      await delay(750);
    }
    throw new GeminiBridgeError(
      "GENERATION_TIMEOUT",
      `${config.label} did not complete within the generation timeout${lastText.length > 0 ? `; received ${lastText.length} characters` : ""}.`,
      {
        ...context,
        provider,
        billing: lastBilling,
        conversationUrl: lastUrl,
        partialChars: lastText.length,
        phase: lastText.length > 0 ? BRIDGE_PHASES.GENERATING : BRIDGE_PHASES.SUBMITTED,
      },
    );
  }

  async uploadAttachments(client, files, signal, provider = DEFAULT_PROVIDER) {
    const adapter = pageAdapter(provider);
    const config = providerConfig(provider);
    const expected = files.map(({ name, size }) => ({ name, size }));
    const names = files.map(file => file.name);
    const prepared = await client.call(adapter.prepareUpload.toString(), [provider === "aistudio" ? expected : names]);
    if (!prepared?.ok) {
      throw new GeminiBridgeError(
        prepared?.code ?? "UPLOAD_UNSUPPORTED",
        prepared?.message ?? `${config.label} upload controls are unavailable.`,
        { phase: BRIDGE_PHASES.UPLOADING, provider },
      );
    }
    for (const group of prepared.groups) {
      if (signal?.aborted) throw new GeminiBridgeError("CANCELLED", "Upload cancelled.", { phase: BRIDGE_PHASES.UPLOADING, provider });
      const selected = files.filter(file => group.names.includes(file.name));
      const current = await client.call(adapter.prepareUpload.toString(), [provider === "aistudio"
        ? selected.map(({ name, size }) => ({ name, size })) : group.names]);
      if (!current?.ok || current.groups.length !== 1) {
        throw new GeminiBridgeError(current?.code ?? "UPLOAD_UNSUPPORTED",
          current?.message ?? `${config.label} upload controls changed before selecting files.`,
          { phase: BRIDGE_PHASES.UPLOADING, provider });
      }
      const checked = await validateAttachments(selected.map(file => file.path));
      if (checked.some((file, i) => file.size !== selected[i].size || file.mtimeMs !== selected[i].mtimeMs)) {
        throw new GeminiBridgeError("UPLOAD_FAILED", "An attachment changed before upload.", { phase: BRIDGE_PHASES.UPLOADING, provider });
      }
      const remote = await client.send("Runtime.evaluate", {
        expression: `document.querySelectorAll('input[type="file"]')[${current.groups[0].index}]`, returnByValue: false,
      });
      const objectId = remote.result?.objectId;
      if (!objectId) throw new GeminiBridgeError("UPLOAD_FAILED", "The file input disappeared before upload.", { phase: BRIDGE_PHASES.UPLOADING, provider });
      try { await client.send("DOM.setFileInputFiles", { objectId, files: selected.map(file => file.path) }); }
      finally { await client.send("Runtime.releaseObject", { objectId }, 1000).catch(() => {}); }
    }
    let readyChecks = 0;
    let lastState = null;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      if (signal?.aborted) throw new GeminiBridgeError("CANCELLED", "Upload cancelled.", { phase: BRIDGE_PHASES.UPLOADING, provider });
      const state = await client.call(adapter.readUploadState.toString(), [provider === "aistudio" ? expected : names]);
      lastState = state;
      if (state?.failure) throw new GeminiBridgeError(state.code ?? "UPLOAD_FAILED", state.failure.slice(0, 240),
        { phase: BRIDGE_PHASES.UPLOADING, provider, upload: state });
      readyChecks = state?.ready ? readyChecks + 1 : 0;
      if (readyChecks >= 2) return;
      await delay(500);
    }
    throw new GeminiBridgeError("OUTCOME_UNKNOWN", "Attachment upload did not reach a confirmed ready state. Inspect the existing draft before retrying.", { phase: BRIDGE_PHASES.UPLOADING, provider, upload: lastState });
  }

  async runAttempt({ conversationId, destination, provider = DEFAULT_PROVIDER, prompt, requestId, signal, model, reasoningEffort = null, files = [] }, onProgress) {
    const resolvedProvider = normalizeProvider(provider);
    const config = providerConfig(resolvedProvider);
    const adapter = pageAdapter(resolvedProvider);
    let client = null;
    let completed = false;
    let conversationUrl = destination;
    let phase = BRIDGE_PHASES.PRE_SUBMIT;
    let port = null;
    try {
      await this.runtime.recoverPending();
      port = await this.startBrowser(signal);
      await this.runtime.assertNoPending();
      await this.runtime.assertConversationAvailable(destination, resolvedProvider);
      await this.runtime.markRequest({ requestId, phase, conversationUrl, model, provider: resolvedProvider });
      const opened = await this.openTarget(port);
      client = opened.client;
      if (conversationId == null || opened.target.url !== destination) {
        await client.send("Page.navigate", { url: destination });
      }
      await this.waitForComposer(client, onProgress, signal, destination, resolvedProvider);
      if (conversationId != null) await delay(4_000);
      await onProgress?.(`Waiting for the ${config.label} model selector and verifying ${modelLabel(resolvedProvider, model)}.`, 20);
      const selected = await client.call(adapter.ensureReady.toString(), [conversationId != null, model], 135_000);
      if (!selected?.ok) throw new GeminiBridgeError(selected?.code ?? "MODEL_UNAVAILABLE",
        selected?.message ?? "Unable to verify the selected model before submission.",
        { billing: selected?.billing, phase, provider: resolvedProvider, quota: selected?.quota });
      if (signal?.aborted) throw new GeminiBridgeError("CANCELLED", "Request cancelled before submission.", { provider: resolvedProvider });
      if (files.length) {
        phase = BRIDGE_PHASES.UPLOADING;
        await this.runtime.markRequest({ requestId, phase, conversationUrl, provider: resolvedProvider,
          attachments: files.map(({ name, size }) => ({ name, size })) });
        await onProgress?.(`Uploading ${files.length} selected context file(s) to ${config.label}.`, 22);
        await this.uploadAttachments(client, files, signal, resolvedProvider);
      }
      // A different tab may update account-wide model preferences while this
      // task uploads files. Verify this page again immediately before sending.
      const finalReady = await client.call(adapter.ensureReady.toString(), [true, model], 135_000);
      if (!finalReady?.ok) throw new GeminiBridgeError(finalReady?.code ?? "MODEL_MISMATCH",
        finalReady?.message ?? "The page model or billing mode changed before submission.",
        { billing: finalReady?.billing, phase, provider: resolvedProvider, quota: finalReady?.quota });
      if (reasoningEffort != null) {
        const effort = await client.call(ensureReasoningEffort.toString(), [reasoningEffort], 15_000);
        if (!effort?.ok) throw new GeminiBridgeError("REASONING_UNAVAILABLE",
          effort?.message ?? "The requested reasoning effort requires an explicit verified page control.",
          { phase: BRIDGE_PHASES.PRE_SUBMIT, provider: resolvedProvider, retry_safe: true });
      }
      if (adapter.prepareInput) {
        const input = await client.call(adapter.prepareInput.toString());
        if (!input?.ok) throw new GeminiBridgeError(input?.code ?? "UI_CHANGED",
          input?.message ?? "Unable to focus the prompt input.", { phase, provider: resolvedProvider });
        await client.send("Input.insertText", { text: prompt });
      }
      await onProgress?.(`Submitting a prompt to ${config.label}.`, 25);
      // Runtime.callFunctionOn may disconnect after the page has clicked Run.
      // From this point onward, treat the outcome as submitted rather than risk a duplicate.
      const baseline = await client.call(adapter.readGenerationState.toString(), [{}, model]);
      phase = BRIDGE_PHASES.SUBMITTED;
      await this.runtime.markRequest({ requestId, phase, conversationUrl, provider: resolvedProvider,
        before: { count: baseline.snapshot.count, completedCount: baseline.snapshot.completedCount,
          latestTurnId: baseline.snapshot.latestTurnId ?? null } });
      const submission = await client.call(
        adapter.submit.toString(),
        [prompt, model, Boolean(adapter.prepareInput)],
        30_000,
      );
      if (submission == null) {
        throw new GeminiBridgeError("OUTCOME_UNKNOWN",
          `${config.label} returned no submission evidence; the outcome is unknown.`,
          { phase, provider: resolvedProvider });
      }
      if (submission.ok === false) {
        // AI Studio's submit function re-checks model/billing itself. A false
        // result with clicked=false means no Run click happened.
        if (submission.clicked !== true) {
          phase = BRIDGE_PHASES.PRE_SUBMIT;
          await this.runtime.markRequest({ requestId, phase, conversationUrl, model, provider: resolvedProvider });
        }
        throw new GeminiBridgeError(submission.code ?? (submission.clicked === true ? "OUTCOME_UNKNOWN" : "UI_CHANGED"),
          submission.message ?? "Submission was not confirmed.",
          { billing: submission.billing, phase, provider: resolvedProvider });
      }
      if (submission.nativeClick) {
        const { x, y } = submission.nativeClick;
        await client.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
        await client.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
        await client.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
      }
      conversationUrl = submission.url ?? conversationUrl;
      await this.runtime.markRequest({ requestId, phase, conversationUrl, model, provider: resolvedProvider });
      const result = await this.waitForAnswer(client, submission.before, onProgress, signal, {
        conversationId,
        conversationUrl,
        hasAttachments: files.length > 0,
        model,
        provider: resolvedProvider,
        billing: submission.billing ?? finalReady.billing ?? null,
      });
      completed = true;
      await this.runtime.clearRequest(requestId);
      const billing = result.billing ?? (resolvedProvider === "gemini"
        ? { evidence: [], mode: config.billingMode, pro: null, apiKeySelected: false }
        : finalReady.billing ?? null);
      return { ...result, reasoning_effort: reasoningEffort, billing, billing_mode: config.billingMode,
        model, model_id: resolvedProvider === "aistudio" ? modelId(resolvedProvider, model) : null,
        provider: resolvedProvider, attachments: files.map(({ name, size }) => ({ name, size })) };
    } catch (error) {
      const normalized = normalizeBridgeError(error, {
        conversationId,
        conversationUrl,
        phase,
        provider: resolvedProvider,
      });
      normalized.requestId = error.requestId ?? requestId;
      if (normalized.upload != null) await this.runtime.markRequest({ requestId, phase, conversationUrl,
        provider: resolvedProvider, upload: normalized.upload });
      if (client != null && !completed && phase !== BRIDGE_PHASES.PRE_SUBMIT) {
        await client.call(adapter.cancel.toString(), [], 5_000).catch(() => {});
        // Recovery examines the original page and archives the outcome before
        // releasing its slot. A failed inspection preserves a separate target.
        try {
          normalized.recovery = await this.runtime.recoverPending();
          if (isSpecificConversationUrl(normalized.recovery?.conversationUrl, resolvedProvider)) {
            normalized.conversationUrl = normalized.recovery.conversationUrl;
          }
          if (normalized.recovery?.billing) normalized.billing = normalized.billing ?? normalized.recovery.billing;
        } catch {}
      }
      throw normalized;
    } finally { client?.close(); }
  }

  async ask({ conversationId = null, ownerThreadId = null, prompt, provider = null, signal, model = null, reasoningEffort = null, filePaths = [], closeAfter = false }, onProgress) {
    if (reasoningEffort != null && !["high", "xhigh"].includes(reasoningEffort)) {
      throw new GeminiBridgeError("INVALID_INPUT", "reasoning_effort must be high or xhigh.", { phase: "PRE_SUBMIT" });
    }
    const clean = cleanPrompt(prompt);
    const files = await validateAttachments(filePaths);
    const requestId = crypto.randomUUID();
    const requestedProvider = provider == null ? null : normalizeProvider(provider);
    let conversation = null;
    if (conversationId != null) {
      conversation = await getConversation(conversationId);
      if (conversation == null) {
        throw new GeminiBridgeError(
          "CONVERSATION_NOT_FOUND",
          `No local conversation was found for ${conversationId}.`,
          { conversationId, provider: requestedProvider ?? DEFAULT_PROVIDER },
        );
      }
    }
    const conversationProvider = conversation?.provider ?? DEFAULT_PROVIDER;
    if (conversation != null && requestedProvider != null && requestedProvider !== conversationProvider) {
      throw new GeminiBridgeError(
        "PROVIDER_MISMATCH",
        `This conversation belongs to ${providerConfig(conversationProvider).label}; start a fresh conversation to switch Web providers.`,
        { conversationId, provider: conversationProvider },
      );
    }
    const resolvedProvider = requestedProvider ?? conversationProvider;
    const config = providerConfig(resolvedProvider);
    try {
      model = canonicalModel(resolvedProvider, model ?? conversation?.model ?? null);
    } catch (error) {
      throw new GeminiBridgeError(error?.code ?? "INVALID_INPUT", error.message, {
        conversationId,
        provider: resolvedProvider,
      });
    }
    if (conversation?.model && conversation.model !== model) {
      throw new GeminiBridgeError("MODEL_MISMATCH", "Start a fresh conversation to change its model.", {
        conversationId,
        provider: resolvedProvider,
      });
    }

    const destination = conversationDestination(resolvedProvider, conversation?.conversationUrl ?? null, model);
    if (resolvedProvider === "aistudio" && (await this.baseRuntime.read())?.browserMode !== "headed") {
      await onProgress?.("Preparing the dedicated visible Chrome window for AI Studio.", 10);
      // Acquire all slots before a mode upgrade; existing work settles first.
      await this.pool.exclusive(signal, () => this.baseRuntime.start(signal, { mode: "headed" }));
    }
    const result = await this.pool.run({ signal, conversationUrl: destination, provider: resolvedProvider }, scoped => this.runtimeContext.run(scoped,
      async () => {
        let lastError = null;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            const result = await this.runAttempt(
              { conversationId, destination, prompt: clean, requestId, signal, model, reasoningEffort, files, provider: resolvedProvider },
              onProgress,
            );
            const savedId = isSpecificConversationUrl(result.conversationUrl, resolvedProvider) ? await saveConversation(
              {
                conversationId,
                conversationUrl: result.conversationUrl,
                ownerThreadId,
                model,
                provider: resolvedProvider,
              },
              signal,
            ) : null;
            await onProgress?.(`${config.label} returned a complete answer.`, 100);
            return {
              ...result,
              billing_mode: resolvedProvider === "aistudio" ? "subscription" : (result.billing_mode ?? config.billingMode),
              conversationId: savedId,
              model,
              model_id: resolvedProvider === "aistudio" ? modelId(resolvedProvider, model) : null,
              provider: resolvedProvider,
              requestId,
            };
          } catch (error) {
            lastError = normalizeBridgeError(error, { conversationId, provider: resolvedProvider });
            if (lastError.conversationId == null && isSpecificConversationUrl(lastError.conversationUrl, resolvedProvider)) {
              lastError.conversationId = await saveConversation(
                { conversationUrl: lastError.conversationUrl, ownerThreadId, model, provider: resolvedProvider },
                signal,
              );
            }
            if (shouldRetryBridgeError(lastError, attempt, signal?.aborted)) {
              await onProgress?.("A pre-submit browser failure occurred; retrying once safely.", 15);
              await delay(1_000);
              continue;
            }
            throw lastError;
          }
        }
        throw lastError;
      },
    ));
    if (closeAfter === true && resolvedProvider === "aistudio") {
      const cleanup = await this.closeOwnedBrowserIfIdle(signal);
      result.browserClosed = cleanup?.stopped === true;
    }
    return result;
  }

  async analyze({ language, question, signal, url }, onProgress) {
    const video = canonicalizeYoutubeUrl(url);
    const legacy = await findLegacyConversation(video.videoId);
    const result = await this.ask(
      {
        conversationId: legacy?.conversationId ?? null,
        ownerThreadId: process.env.CODEX_THREAD_ID ?? null,
        prompt: buildGeminiPrompt({ language, question, url: video.url }),
        signal,
      },
      onProgress,
    );
    await saveConversation(
      {
        conversationId: result.conversationId,
        conversationUrl: result.conversationUrl,
        legacyVideoId: video.videoId,
        provider: "gemini",
        ownerThreadId: process.env.CODEX_THREAD_ID ?? null,
      },
      signal,
    );
    return { ...result, video };
  }
}

export { AISTUDIO_HOME, GEMINI_HOME, MAX_PROMPT_CHARS, normalizeBridgeError };
