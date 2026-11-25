import assert from "node:assert/strict";
import test from "node:test";

import { hasVerifiedUpload } from "../src/provider-adapters.mjs";
import {
  canonicalModel,
  conversationDestination,
  DEFAULT_PROVIDER,
  inferProviderFromUrl,
  isConversationUrl,
  isHomeUrl,
  isProviderUrl,
  modelHome,
  modelId,
  modelLabel,
  normalizeProvider,
  providerConfig,
  providerStatuses,
} from "../src/providers.mjs";

test("provider selection defaults to legacy Gemini and rejects unknown values", () => {
  assert.equal(DEFAULT_PROVIDER, "gemini");
  assert.equal(normalizeProvider(undefined), "gemini");
  assert.equal(normalizeProvider(null), "gemini");
  assert.throws(() => normalizeProvider(""), (error) => error.code === "INVALID_INPUT");
  assert.equal(normalizeProvider("aistudio"), "aistudio");
  assert.throws(() => normalizeProvider("api"), (error) => error.code === "INVALID_INPUT");
});

test("provider homes are exact origin/path checks with query tolerated", () => {
  assert.equal(isHomeUrl("gemini", "https://gemini.google.com/app"), true);
  assert.equal(isHomeUrl("gemini", "https://gemini.google.com/app/"), true);
  assert.equal(isHomeUrl("gemini", "https://gemini.google.com/app?x=1"), true);
  assert.equal(isHomeUrl("gemini", "https://gemini.google.com.evil.test/app"), false);
  assert.equal(isHomeUrl("aistudio", "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"), true);
  assert.equal(isHomeUrl("aistudio", "https://aistudio.google.com/prompts/new_chat/"), true);
  assert.equal(isHomeUrl("aistudio", "https://aistudio.google.com/app/prompts/new_chat"), false);
  assert.equal(isHomeUrl("aistudio", "https://gemini.google.com/prompts/new_chat"), false);
});

test("conversation URL guards are provider-scoped and exclude the AI Studio landing route", () => {
  assert.equal(isConversationUrl("gemini", "https://gemini.google.com/app/abc123"), true);
  assert.equal(isConversationUrl("gemini", "https://aistudio.google.com/prompts/abc123"), false);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com/prompts/abc123"), true);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com/app/prompts/abc123?model=gemini-4-argon"), false);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com/prompts/new_chat"), false);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"), false);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com/settings"), false);
  assert.equal(isConversationUrl("aistudio", "https://aistudio.google.com.evil.test/prompts/abc"), false);
  assert.equal(isProviderUrl("aistudio", "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"), true);
});

test("provider-specific model canonicalization accepts labels and exact AI Studio IDs", () => {
  assert.equal(canonicalModel("gemini"), "4 Argon");
  assert.equal(canonicalModel("gemini", "4 Hydrogen"), "4 Hydrogen");
  assert.equal(canonicalModel("gemini", "3.8 Flash"), "3.8 Flash");
  assert.equal(canonicalModel("aistudio"), "4 Argon");
  assert.equal(canonicalModel("aistudio", "4 Argon"), "4 Argon");
  assert.equal(canonicalModel("aistudio", "gemini-4-argon"), "4 Argon");
  assert.equal(canonicalModel("aistudio", "4 Hydrogen"), "4 Hydrogen");
  assert.equal(canonicalModel("aistudio", "gemini-4-hydrogen"), "4 Hydrogen");
  assert.equal(canonicalModel("aistudio", "3.8 Flash"), "3.8 Flash");
  assert.equal(canonicalModel("aistudio", "gemini-3.8-flash"), "3.8 Flash");
  assert.throws(() => canonicalModel("aistudio", "5 Neon"), (error) => error.code === "INVALID_INPUT");
  assert.throws(() => canonicalModel("gemini", "gemini-4-argon"), (error) => error.code === "INVALID_INPUT");
});

test("model-specific AI Studio destinations preserve the exact official ID", () => {
  assert.equal(modelHome("aistudio", "4 Argon"), "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon");
  assert.equal(modelHome("aistudio", "4 Hydrogen"), "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen");
  assert.equal(modelHome("aistudio", "gemini-3.8-flash"), "https://aistudio.google.com/prompts/new_chat?model=gemini-3.8-flash");
  assert.equal(conversationDestination("aistudio", null, "4 Hydrogen"), "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen");
  assert.equal(conversationDestination("aistudio", "https://aistudio.google.com/prompts/abc", "4 Hydrogen"), "https://aistudio.google.com/prompts/abc");
  assert.equal(conversationDestination("gemini", null, "4 Hydrogen"), "https://gemini.google.com/app");
  assert.equal(modelId("aistudio", "4 Hydrogen"), "gemini-4-hydrogen");
  assert.equal(modelLabel("aistudio", "4 Hydrogen"), "Gemini 4 Hydrogen (gemini-4-hydrogen)");
});

test("URL inference and status expose the verified AI Studio contract", () => {
  assert.equal(inferProviderFromUrl("https://aistudio.google.com/prompts/abc"), "aistudio");
  assert.equal(inferProviderFromUrl("https://gemini.google.com/app"), "gemini");
  assert.equal(inferProviderFromUrl("https://example.com"), null);
  const status = providerStatuses();
  assert.deepEqual(status.aistudio.supportedModels, ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]);
  assert.deepEqual(status.gemini.supportedModels, ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]);
  assert.equal(status.aistudio.billingMode, "subscription");
  assert.deepEqual(status.aistudio.attachments, { enabled: true });
  assert.deepEqual(status.gemini.attachments, { enabled: true });
  assert.equal(hasVerifiedUpload("aistudio"), true);
  assert.equal(hasVerifiedUpload("gemini"), true);
  assert.equal(providerConfig("aistudio").home, "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon");
});
