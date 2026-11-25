import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { runInNewContext } from "node:vm";

import { readAistudioUploadState } from "../src/aistudio-upload.mjs";
import { createAistudioPage, FakeElement } from "./aistudio-fixture.mjs";

process.env.GEMINI_WEB_BRIDGE_HOME = await mkdtemp(join(tmpdir(), "gemini-provider-routing-"));

const {
  GeminiBridgeError,
  GeminiBrowserBridge,
} = await import("../src/gemini-browser.mjs");
const { fallbackFor } = await import("../src/fallback-policy.mjs");
const {
  getConversation,
  paths,
  saveConversation,
} = await import("../src/state-store.mjs");

function pageContext(page) {
  return {
    Date: page.Date,
    Event: page.Event,
    InputEvent: page.InputEvent,
    URL,
    document: page.document,
    getComputedStyle: page.getComputedStyle,
    location: page.location,
    queueMicrotask,
    setTimeout: page.setTimeout,
  };
}

function fakeCdpClient(page, hooks = {}) {
  const calls = [];
  let readCalls = 0;
  return {
    calls,
    get readCalls() { return readCalls; },
    async call(declaration, args = []) {
      calls.push({ args, declaration, method: "call" });
      const value = await runInNewContext(`(${declaration})`, pageContext(page))(...args);
      if (/function readAistudioUploadState/.test(declaration)) {
        readCalls += 1;
        hooks.afterRead?.(readCalls, value);
      }
      return value;
    },
    async send(method, params = {}) {
      calls.push({ method, params });
      if (method === "Runtime.evaluate") return { result: { objectId: "aistudio-file-input" } };
      if (method === "DOM.setFileInputFiles") {
        hooks.setFiles?.(params);
        return {};
      }
      if (method === "Runtime.releaseObject") return {};
      throw new Error(`Unexpected CDP method in test: ${method}`);
    },
    close() {},
  };
}

const routingRuntimeRoot = await mkdtemp(join(tmpdir(), "gemini-routing-runtime-"));
function routingRuntime() {
  const slot = { read: async () => null, recoverPending: async () => null };
  return {
    root: routingRuntimeRoot,
    read: async () => ({ browserMode: "headed", inFlight: null, poolVersion: 1, version: 3 }),
    write: async () => {},
    recoverPending: async () => null,
    recoveryHistory: async () => [],
    status: async () => ({}),
    taskRuntime: () => slot,
  };
}

async function makeContextFile(root, name = "context.txt") {
  const path = join(root, name);
  await writeFile(path, "synthetic context");
  const info = await stat(path);
  return { mtimeMs: info.mtimeMs, name, path, size: info.size };
}

class RoutingBridge extends GeminiBrowserBridge {
  inputs = [];

  constructor() {
    super({ runtime: routingRuntime() });
  }

  async runAttempt(input) {
    this.inputs.push(input);
    return {
      answer: `answer:${input.provider}`,
      billing: input.provider === "aistudio"
        ? { apiKeySelected: false, evidence: [{ kind: "pro_badge", text: "PRO" }], mode: "subscription", pro: true }
        : null,
      conversationUrl: input.provider === "aistudio"
        ? "https://aistudio.google.com/prompts/ai-fixture"
        : "https://gemini.google.com/app/gemini-fixture",
    };
  }
}

test("legacy ask defaults to Gemini while AI Studio is explicit and canonical", async () => {
  const bridge = new RoutingBridge();
  const legacy = await bridge.ask({ prompt: "legacy prompt" });
  assert.equal(bridge.inputs[0].provider, "gemini");
  assert.equal(bridge.inputs[0].model, "4 Argon");
  assert.equal(bridge.inputs[0].destination, "https://gemini.google.com/app");
  assert.equal(legacy.provider, "gemini");

  const studio = await bridge.ask({ prompt: "studio prompt", provider: "aistudio" });
  assert.equal(bridge.inputs[1].provider, "aistudio");
  assert.equal(bridge.inputs[1].model, "4 Argon");
  assert.equal(bridge.inputs[1].destination, "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon");
  assert.equal(studio.provider, "aistudio");
  assert.equal(studio.model, "4 Argon");
  assert.equal(studio.model_id, "gemini-4-argon");
  assert.equal(studio.billing_mode, "subscription");
  assert.equal(studio.billing.mode, "subscription");
  assert.equal((await getConversation(studio.conversationId)).provider, "aistudio");
  assert.equal((await getConversation(studio.conversationId)).model, "4 Argon");

  const hydrogen = await bridge.ask({ prompt: "hydrogen studio prompt", provider: "aistudio", model: "4 Hydrogen" });
  assert.equal(bridge.inputs.at(-1).provider, "aistudio");
  assert.equal(bridge.inputs.at(-1).model, "4 Hydrogen");
  assert.equal(bridge.inputs.at(-1).destination, "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen");
  assert.equal(hydrogen.model, "4 Hydrogen");
  assert.equal(hydrogen.model_id, "gemini-4-hydrogen");
});

