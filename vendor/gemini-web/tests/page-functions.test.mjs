import assert from "node:assert/strict";
import test from "node:test";

import {
  cancelGeminiGeneration,
  inspectGeminiPage,
  readGeminiGenerationState,
  submitGeminiPrompt,
} from "../src/gemini-page.mjs";
import {
  cancelAistudioGeneration,
  ensureAistudioModelAndBilling,
  inspectAistudioPage,
  readAistudioGenerationState,
  submitAistudioPrompt,
} from "../src/aistudio-page.mjs";

test("page automation functions remain self-contained for CDP serialization", () => {
  assert.doesNotMatch(inspectGeminiPage.toString(), /\[native code\]/);
  assert.doesNotMatch(cancelGeminiGeneration.toString(), /\[native code\]/);
  assert.doesNotMatch(readGeminiGenerationState.toString(), /\[native code\]/);
  assert.doesNotMatch(submitGeminiPrompt.toString(), /\[native code\]/);
  assert.match(submitGeminiPrompt.toString(), /innerText.*!== prompt/s);
  assert.match(inspectGeminiPage.toString(), /signedOut/);
  assert.match(readGeminiGenerationState.toString(), /getClientRects/);
  assert.match(readGeminiGenerationState.toString(), /response-footer\.complete/);
  assert.match(readGeminiGenerationState.toString(), /RATE_LIMITED/);
  assert.match(readGeminiGenerationState.toString(), /INTERACTION_REQUIRED/);
});

test("AI Studio page functions are self-contained and use literal defaults", () => {
  for (const fn of [inspectAistudioPage, ensureAistudioModelAndBilling, submitAistudioPrompt,
    readAistudioGenerationState, cancelAistudioGeneration]) {
    const source = fn.toString();
    assert.doesNotMatch(source, /\[native code\]/);
    assert.doesNotMatch(source, /\bAISTUDIO_MODEL_ID\b/);
  }
  assert.match(ensureAistudioModelAndBilling.toString(), /model = "4 Argon"/);
  assert.match(submitAistudioPrompt.toString(), /expectedModel = "4 Argon"/);
  assert.match(readAistudioGenerationState.toString(), /turn-footer/);
  assert.match(readAistudioGenerationState.toString(), /model-error/);
});
