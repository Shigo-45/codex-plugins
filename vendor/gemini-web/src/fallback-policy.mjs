// The unified writing worker owns dispatch; these receipts describe its next
// eligible route after a confirmed browser failure.
const MODELS = ["4 Argon", "4 Carbon", "4 Neon", "3.8 Flash"];
const effort = model => model === "4 Neon" ? "xhigh" : "high";
const label = value => MODELS.find(model => value === model || value === `gemini-${model.toLowerCase().replaceAll(" ", "-")}`);
export function fallbackFor(error) {
  if (!["RATE_LIMITED", "WEB_QUOTA_EXHAUSTED"].includes(error?.code)) return null;
  const outcome = error.recovery?.outcome;
  const settled = error.phase === "PRE_SUBMIT" || ["FAILED", "CANCELLED", "UPLOAD_ABANDONED"].includes(outcome);
  if (!settled || outcome === "COMPLETED") return null;
  const exhausted = (error.quota?.exhausted_models ?? []).map(label);
  const current = label(error.model) ?? exhausted.find(Boolean);
  const candidates = current ? MODELS.slice(MODELS.indexOf(current) + 1) : MODELS;
  const next = error.code === "WEB_QUOTA_EXHAUSTED" ? null : candidates.find(model => !exhausted.includes(model));
  const aistudio = error.provider === "aistudio";
  if (!next && !aistudio) return { action: "routes_exhausted", channel: null, model: null, reason: "web_quota_exhausted" };
  const model = next ?? MODELS[0];
  return {
    action: "continue_authorized_workflow", channel: aistudio && next ? "aistudio" : "web",
    model, reasoning_effort: effort(model),
    reason: next ? "requested_model_rate_limited" : "aistudio_quota_exhausted",
  };
}