test("AI Studio follow-ups inherit provider/model and reject explicit provider or model switches", async () => {
  const bridge = new RoutingBridge();
  const created = await bridge.ask({ prompt: "first studio", provider: "aistudio" });
  const follow = await bridge.ask({ prompt: "second studio", conversationId: created.conversationId });
  assert.equal(follow.provider, "aistudio");
  assert.equal(bridge.inputs.at(-1).provider, "aistudio");
  assert.equal(bridge.inputs.at(-1).model, "4 Argon");
  await assert.rejects(
    () => bridge.ask({ prompt: "wrong provider", conversationId: created.conversationId, provider: "gemini" }),
    (error) => error.code === "PROVIDER_MISMATCH" && error.provider === "aistudio",
  );
  await assert.rejects(
    () => bridge.ask({ prompt: "wrong model", conversationId: created.conversationId, model: "4 Hydrogen" }),
    (error) => error.code === "MODEL_MISMATCH" && error.provider === "aistudio",
  );
});

test("AI Studio rejects unsupported models before any attempt runs", async () => {
  const bridge = new RoutingBridge();
  const before = bridge.inputs.length;
  await assert.rejects(
    () => bridge.ask({ prompt: "must not send", provider: "aistudio", model: "5 Neon" }),
    (error) => error.code === "INVALID_INPUT" && /4 Hydrogen/.test(error.message),
  );
  assert.equal(bridge.inputs.length, before);
  await assert.rejects(() => bridge.ask({ prompt: "bad provider", provider: "api" }), (error) => error.code === "INVALID_INPUT");
  assert.equal(bridge.inputs.length, before);
});

test("old conversation records without a provider remain Gemini", async () => {
  const id = await saveConversation({ conversationUrl: "https://gemini.google.com/app/old-record", model: "3.8 Flash" });
  const stored = JSON.parse(await readFile(paths.conversations, "utf8"));
  delete stored.conversations[id].provider;
  await writeFile(paths.conversations, JSON.stringify(stored));
  const conversation = await getConversation(id);
  assert.equal(conversation.provider, "gemini");
  assert.equal(conversation.model, "3.8 Flash");
  const bridge = new RoutingBridge();
  assert.equal((await bridge.ask({ prompt: "old follow-up", conversationId: id })).provider, "gemini");
  assert.equal(bridge.inputs.at(-1).model, "3.8 Flash");
});

test("conversation state rejects cross-provider URLs and infers provider only for known URLs", async () => {
  await assert.rejects(
    () => saveConversation({ conversationUrl: "https://gemini.google.com/app/not-ai", provider: "aistudio" }),
    /Invalid aistudio conversation URL/,
  );
  const inferred = await saveConversation({ conversationUrl: "https://aistudio.google.com/prompts/inferred?model=gemini-4-argon" });
  assert.equal((await getConversation(inferred)).provider, "aistudio");
  assert.equal((await getConversation(inferred)).model, null);
  await assert.rejects(
    () => saveConversation({ conversationUrl: "https://aistudio.google.com/app/prompts/unverified" }),
    /Invalid aistudio conversation URL/,
  );
});

