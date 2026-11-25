export const DEFAULT_PROVIDER = "gemini";

// Human-facing Web labels paired with the exact model IDs reported by Google.
export const MODEL_IDS = Object.freeze({
  "4 Argon": "gemini-4-argon",
  "4 Carbon": "gemini-4-carbon",
  "4 Neon": "gemini-4-neon",
  "4 Hydrogen": "gemini-4-hydrogen",
  "3.8 Flash": "gemini-3.8-flash",
});
export const SUPPORTED_MODELS = Object.freeze(Object.keys(MODEL_IDS));

export const PROVIDERS = Object.freeze({
  gemini: Object.freeze({
    billingMode: "gemini-web-account",
    defaultModel: "4 Argon",
    home: "https://gemini.google.com/app",
    id: "gemini",
    label: "Gemini Web",
    models: SUPPORTED_MODELS,
    origin: "https://gemini.google.com",
  }),
  aistudio: Object.freeze({
    billingMode: "subscription",
    defaultModel: "4 Argon",
    home: "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon",
    id: "aistudio",
    label: "AI Studio Web",
    modelId: "gemini-4-argon",
    models: SUPPORTED_MODELS,
    origin: "https://aistudio.google.com",
  }),
});

export const PROVIDER_IDS = Object.freeze(Object.keys(PROVIDERS));

const invalid = (message) => Object.assign(new Error(message), { code: "INVALID_INPUT" });

export function normalizeProvider(value) {
  if (value == null) return DEFAULT_PROVIDER;
  if (typeof value === "string" && value.length > 0 && PROVIDER_IDS.includes(value)) return value;
  throw invalid("provider must be \"gemini\" or \"aistudio\".");
}

export function providerConfig(value) {
  return PROVIDERS[normalizeProvider(value)];
}

export function isAistudioModel(value) {
  return SUPPORTED_MODELS.includes(value) || Object.values(MODEL_IDS).includes(value);
}

export function canonicalModel(provider, value) {
  const id = normalizeProvider(provider);
  const config = PROVIDERS[id];
  if (value == null || value === "") return config.defaultModel;
  if (config.models.includes(value)) return value;
  if (id === "aistudio") {
    const human = Object.keys(MODEL_IDS).find((label) => MODEL_IDS[label] === value);
    if (human) return human;
  }
  throw invalid(`Unsupported ${config.label} model; choose ${config.models.join(", ")}; prompt was not submitted.`);
}

export function modelId(provider, model) {
  const id = normalizeProvider(provider);
  const canonical = canonicalModel(id, model);
  if (id === "aistudio") return MODEL_IDS[canonical];
  return null;
}

export function modelLabel(provider, model) {
  const id = normalizeProvider(provider);
  const canonical = canonicalModel(id, model);
  if (id === "aistudio") return `Gemini ${canonical} (${MODEL_IDS[canonical]})`;
  return canonical;
}

export function modelHome(provider, model) {
  const id = normalizeProvider(provider);
  const config = PROVIDERS[id];
  const canonical = canonicalModel(id, model);
  if (id !== "aistudio") return config.home;
  const base = config.home.split("?")[0];
  return `${base}?model=${encodeURIComponent(MODEL_IDS[canonical])}`;
}

export function isHomeUrl(provider, value) {
  let url;
  try { url = new URL(value); } catch { return false; }
  const id = normalizeProvider(provider);
  if (url.origin !== PROVIDERS[id].origin) return false;
  const path = url.pathname.replace(/\/$/, "");
  return id === "gemini" ? path === "/app" : path === "/prompts/new_chat";
}

export function isConversationUrl(provider, value) {
  let url;
  try { url = new URL(value); } catch { return false; }
  const id = normalizeProvider(provider);
  if (url.origin !== PROVIDERS[id].origin) return false;
  if (id === "gemini") return /^\/app\/[^/?#]+/.test(url.pathname);
  // Live evidence only confirms /prompts/<id>. The new-chat landing route is
  // deliberately not a resumable conversation, and /app/prompts is unverified.
  return /^\/prompts\/(?!new_chat(?:\/|$))[^/?#]+/.test(url.pathname);
}

export function isProviderUrl(provider, value) {
  return isHomeUrl(provider, value) || isConversationUrl(provider, value);
}

export function inferProviderFromUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.origin === PROVIDERS.gemini.origin) return "gemini";
  if (url.origin === PROVIDERS.aistudio.origin) return "aistudio";
  return null;
}

export function conversationDestination(provider, conversationUrl = null, model = null) {
  if (conversationUrl != null) return conversationUrl;
  return model == null ? providerConfig(provider).home : modelHome(provider, model);
}

export function providerStatuses() {
  return Object.fromEntries(PROVIDER_IDS.map((id) => {
    const config = PROVIDERS[id];
    return [id, {
      attachments: { enabled: true },
      billingMode: config.billingMode,
      defaultModel: config.defaultModel,
      home: config.home,
      label: config.label,
      supportedModels: [...config.models],
    }];
  }));
}
