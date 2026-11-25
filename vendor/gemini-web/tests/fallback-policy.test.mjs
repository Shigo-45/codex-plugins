import assert from "node:assert/strict";
import test from "node:test";
import { fallbackFor } from "../src/fallback-policy.mjs";
import { GeminiBridgeError, normalizeBridgeError, shouldRetryBridgeError } from "../src/gemini-browser.mjs";

test("Web quota errors preserve evidence and route an authorized workflow to terminal exhaustion", () => {
  const quota = { scope: "web", exhausted_models: ["4 Argon", "4 Hydrogen", "3.8 Flash"], evidence: [{ text: "用量限额重置时间：20:22" }] };
  const error = normalizeBridgeError(Object.assign(new Error("quota exhausted"), { code: "WEB_QUOTA_EXHAUSTED", quota }));
  assert.equal(error.code, "WEB_QUOTA_EXHAUSTED");
  assert.deepEqual(error.quota, quota);
  assert.equal(fallbackFor(error).channel, null);
  assert.equal(fallbackFor(error).model, null);
  assert.equal(shouldRetryBridgeError(error, 0), false);
});

test("single-model quota advances Argon to Carbon to Neon to Flash and terminal exhaustion", () => {
  for (const [model, channel, next] of [
    ["4 Argon", "web", "4 Carbon"],
    ["4 Carbon", "web", "4 Neon"],
    ["4 Neon", "web", "3.8 Flash"],
    
    ["3.8 Flash", null, null],
  ]) {
    const error = new GeminiBridgeError("RATE_LIMITED", "limit", { model, quota: { exhausted_models: [model] } });
    assert.equal(fallbackFor(error).channel, channel);
    assert.equal(fallbackFor(error).model, next);
  }
});

test("AI Studio rate limits advance Argon to Carbon to Neon to Flash before Web", () => {
  for (const [model, channel, next] of [
    ["4 Argon", "aistudio", "4 Carbon"],
    ["4 Carbon", "aistudio", "4 Neon"],
    ["4 Neon", "aistudio", "3.8 Flash"],
    
    ["3.8 Flash", "web", "4 Argon"],
  ]) {
    const error = new GeminiBridgeError("RATE_LIMITED", "limit", {
      model,
      phase: "PRE_SUBMIT",
      provider: "aistudio",
      quota: { exhausted_models: [model], scope: "model" },
    });
    assert.equal(fallbackFor(error).channel, channel);
    assert.equal(fallbackFor(error).model, next);
  }
});

test("uncertain and completed submitted requests stay behind recovery guards", () => {
  for (const outcome of [undefined, "OUTCOME_UNKNOWN", "DETACHED_GENERATING", "SETTLED_UNKNOWN", "COMPLETED"]) {
    const error = new GeminiBridgeError("WEB_QUOTA_EXHAUSTED", "limit", { phase: "SUBMITTED" });
    error.recovery = outcome ? { outcome } : null;
    assert.equal(fallbackFor(error), null);
  }
  const failed = new GeminiBridgeError("WEB_QUOTA_EXHAUSTED", "limit", { phase: "SUBMITTED" });
  failed.recovery = { outcome: "FAILED" };
  assert.equal(fallbackFor(failed).model, null);
  assert.equal(fallbackFor(new GeminiBridgeError("MODEL_UNAVAILABLE", "unknown UI")), null);
});