test("malformed provider records error explicitly and are preserved", async () => {
  const id = await saveConversation({ conversationUrl: "https://gemini.google.com/app/malformed-provider" });
  const stored = JSON.parse(await readFile(paths.conversations, "utf8"));
  stored.conversations[id].provider = "openai";
  await writeFile(paths.conversations, JSON.stringify(stored));
  await assert.rejects(
    () => getConversation(id),
    (error) => error.code === "INVALID_INPUT" && /invalid provider/.test(error.message),
  );
  const after = JSON.parse(await readFile(paths.conversations, "utf8"));
  assert.equal(after.conversations[id].provider, "openai");
  assert.equal(after.conversations[id].conversationUrl, "https://gemini.google.com/app/malformed-provider");
  // Repair the fixture so later tests are not blocked by the deliberately bad record.
  delete after.conversations[id];
  await writeFile(paths.conversations, JSON.stringify(after));
});

test("AI Studio uploads use the bridge's existing prepare/select/read CDP contract", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "aistudio-upload-bridge-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const file = await makeContextFile(root);
  const page = createAistudioPage();
  const input = new FakeElement("input", {
    attrs: { accept: ".txt", type: "file" },
    className: "file-input",
  });
  const container = new FakeElement("div", {
    attrs: { "data-test-id": "prompt-media-container" },
  });
  page.document.body.append(input, container);
  page.elements.uploadInput = input;
  let spinner = null;
  input.onEvent = (event) => {
    if (event.type !== "change") return;
    spinner = new FakeElement("mat-progress-spinner", { className: "upload-progress" });
    container.append(spinner);
    for (const selected of Array.from(input.files ?? [])) {
      container.append(new FakeElement("div", {
        className: "file-preview-chip",
        text: `${selected.name}\n${selected.size} B`,
      }));
    }
  };
  const client = fakeCdpClient(page, {
    afterRead(callNumber, state) {
      if (callNumber === 1) {
        assert.equal(state.ready, false);
        assert.equal(state.busy, true);
        spinner.visible = false;
      }
    },
    setFiles(params) {
      input.files = params.files.map((path) => {
        assert.equal(path, file.path);
        return { name: file.name, size: file.size };
      });
      input.dispatchEvent(new page.Event("change", { bubbles: true }));
    },
  });

  const bridge = new GeminiBrowserBridge({ runtime: {} });
  await bridge.uploadAttachments(client, [file], null, "aistudio");

  assert.equal(client.calls.filter((call) => call.method === "DOM.setFileInputFiles").length, 1);
  assert.equal(client.calls.some((call) => call.method === "Runtime.evaluate"), true);
  assert.equal(input.files.length, 1);
  assert.equal(input.files[0].name, "context.txt");
  assert.equal(spinner.visible, false);
  assert.equal(client.readCalls >= 1, true);
  assert.equal(page.elements.run.clickCount, 0);

  const state = page.run(readAistudioUploadState)([{ name: file.name, size: file.size }]);
  assert.equal(state.ready, true);
  assert.deepEqual(JSON.parse(JSON.stringify(state.receipts)), [
    { name: file.name, size: file.size, sizeEvidence: "page" },
  ]);
});

test("AI Studio upload rejection happens before any file-input CDP command", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "aistudio-upload-reject-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const file = await makeContextFile(root);
  const page = createAistudioPage();
  page.document.body.append(new FakeElement("input", {
    attrs: { accept: ".pdf", type: "file" },
    className: "file-input",
  }));
  const client = fakeCdpClient(page);
  const bridge = new GeminiBrowserBridge({ runtime: {} });

  await assert.rejects(
    () => bridge.uploadAttachments(client, [file], null, "aistudio"),
    (error) => error.code === "UPLOAD_UNSUPPORTED" &&
      error.provider === "aistudio" && error.phase === "UPLOADING",
  );
  assert.equal(client.calls.filter((call) => call.method === "DOM.setFileInputFiles").length, 0);
  assert.equal(client.calls.filter((call) => call.method === "Runtime.evaluate").length, 0);
  assert.equal(page.elements.run.clickCount, 0);
});

