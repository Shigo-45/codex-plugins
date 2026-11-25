import { chmod, mkdir, open, readFile, rename } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

import { withFileLock } from "./operation-lock.mjs";
import {
  canonicalModel,
  DEFAULT_PROVIDER,
  inferProviderFromUrl,
  isConversationUrl,
  normalizeProvider,
  PROVIDER_IDS,
} from "./providers.mjs";

const ROOT = process.env.GEMINI_WEB_BRIDGE_HOME ?? join(
  homedir(),
  "Library",
  "Application Support",
  "Codex UI Extensions",
  "Gemini Web Bridge",
);
const CONVERSATION_SCHEMA_VERSION = 2;

export const paths = {
  browserLock: join(ROOT, "locks", "browser"),
  conversations: join(ROOT, "conversations.json"),
  legacySessions: join(ROOT, "sessions.json"),
  profile: join(ROOT, "Chrome Profile"),
  root: ROOT,
  settings: join(ROOT, "settings.json"),
  stateLock: join(ROOT, "locks", "state"),
};

async function ensureRoot() {
  await mkdir(ROOT, { recursive: true, mode: 0o700 });
  await mkdir(paths.profile, { recursive: true, mode: 0o700 });
  await chmod(ROOT, 0o700).catch(() => {});
}

async function readJson(path, fallback) {
  await ensureRoot();
  let source;
  try {
    source = await readFile(path, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    throw error;
  }
  try {
    return JSON.parse(source);
  } catch {
    const backup = `${path}.corrupt-${Date.now()}`;
    await rename(path, backup).catch(() => {});
    await chmod(backup, 0o600).catch(() => {});
    return fallback;
  }
}

async function writeJson(path, value) {
  await ensureRoot();
  const temporary = `${path}.tmp-${process.pid}-${crypto.randomUUID()}`;
  const file = await open(temporary, "w", 0o600);
  try {
    await file.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
    await file.sync();
  } finally {
    await file.close();
  }
  await rename(temporary, path);
  await chmod(path, 0o600).catch(() => {});
}

function emptyConversationState() {
  return { conversations: {}, schemaVersion: CONVERSATION_SCHEMA_VERSION };
}

function validConversationUrl(provider, value) {
  return typeof value === "string" && isConversationUrl(provider, value);
}

function invalidProviderError(conversationId, provider) {
  return Object.assign(
    new Error(`Conversation ${conversationId ?? "(unknown)"} has an invalid provider ${JSON.stringify(provider)}; the record was preserved. Use "gemini" or "aistudio".`),
    { code: "INVALID_INPUT" },
  );
}

function assertStoredProvider(conversationId, value) {
  if (value?.provider != null && !PROVIDER_IDS.includes(value.provider)) {
    throw invalidProviderError(conversationId, value.provider);
  }
}

function normalizeConversation(value) {
  if (value == null) return null;
  const provider = normalizeProvider(value.provider ?? DEFAULT_PROVIDER);
  if (!validConversationUrl(provider, value.conversationUrl)) return null;
  let model = null;
  if (value.model != null) {
    try { model = canonicalModel(provider, value.model); }
    catch { model = null; }
  }
  return {
    conversationUrl: value.conversationUrl,
    createdAt: typeof value.createdAt === "string" ? value.createdAt : new Date().toISOString(),
    lastUsedAt: typeof value.lastUsedAt === "string" ? value.lastUsedAt : new Date().toISOString(),
    legacyVideoId: typeof value.legacyVideoId === "string" ? value.legacyVideoId : null,
    ownerThreadId: typeof value.ownerThreadId === "string" ? value.ownerThreadId : null,
    model,
    provider,
  };
}

async function readConversationStateUnlocked() {
  const stored = await readJson(paths.conversations, null);
  const state = emptyConversationState();
  if (stored?.schemaVersion === CONVERSATION_SCHEMA_VERSION && stored.conversations != null) {
    for (const [id, value] of Object.entries(stored.conversations)) {
      if (value?.provider != null && !PROVIDER_IDS.includes(value.provider)) {
        // Preserve malformed records byte-for-byte so an operator can inspect
        // or repair them; do not silently drop them during unrelated writes.
        state.conversations[id] = value;
        continue;
      }
      const normalized = normalizeConversation(value);
      if (normalized != null) state.conversations[id] = normalized;
    }
    if (stored.migration != null) state.migration = stored.migration;
  }
  return state;
}

async function ensureConversationStateUnlocked() {
  const state = await readConversationStateUnlocked();
  if (state.migration?.legacySessionsImportedAt != null) return state;

  const legacy = await readJson(paths.legacySessions, {});
  let imported = 0;
  for (const [videoId, value] of Object.entries(legacy ?? {})) {
    if (!validConversationUrl("gemini", value?.conversationUrl)) continue;
    const id = `conv_${crypto.randomUUID()}`;
    const timestamp = typeof value.lastUsedAt === "string" ? value.lastUsedAt : new Date().toISOString();
    state.conversations[id] = {
      conversationUrl: value.conversationUrl,
      createdAt: timestamp,
      lastUsedAt: timestamp,
      legacyVideoId: videoId,
      ownerThreadId: null,
      provider: "gemini",
    };
    imported += 1;
  }
  state.migration = {
    importedLegacySessions: imported,
    legacySessionsImportedAt: new Date().toISOString(),
  };
  await writeJson(paths.conversations, state);
  return state;
}

async function mutateConversations(mutator, signal) {
  return withFileLock(
    paths.stateLock,
    { label: "Gemini Web Bridge state", signal, staleAfterMs: 60_000, timeoutMs: 30_000 },
    async () => {
      const state = await ensureConversationStateUnlocked();
      const result = await mutator(state);
      await writeJson(paths.conversations, state);
      return result;
    },
  );
}

export async function authorizationStatus() {
  const settings = await readJson(paths.settings, {});
  return {
    authorized: settings.authorized === true,
    authorizedAt: typeof settings.authorizedAt === "string" ? settings.authorizedAt : null,
  };
}

export async function authorize(signal) {
  return withFileLock(
    paths.stateLock,
    { label: "Gemini Web Bridge state", signal, staleAfterMs: 60_000, timeoutMs: 30_000 },
    async () => {
      const settings = await readJson(paths.settings, {});
      const value = { ...settings, authorized: true, authorizedAt: new Date().toISOString() };
      await writeJson(paths.settings, value);
      return { authorized: true, authorizedAt: value.authorizedAt };
    },
  );
}

export async function getConversation(conversationId) {
  const state = await withFileLock(
    paths.stateLock,
    { label: "Gemini Web Bridge state", staleAfterMs: 60_000, timeoutMs: 30_000 },
    ensureConversationStateUnlocked,
  );
  const value = state.conversations[conversationId];
  assertStoredProvider(conversationId, value);
  return normalizeConversation(value);
}

export async function listConversations({ ownerThreadId = null, scope = "current" } = {}) {
  const state = await withFileLock(
    paths.stateLock,
    { label: "Gemini Web Bridge state", staleAfterMs: 60_000, timeoutMs: 30_000 },
    ensureConversationStateUnlocked,
  );
  for (const [conversationId, value] of Object.entries(state.conversations)) {
    assertStoredProvider(conversationId, value);
  }
  return Object.entries(state.conversations)
    .filter(([, value]) => scope === "all" || value.ownerThreadId === ownerThreadId)
    .map(([conversationId, value]) => ({
      conversationId,
      createdAt: value.createdAt,
      lastUsedAt: value.lastUsedAt,
      legacy: value.legacyVideoId != null,
      ownerThreadId: value.ownerThreadId,
      provider: value.provider ?? DEFAULT_PROVIDER,
    }))
    .sort((left, right) => right.lastUsedAt.localeCompare(left.lastUsedAt));
}

export async function saveConversation(
  { conversationId = null, conversationUrl, legacyVideoId = null, ownerThreadId = null, model = null, provider = null },
  signal,
) {
  const resolvedProvider = normalizeProvider(provider ?? inferProviderFromUrl(conversationUrl) ?? DEFAULT_PROVIDER);
  if (!validConversationUrl(resolvedProvider, conversationUrl)) {
    throw new Error(`Invalid ${resolvedProvider} conversation URL.`);
  }
  const resolvedModel = model == null ? null : canonicalModel(resolvedProvider, model);
  return mutateConversations((state) => {
    const now = new Date().toISOString();
    const id = conversationId ?? `conv_${crypto.randomUUID()}`;
    assertStoredProvider(id, state.conversations[id]);
    const existing = normalizeConversation(state.conversations[id]);
    if (existing != null && existing.provider !== resolvedProvider) {
      throw Object.assign(new Error("A conversation cannot switch Web providers."), { code: "PROVIDER_MISMATCH" });
    }
    state.conversations[id] = {
      conversationUrl,
      createdAt: existing?.createdAt ?? now,
      lastUsedAt: now,
      legacyVideoId: legacyVideoId ?? existing?.legacyVideoId ?? null,
      ownerThreadId: ownerThreadId ?? existing?.ownerThreadId ?? null,
      model: resolvedModel ?? existing?.model ?? null,
      provider: resolvedProvider,
    };
    return id;
  }, signal);
}

export async function findLegacyConversation(videoId) {
  const state = await withFileLock(
    paths.stateLock,
    { label: "Gemini Web Bridge state", staleAfterMs: 60_000, timeoutMs: 30_000 },
    ensureConversationStateUnlocked,
  );
  const matched = Object.entries(state.conversations)
    .filter(([, value]) => value.legacyVideoId === videoId)
    .sort(([, left], [, right]) => right.lastUsedAt.localeCompare(left.lastUsedAt))[0];
  if (matched == null) return null;
  assertStoredProvider(matched[0], matched[1]);
  return { conversationId: matched[0], ...normalizeConversation(matched[1]) };
}

export { CONVERSATION_SCHEMA_VERSION };
