// Self-contained: serialized and evaluated in the dedicated task page.
export async function ensureGeminiModel(continuation = false, model = "4 Argon") {
  const SUPPORTED_MODELS = ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"];
  if (!SUPPORTED_MODELS.includes(model)) return { ok: false, code: "INVALID_INPUT", message: "Unsupported Gemini Web model." };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => element?.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden";
  const label = element => `${element.getAttribute?.("aria-label") ?? ""} ${element.innerText ?? ""}`.trim();
  const matches = text => model === "4 Argon" ? /\bArgon\b/i.test(text) :
    model === "4 Carbon" ? /\bCarbon\b/i.test(text) :
    model === "4 Neon" ? /\bNeon\b/i.test(text) :
    model === "4 Hydrogen" ? /\bHydrogen\b/i.test(text) : /\b3\.8\s+Flash\b(?![-\s]*Lite)/i.test(text);
  const shortFlash = text => model === "3.8 Flash" && /\bFlash\b(?![-\s]*Lite)/i.test(text);
  const modelOptions = () => {
    const candidates = Array.from(document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], [role="option"], button, span, li'));
    const semantic = candidates.filter(element => visible(element) && ["menuitem", "menuitemradio", "option"].includes(element.getAttribute?.("role")));
    return (semantic.length ? semantic : candidates).filter(visible);
  };
  const named = (element, name) => (element.innerText ?? "").split(/\r?\n/).some(line =>
    (name === "4 Argon" ? /^4\s*Argon(?:\s|$)/i :
      name === "4 Carbon" ? /^4\s*Carbon(?:\s|$)/i :
      name === "4 Neon" ? /^4\s*Neon(?:\s|$)/i :
      name === "4 Hydrogen" ? /^4\s*Hydrogen(?:\s|$)/i :
        /^3\.8\s+Flash(?![-\s]*Lite)(?:\s|$)/i).test(line.trim()));
  const option = () => modelOptions().find(element => named(element, model) && !element.disabled && element.getAttribute?.("aria-disabled") !== "true");
  // Disabled options still carry the account's quota reason and reset time.
  // Inspect them before filtering for clickable options; never infer quota from
  // a disabled control alone (it may require a subscription or be unavailable).
  const quotaFailure = () => {
    const exhausted = SUPPORTED_MODELS.flatMap(name => {
      const element = modelOptions().find(candidate => named(candidate, name));
      const text = element ? label(element) : "";
      const quota = /用量限额重置|(?:已|您已|你已)?达到.{0,40}(?:上限|限额)|(?:usage\s+)?limit\s+(?:resets?|reached)|(?:you(?:'ve| have)\s+)?reached\s+(?:your\s+|the\s+)?(?:usage\s+)?(?:limit|quota)|quota\s+(?:exhausted|resets?)/i.test(text);
      return quota ? [{ model: name, text: text.slice(0, 240) }] : [];
    });
    if (!exhausted.some(entry => entry.model === model)) return null;
    const all = exhausted.length === SUPPORTED_MODELS.length;
    return {
      ok: false, code: all ? "WEB_QUOTA_EXHAUSTED" : "RATE_LIMITED",
      message: `Gemini Web ${all ? SUPPORTED_MODELS.join(", ") + " have" : model + " has"} reached the usage limit. ${exhausted.map(entry => entry.text).join("; ")}`,
      quota: { scope: all ? "web" : "model", exhausted_models: exhausted.map(entry => entry.model), evidence: exhausted },
    };
  };
  const isSelected = target => target?.classList?.contains("selected") ||
    target?.getAttribute?.("aria-selected") === "true" || target?.getAttribute?.("aria-checked") === "true";
  const selector = () => Array.from(document.querySelectorAll('button, [role="button"], [aria-haspopup]'))
    .find(element => visible(element) && (
      element.getAttribute("data-test-id") === "bard-mode-menu-button" ||
      /模式选择|(?:mode|model) (?:selector|picker|menu)|current (?:mode|model)|select.*model|choose.*model/i.test(label(element))
    ));
  // Gemini renders its editor before loading the model switcher's lazy chunks.
  // Proxied cold loads can take over a minute after the editor becomes ready.
  const discoveryDeadline = Date.now() + 120000;
  let control = selector();
  while (!control && Date.now() < discoveryDeadline) {
    await pause(100);
    control = selector();
  }
  if (!control) return { ok: false, code: "MODEL_UNAVAILABLE", message: "Gemini model selector did not finish loading within 120 seconds; prompt was not submitted." };
  if (matches(label(control))) return { ok: true, model };
  const wrongContinuation = continuation && !shortFlash(label(control));
  control.click();
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const limited = quotaFailure();
    if (limited) { control.click(); return limited; }
    const target = option();
    if (wrongContinuation && modelOptions().length) {
      control.click();
      return { ok: false, code: "MODEL_MISMATCH", message: `This conversation is not in ${model} mode. Start a new conversation explicitly to change models.` };
    }
    if (target) {
      if (continuation) {
        const selected = isSelected(target);
        control.click();
        return selected ? { ok: true, model } : { ok: false, code: "MODEL_MISMATCH", message: `This conversation is not in ${model} mode. Start a fresh conversation to change models.` };
      }
      target.click();
      for (let i = 0; i < 20; i++) {
        if (matches(label(selector() ?? control))) return { ok: true, model };
        if (shortFlash(label(selector() ?? control))) {
          const picker = selector() ?? control;
          picker.click();
          for (let check = 0; check < 20; check++) {
            if (isSelected(option())) { picker.click(); return { ok: true, model }; }
            await pause(100);
          }
          picker.click();
          break;
        }
        await pause(100);
      }
      break;
    }
    await pause(100);
  }
  return { ok: false, code: "MODEL_UNAVAILABLE", message: `${model} could not be selected and verified; prompt was not submitted.` };
}

export { ensureGeminiModel as ensureGeminiArgon };