test("AI Studio permission-denied turns fail with an accurate submitted phase", async () => {
  const bridge = new GeminiBrowserBridge({ runtime: {} });
  const client = {
    call: async (declaration) => {
      assert.match(declaration, /function readAistudioGenerationState/);
      return {
        billing: { apiKeySelected: false, evidence: [], mode: "subscription", pro: true },
        billing_mode: "subscription",
        complete: false,
        failure: { kind: "PERMISSION_DENIED", text: "权限遭拒。请检查您的账号权限和项目设置。" },
        isNew: false,
        model: "4 Argon",
        model_id: "gemini-4-argon",
        provider: "aistudio",
        snapshot: { count: 1, completedCount: 1, latestTurnId: "denied", text: "" },
        stopVisible: false,
        url: "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon",
      };
    },
  };
  await assert.rejects(
    () => bridge.waitForAnswer(client, { count: 0, latestTurnId: null }, null, null, {
      conversationUrl: "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon",
      hasAttachments: false,
      model: "4 Argon",
      provider: "aistudio",
    }),
    (error) => error.code === "PERMISSION_DENIED" &&
      error.phase === "SUBMITTED" && error.provider === "aistudio" &&
      /权限遭拒/.test(error.message),
  );
});

test("AI Studio quota fallback advances Argon to Carbon to Neon to Flash before Web", () => {
  const cases = [
    ["4 Argon", "aistudio", "4 Carbon"],
    ["4 Carbon", "aistudio", "4 Neon"],
    ["4 Neon", "aistudio", "3.8 Flash"],
    ["3.8 Flash", "web", "4 Argon"],
  ];
  for (const [model, channel, next] of cases) {
    const error = new GeminiBridgeError("RATE_LIMITED", "limit", {
      phase: "PRE_SUBMIT",
      provider: "aistudio",
      model,
      quota: { exhausted_models: [model], scope: "model" },
    });
    assert.equal(fallbackFor(error).channel, channel);
    assert.equal(fallbackFor(error).model, next);
  }
});

test("AI Studio quota fallback infers the next model from legacy model IDs", () => {
  const error = new GeminiBridgeError("RATE_LIMITED", "limit", {
    phase: "PRE_SUBMIT",
    provider: "aistudio",
    quota: { exhausted_models: ["gemini-4-argon"], scope: "model" },
  });
  assert.deepEqual(fallbackFor(error), {
    action: "continue_authorized_workflow",
    channel: "aistudio",
    model: "4 Carbon",
    reasoning_effort: "high",
    reason: "requested_model_rate_limited",
  });
  const exhausted = new GeminiBridgeError("WEB_QUOTA_EXHAUSTED", "all exhausted", {
    phase: "PRE_SUBMIT",
    provider: "aistudio",
  });
  assert.equal(fallbackFor(exhausted).channel, "web");
  assert.equal(fallbackFor(exhausted).model, "4 Argon");
});

test("AI Studio model-unavailable turns preserve MODEL_UNAVAILABLE after submission", async () => {
  const bridge = new GeminiBrowserBridge({ runtime: {} });
  const client = {
    call: async (declaration) => {
      assert.match(declaration, /function readAistudioGenerationState/);
      return {
        billing: { apiKeySelected: false, evidence: [], mode: "subscription", pro: true },
        billing_mode: "subscription",
        complete: false,
        failure: { kind: "MODEL_UNAVAILABLE", text: "Model isn't available right now. Please wait a minute and try again." },
        isNew: false,
        model: "4 Hydrogen",
        model_id: "gemini-4-hydrogen",
        provider: "aistudio",
        snapshot: { count: 1, completedCount: 1, latestTurnId: "unavailable", text: "" },
        stopVisible: false,
        url: "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen",
      };
    },
  };
  await assert.rejects(
    () => bridge.waitForAnswer(client, { count: 0, latestTurnId: null }, null, null, {
      conversationUrl: "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen",
      hasAttachments: false,
      model: "4 Hydrogen",
      provider: "aistudio",
    }),
    (error) => error.code === "MODEL_UNAVAILABLE" &&
      error.phase === "SUBMITTED" && error.provider === "aistudio" &&
      /Model isn't available right now/.test(error.message),
  );
});

test("requested reasoning effort survives public ask admission into the task slot", async () => {
  const bridge = new RoutingBridge();
  await bridge.ask({ prompt: "synthetic writing", model: "4 Neon", reasoningEffort: "xhigh" });
  assert.equal(bridge.inputs[0].model, "4 Neon");
  assert.equal(bridge.inputs[0].reasoningEffort, "xhigh");
  await assert.rejects(bridge.ask({ prompt: "synthetic", reasoningEffort: "low" }), error => error.code === "INVALID_INPUT");
});
