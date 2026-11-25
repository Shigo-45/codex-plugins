// AI Studio page automation. Every exported function is serialized and run in
// the page, so each one defines its own constants/selectors and must never rely
// on module-scope values.
const AISTUDIO_MODEL_ID = "gemini-4-argon";
const AISTUDIO_HUMAN_MODEL = "4 Argon";
const AISTUDIO_MODEL_IDS = Object.freeze({
  "4 Argon": "gemini-4-argon",
  "4 Carbon": "gemini-4-carbon", "4 Neon": "gemini-4-neon", "4 Hydrogen": "gemini-4-hydrogen",
  "3.8 Flash": "gemini-3.8-flash",
});

export function inspectAistudioPage() {
  const MODEL_LABELS = { "gemini-4-argon": "4 Argon", "gemini-4-carbon": "4 Carbon", "gemini-4-neon": "4 Neon", "gemini-4-hydrogen": "4 Hydrogen", "gemini-3.8-flash": "3.8 Flash" };
  const visible = (element) => element != null &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const rawText = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
  const composer = Array.from(document.querySelectorAll(
    'textarea[formcontrolname="promptText"], textarea[aria-label="Enter a prompt"]',
  )).find(visible) ?? null;
  const model = Array.from(document.querySelectorAll(
    'button.model-selector-card span[data-test-id="model-name"]',
  )).find(visible) ?? null;
  const account = Array.from(document.querySelectorAll("button.account-switcher-button")).find(visible) ?? null;
  const pro = account != null && rawText(account).split(/\r?\n/).some((line) => line.trim() === "PRO");
  const apiKey = Array.from(document.querySelectorAll("button.paid-api-key-button")).find(visible) ?? null;
  const noApiKey = apiKey != null &&
    (apiKey.getAttribute("aria-label") ?? "").trim() === "No API key selected" &&
    rawText(apiKey) === "key_off";
  const runLabel = Array.from(document.querySelectorAll("span.run-button-label"))
    .find((element) => visible(element) && rawText(element) === "Run") ?? null;
  const run = runLabel?.closest("button") ?? null;
  const signInVisible = Array.from(document.querySelectorAll("a, button")).some((element) => {
    const value = (element.innerText ?? element.textContent ?? "").trim();
    return /^(sign in|登录|登入)$/i.test(value) && visible(element);
  });
  const currentModel = model == null ? null : rawText(model).slice(0, 80);
  return {
    composerReady: composer != null,
    modalVisible: Array.from(document.querySelectorAll("mat-dialog-container")).some(visible),
    model: currentModel != null && MODEL_LABELS[currentModel] != null ? MODEL_LABELS[currentModel] : null,
    modelId: currentModel != null && MODEL_LABELS[currentModel] != null ? currentModel : null,
    modelText: currentModel,
    noApiKeyVisible: noApiKey,
    proVisible: pro,
    provider: "aistudio",
    runReady: run != null && !run.disabled && run.getAttribute?.("aria-disabled") !== "true",
    signedOut: signInVisible,
    title: document.title,
    url: location.href,
  };
}

export async function ensureAistudioModelAndBilling(continuation, model = "4 Argon") {
  const MODEL_IDS = { "4 Argon": "gemini-4-argon", "4 Carbon": "gemini-4-carbon", "4 Neon": "gemini-4-neon", "4 Hydrogen": "gemini-4-hydrogen", "3.8 Flash": "gemini-3.8-flash" };
  const MODEL_LABELS = { "gemini-4-argon": "4 Argon", "gemini-4-carbon": "4 Carbon", "gemini-4-neon": "4 Neon", "gemini-4-hydrogen": "4 Hydrogen", "gemini-3.8-flash": "3.8 Flash" };
  const selected = MODEL_IDS[model] ? { human: model, id: MODEL_IDS[model] } :
    MODEL_LABELS[model] ? { human: MODEL_LABELS[model], id: model } : null;
  if (selected == null) {
    return {
      ok: false,
      code: "MODEL_UNAVAILABLE",
      message: "AI Studio Web supports only 4 Argon, 4 Hydrogen, or 3.8 Flash (verified as gemini-4-argon, gemini-4-hydrogen, or gemini-3.8-flash); prompt was not submitted.",
    };
  }
  const MODEL_ID = selected.id;
  const HUMAN_MODEL = selected.human;
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => element != null &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const rawText = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
  const modelName = () => {
    const span = Array.from(document.querySelectorAll(
      'button.model-selector-card span[data-test-id="model-name"]',
    )).find(visible);
    return span == null ? null : rawText(span);
  };
  const proVisible = () => {
    const account = Array.from(document.querySelectorAll("button.account-switcher-button")).find(visible);
    if (account == null) return false;
    return rawText(account).split(/\r?\n/).some((line) => line.trim() === "PRO");
  };
  const apiKeyState = () => {
    const button = Array.from(document.querySelectorAll("button.paid-api-key-button")).find(visible);
    if (button == null) return "missing";
    const aria = (button.getAttribute("aria-label") ?? "").trim();
    const value = rawText(button);
    if (aria === "No API key selected" && value === "key_off") return "no_api_key";
    const combined = `${aria} ${value}`;
    if (!/no api key selected/i.test(combined) &&
        /(?:api key (?:is )?(?:selected|active)|using (?:an? )?(?:paid )?api key|paid api key)/i.test(combined)) {
      return "paid_key";
    }
    return "unknown";
  };
  const modalVisible = () => Array.from(document.querySelectorAll("mat-dialog-container")).some(visible);
  const billing = (pro, keyState) => ({
    apiKeySelected: keyState === "paid_key",
    evidence: [
      ...(pro ? [{ kind: "pro_badge", text: "PRO" }] : []),
      ...(keyState === "no_api_key" ? [{ kind: "api_key", text: "No API key selected" }] : []),
      ...(modelName() === MODEL_ID ? [{ kind: "model", text: MODEL_ID }] : []),
    ],
    mode: keyState === "no_api_key" && pro ? "subscription" : keyState === "paid_key" ? "paid_api_key" : "unverified",
    pro,
  });

  // This first-party cookie notice can cover Run in a fresh dedicated profile.
  // Acknowledge only its known informational control; account/terms dialogs
  // remain subject to the modal guard below.
  const notice = Array.from(document.querySelectorAll(".glue-cookie-notification-bar.visible"))
    .find((element) => visible(element) && /Google AI Studio uses cookies from Google/.test(rawText(element)));
  const acknowledge = notice?.querySelector("button.glue-cookie-notification-bar__accept");
  if (acknowledge != null && rawText(acknowledge) === "OK, got it") {
    acknowledge.click();
    await pause(150);
    if (visible(notice)) {
      return { ok: false, code: "INTERACTION_REQUIRED", message: "AI Studio's cookie notice is covering the page. Dismiss it in the dedicated browser before submitting." };
    }
  }

  const deadline = Date.now() + 60000;
  let lastModel = null;
  while (Date.now() < deadline) {
    if (modalVisible()) {
      return {
        ok: false,
        code: "INTERACTION_REQUIRED",
        message: "AI Studio is showing a visible dialog (for example welcome or terms). Dismiss it manually before submitting; no dialog was accepted automatically.",
      };
    }
    const keyState = apiKeyState();
    if (keyState === "paid_key") {
      return {
        ok: false,
        code: "PAID_KEY_ACTIVE",
        message: "AI Studio is using an API key. Paid-key billing is not supported by this Web provider; prompt was not submitted.",
        billing: billing(proVisible(), keyState),
      };
    }
    if (keyState === "unknown") {
      return {
        ok: false,
        code: "BILLING_UNVERIFIED",
        message: "AI Studio's paid-api-key button is in an unrecognized state. Expected aria-label \"No API key selected\" and innerText key_off; prompt was not submitted.",
        billing: billing(proVisible(), keyState),
      };
    }
    lastModel = modelName();
    if (lastModel != null && lastModel !== MODEL_ID) {
      return {
        ok: false,
        code: "MODEL_MISMATCH",
        message: `AI Studio model-name is "${lastModel.slice(0, 80)}", not the verified ${MODEL_ID}; prompt was not submitted.`,
        billing: billing(proVisible(), keyState),
      };
    }
    const pro = proVisible();
    if (lastModel === MODEL_ID && pro && keyState === "no_api_key") {
      const verifiedBilling = billing(pro, keyState);
      return {
        billing: verifiedBilling,
        billing_mode: "subscription",
        evidence: verifiedBilling.evidence,
        model: HUMAN_MODEL,
        model_id: MODEL_ID,
        ok: true,
      };
    }
    await pause(200);
  }
  if (lastModel == null) {
    return {
      ok: false,
      code: "MODEL_UNAVAILABLE",
      message: `AI Studio did not show button.model-selector-card span[data-test-id="model-name"] with ${MODEL_ID}; prompt was not submitted.`,
    };
  }
  return {
    ok: false,
    code: "BILLING_UNVERIFIED",
    message: "AI Studio did not show both PRO on button.account-switcher-button and button.paid-api-key-button aria-label \"No API key selected\" / innerText key_off. Billing is unverified; prompt was not submitted.",
    billing: billing(proVisible(), apiKeyState()),
  };
}

export function prepareAistudioInput() {
  const visible = element => element != null && element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  if (Array.from(document.querySelectorAll("mat-dialog-container")).some(visible)) {
    return { ok: false, code: "INTERACTION_REQUIRED", message: "Dismiss the AI Studio dialog before entering a prompt." };
  }
  const input = Array.from(document.querySelectorAll(
    'textarea[formcontrolname="promptText"], textarea[aria-label="Enter a prompt"]',
  )).find(visible);
  if (input == null || input.disabled || input.readOnly) {
    return { ok: false, code: "UI_CHANGED", message: "AI Studio prompt input is unavailable." };
  }
  input.focus();
  input.select();
  return { ok: document.activeElement === input };
}

export async function submitAistudioPrompt(prompt, expectedModel = "4 Argon", nativeInput = false) {
  const MODEL_IDS = { "4 Argon": "gemini-4-argon", "4 Carbon": "gemini-4-carbon", "4 Neon": "gemini-4-neon", "4 Hydrogen": "gemini-4-hydrogen", "3.8 Flash": "gemini-3.8-flash" };
  const MODEL_LABELS = { "gemini-4-argon": "4 Argon", "gemini-4-carbon": "4 Carbon", "gemini-4-neon": "4 Neon", "gemini-4-hydrogen": "4 Hydrogen", "gemini-3.8-flash": "3.8 Flash" };
  const selected = MODEL_IDS[expectedModel] ? { human: expectedModel, id: MODEL_IDS[expectedModel] } :
    MODEL_LABELS[expectedModel] ? { human: MODEL_LABELS[expectedModel], id: expectedModel } : null;
  if (selected == null) {
    return {
      ok: false,
      clicked: false,
      code: "MODEL_UNAVAILABLE",
      message: "AI Studio Web supports only 4 Argon, 4 Hydrogen, or 3.8 Flash (verified as gemini-4-argon, gemini-4-hydrogen, or gemini-3.8-flash); prompt was not submitted.",
    };
  }
  const MODEL_ID = selected.id;
  const HUMAN_MODEL = selected.human;
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => element != null &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const rawText = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
  const modelName = () => {
    const span = Array.from(document.querySelectorAll(
      'button.model-selector-card span[data-test-id="model-name"]',
    )).find(visible);
    return span == null ? null : rawText(span);
  };
  const proVisible = () => {
    const account = Array.from(document.querySelectorAll("button.account-switcher-button")).find(visible);
    if (account == null) return false;
    return rawText(account).split(/\r?\n/).some((line) => line.trim() === "PRO");
  };
  const apiKeyState = () => {
    const button = Array.from(document.querySelectorAll("button.paid-api-key-button")).find(visible);
    if (button == null) return "missing";
    const aria = (button.getAttribute("aria-label") ?? "").trim();
    const value = rawText(button);
    if (aria === "No API key selected" && value === "key_off") return "no_api_key";
    const combined = `${aria} ${value}`;
    if (!/no api key selected/i.test(combined) &&
        /(?:api key (?:is )?(?:selected|active)|using (?:an? )?(?:paid )?api key|paid api key)/i.test(combined)) {
      return "paid_key";
    }
    return "unknown";
  };
  const modalVisible = () => Array.from(document.querySelectorAll("mat-dialog-container")).some(visible);
  const runLabel = (text) => Array.from(document.querySelectorAll("span.run-button-label"))
    .find((element) => visible(element) && rawText(element) === text) ?? null;
  const runButton = () => {
    const label = runLabel("Run");
    return label == null ? null : label.closest("button");
  };
  const stopVisible = () => runLabel("Stop") != null;
  const modelTurns = () => {
    const containers = Array.from(document.querySelectorAll(
      'ms-chat-turn .chat-turn-container.model, ms-chat-turn [data-turn-role="Model"]',
    ));
    const turns = [];
    const seen = new Set();
    for (const container of containers) {
      const turn = container.closest?.("ms-chat-turn");
      if (turn == null || seen.has(turn) || !visible(turn)) continue;
      seen.add(turn);
      turns.push(turn);
    }
    return turns;
  };
  const turnId = (turn, index) => turn.getAttribute?.("data-turn-id") ?? turn.id ?? String(index);
  const inThoughts = (element) => element.closest?.(
    '[data-turn-role="Thoughts"], [data-turn-role="thoughts"], .thoughts, ms-thoughts, [data-test-id*="thought"], [data-testid*="thought"]',
  ) != null;
  const topLevel = (scope, selector) => Array.from(scope.querySelectorAll(selector))
    .filter((element) => visible(element) && !inThoughts(element) &&
      element.parentElement?.closest?.(selector) == null);
  const turnText = (turn) => {
    if (turn == null) return "";
    const chunks = Array.from(turn.querySelectorAll("ms-text-chunk"))
      .filter((element) => visible(element) && !inThoughts(element));
    const pieces = [];
    for (const chunk of chunks) {
      const cmark = topLevel(chunk, "ms-cmark-node.cmark-node");
      if (cmark.length > 0) {
        const value = cmark.map(rawText).filter(Boolean).join("\n");
        if (value) pieces.push(value);
        continue;
      }
      const large = topLevel(chunk, ".very-large-text-container");
      if (large.length > 0) {
        const value = large.map(rawText).filter(Boolean).join("\n");
        if (value) pieces.push(value);
        continue;
      }
      const value = rawText(chunk);
      if (value) pieces.push(value);
    }
    return pieces.join("\n\n");
  };
  const baseline = () => {
    const turns = modelTurns();
    const latest = turns.at(-1) ?? null;
    return {
      count: turns.length,
      latestTurnId: latest == null ? null : turnId(latest, turns.length - 1),
      text: turnText(latest),
    };
  };
  const composer = () => Array.from(document.querySelectorAll(
    'textarea[formcontrolname="promptText"], textarea[aria-label="Enter a prompt"]',
  )).find(visible) ?? null;

  if (modalVisible()) {
    return {
      ok: false,
      clicked: false,
      code: "INTERACTION_REQUIRED",
      message: "A visible mat-dialog-container is blocking AI Studio before typing. Dismiss it manually; no dialog was accepted automatically.",
    };
  }
  const input = composer();
  if (input == null) {
    return {
      ok: false,
      clicked: false,
      code: "UI_CHANGED",
      message: "AI Studio prompt textarea (formcontrolname=promptText / aria-label \"Enter a prompt\") was not found; prompt was not submitted.",
    };
  }
  if (nativeInput) {
    if (input.value !== prompt) return { ok: false, clicked: false, code: "UI_CHANGED",
      message: "AI Studio input differs from the selected prompt; submission stopped." };
  } else {
    input.focus();
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value");
    if (descriptor?.set) descriptor.set.call(input, prompt);
    else input.value = prompt;
    input.dispatchEvent(new InputEvent("input", { bubbles: true, data: prompt, inputType: "insertText" }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  await pause(300);

  let run = null;
  const readyDeadline = Date.now() + 12000;
  while (Date.now() < readyDeadline) {
    run = runButton();
    if (run != null && !run.disabled && run.getAttribute?.("aria-disabled") !== "true") break;
    await pause(200);
  }
  if (run == null || run.disabled || run.getAttribute?.("aria-disabled") === "true") {
    return {
      ok: false,
      clicked: false,
      code: "UI_CHANGED",
      message: "AI Studio Run control (span.run-button-label exact \"Run\") was not enabled; prompt was not submitted.",
    };
  }

  // The mandatory last-moment guard. It re-reads the live DOM immediately
  // before the click and never trusts earlier observations.
  if (modalVisible()) {
    return {
      ok: false,
      clicked: false,
      code: "INTERACTION_REQUIRED",
      message: "A visible mat-dialog-container is blocking AI Studio submission. Dismiss it manually; no dialog was accepted automatically.",
    };
  }
  const finalKeyState = apiKeyState();
  const finalPro = proVisible();
  const finalModel = modelName();
  const finalBilling = {
    apiKeySelected: finalKeyState === "paid_key",
    evidence: [
      ...(finalPro ? [{ kind: "pro_badge", text: "PRO" }] : []),
      ...(finalKeyState === "no_api_key" ? [{ kind: "api_key", text: "No API key selected" }] : []),
      ...(finalModel === MODEL_ID ? [{ kind: "model", text: MODEL_ID }] : []),
    ],
    mode: finalKeyState === "no_api_key" && finalPro ? "subscription" : finalKeyState === "paid_key" ? "paid_api_key" : "unverified",
    pro: finalPro,
  };
  if (finalKeyState === "paid_key") {
    return {
      ok: false,
      clicked: false,
      code: "PAID_KEY_ACTIVE",
      message: "AI Studio is using an API key immediately before submission. Paid-key billing is not supported; prompt was not submitted.",
      billing: finalBilling,
    };
  }
  if (finalKeyState !== "no_api_key" || !finalPro) {
    return {
      ok: false,
      clicked: false,
      code: "BILLING_UNVERIFIED",
      message: "AI Studio billing was not verified immediately before submission. Expected PRO on button.account-switcher-button and \"No API key selected\" / key_off on button.paid-api-key-button.",
      billing: finalBilling,
    };
  }
  if (finalModel !== MODEL_ID) {
    return {
      ok: false,
      clicked: false,
      code: "MODEL_MISMATCH",
      message: `AI Studio model-name was not exactly ${MODEL_ID} immediately before submission; prompt was not submitted.`,
      billing: finalBilling,
    };
  }
  const currentRun = runButton();
  if (currentRun == null || currentRun.disabled || currentRun.getAttribute?.("aria-disabled") === "true") {
    return {
      ok: false,
      clicked: false,
      code: "UI_CHANGED",
      message: "AI Studio Run control changed immediately before submission; prompt was not submitted.",
      billing: finalBilling,
    };
  }

  if (nativeInput && composer()?.value !== prompt) {
    return { ok: false, clicked: false, code: "UI_CHANGED",
      message: "AI Studio prompt changed before the native click; submission stopped.", billing: finalBilling };
  }
  const before = baseline();
  if (nativeInput) {
    currentRun.scrollIntoView({ block: "center", inline: "center" });
    const rect = currentRun.getBoundingClientRect();
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
    if (!(rect.width > 0 && rect.height > 0) ||
        document.elementFromPoint(x, y)?.closest("button") !== currentRun) {
      return { ok: false, clicked: false, code: "INTERACTION_REQUIRED",
        message: "AI Studio Run control is covered; inspect the existing page.", billing: finalBilling };
    }
    return { ok: true, clicked: false, nativeClick: { x, y }, before, billing: finalBilling,
      billing_mode: "subscription", model: HUMAN_MODEL, model_id: MODEL_ID, url: location.href };
  }
  currentRun.click();
  const startDeadline = Date.now() + 12000;
  while (Date.now() < startDeadline) {
    const currentInput = composer();
    const editorText = (currentInput?.value ?? currentInput?.innerText ?? currentInput?.textContent ?? "").trim();
    const turns = modelTurns();
    const latest = turns.at(-1) ?? null;
    const latestTurnId = latest == null ? null : turnId(latest, turns.length - 1);
    if (
      editorText.length === 0 ||
      stopVisible() ||
      turns.length > before.count ||
      (before.latestTurnId != null && latestTurnId !== before.latestTurnId)
    ) {
      return {
        billing: finalBilling,
        billing_mode: "subscription",
        before,
        clicked: true,
        model: HUMAN_MODEL,
        model_id: MODEL_ID,
        ok: true,
        titleBefore: document.title,
        url: location.href,
      };
    }
    await pause(150);
  }
  return {
    ok: false,
    clicked: true,
    code: "OUTCOME_UNKNOWN",
    message: "AI Studio accepted the click but no new Model turn, Stop label, or cleared prompt became observable; the outcome is unknown.",
  };
}

export function readAistudioGenerationState(before, expectedModel = null) {
  const MODEL_IDS = { "4 Argon": "gemini-4-argon", "4 Carbon": "gemini-4-carbon", "4 Neon": "gemini-4-neon", "4 Hydrogen": "gemini-4-hydrogen", "3.8 Flash": "gemini-3.8-flash" };
  const MODEL_LABELS = { "gemini-4-argon": "4 Argon", "gemini-4-carbon": "4 Carbon", "gemini-4-neon": "4 Neon", "gemini-4-hydrogen": "4 Hydrogen", "gemini-3.8-flash": "3.8 Flash" };
  let requestedModel = expectedModel;
  if (requestedModel == null) {
    try { requestedModel = new URL(location.href).searchParams.get("model"); } catch {}
  }
  const selected = MODEL_IDS[requestedModel] ? { human: requestedModel, id: MODEL_IDS[requestedModel] } :
    MODEL_LABELS[requestedModel] ? { human: MODEL_LABELS[requestedModel], id: requestedModel } : null;
  const visible = (element) => element != null &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const rawText = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
  const modelName = () => {
    const span = Array.from(document.querySelectorAll(
      'button.model-selector-card span[data-test-id="model-name"]',
    )).find(visible);
    return span == null ? null : rawText(span);
  };
  const proVisible = () => {
    const account = Array.from(document.querySelectorAll("button.account-switcher-button")).find(visible);
    if (account == null) return false;
    return rawText(account).split(/\r?\n/).some((line) => line.trim() === "PRO");
  };
  const apiKeyState = () => {
    const button = Array.from(document.querySelectorAll("button.paid-api-key-button")).find(visible);
    if (button == null) return "missing";
    const aria = (button.getAttribute("aria-label") ?? "").trim();
    const value = rawText(button);
    if (aria === "No API key selected" && value === "key_off") return "no_api_key";
    const combined = `${aria} ${value}`;
    if (!/no api key selected/i.test(combined) &&
        /(?:api key (?:is )?(?:selected|active)|using (?:an? )?(?:paid )?api key|paid api key)/i.test(combined)) {
      return "paid_key";
    }
    return "unknown";
  };
  const modalVisible = () => Array.from(document.querySelectorAll("mat-dialog-container")).some(visible);
  const runLabel = (text) => Array.from(document.querySelectorAll("span.run-button-label"))
    .find((element) => visible(element) && rawText(element) === text) ?? null;
  const runButton = () => {
    const label = runLabel("Run");
    return label == null ? null : label.closest("button");
  };
  const stopButton = () => {
    const label = runLabel("Stop");
    return label == null ? null : label.closest("button");
  };
  const modelTurns = () => {
    const containers = Array.from(document.querySelectorAll(
      'ms-chat-turn .chat-turn-container.model, ms-chat-turn [data-turn-role="Model"]',
    ));
    const turns = [];
    const seen = new Set();
    for (const container of containers) {
      const turn = container.closest?.("ms-chat-turn");
      if (turn == null || seen.has(turn) || !visible(turn)) continue;
      seen.add(turn);
      turns.push(turn);
    }
    return turns;
  };
  const turnId = (turn, index) => turn.getAttribute?.("data-turn-id") ?? turn.id ?? String(index);
  const inThoughts = (element) => element.closest?.(
    '[data-turn-role="Thoughts"], [data-turn-role="thoughts"], .thoughts, ms-thoughts, [data-test-id*="thought"], [data-testid*="thought"]',
  ) != null;
  const topLevel = (scope, selector) => Array.from(scope.querySelectorAll(selector))
    .filter((element) => visible(element) && !inThoughts(element) &&
      element.parentElement?.closest?.(selector) == null);
  const turnText = (turn) => {
    if (turn == null) return "";
    const chunks = Array.from(turn.querySelectorAll("ms-text-chunk"))
      .filter((element) => visible(element) && !inThoughts(element));
    const pieces = [];
    for (const chunk of chunks) {
      const cmark = topLevel(chunk, "ms-cmark-node.cmark-node");
      if (cmark.length > 0) {
        const value = cmark.map(rawText).filter(Boolean).join("\n");
        if (value) pieces.push(value);
        continue;
      }
      const large = topLevel(chunk, ".very-large-text-container");
      if (large.length > 0) {
        const value = large.map(rawText).filter(Boolean).join("\n");
        if (value) pieces.push(value);
        continue;
      }
      const value = rawText(chunk);
      if (value) pieces.push(value);
    }
    return pieces.join("\n\n");
  };
  const turnFooter = (turn) => {
    if (turn == null) return null;
    return Array.from(turn.querySelectorAll(".turn-footer")).find(visible) ?? null;
  };
  const turnError = (turn) => {
    if (turn == null) return null;
    const errors = Array.from(turn.querySelectorAll(".model-error")).filter(visible);
    return errors.length === 0 ? null : rawText(errors.at(-1)).slice(0, 500);
  };

  const turns = modelTurns();
  const latest = turns.at(-1) ?? null;
  const latestTurnId = latest == null ? null : turnId(latest, turns.length - 1);
  const beforeCount = Number.isInteger(before?.count) ? before.count : 0;
  const beforeId = before?.latestTurnId ?? null;
  const hasBaseline = before != null && (Number.isInteger(before.count) || beforeId != null);
  const isNewTurn = latest != null && (
    !hasBaseline ||
    turns.length > beforeCount ||
    (beforeId != null && latestTurnId !== beforeId)
  );
  const text = isNewTurn ? turnText(latest) : "";
  const footer = turnFooter(latest);
  const stop = stopButton();
  const errorText = isNewTurn ? turnError(latest) : null;
  const keyState = apiKeyState();
  const pro = proVisible();
  const currentModel = modelName();
  const knownCurrent = currentModel != null && MODEL_LABELS[currentModel] != null;
  const MODEL_ID = selected?.id ?? (requestedModel == null && knownCurrent ? currentModel : null);
  const HUMAN_MODEL = selected?.human ?? (MODEL_ID != null ? MODEL_LABELS[MODEL_ID] : null);
  const billing = {
    apiKeySelected: keyState === "paid_key",
    evidence: [
      ...(pro ? [{ kind: "pro_badge", text: "PRO" }] : []),
      ...(keyState === "no_api_key" ? [{ kind: "api_key", text: "No API key selected" }] : []),
      ...(currentModel === MODEL_ID ? [{ kind: "model", text: MODEL_ID }] : []),
    ],
    mode: keyState === "no_api_key" && pro ? "subscription" : keyState === "paid_key" ? "paid_api_key" : "unverified",
    pro,
  };

  let failure = null;
  if (modalVisible()) {
    failure = { kind: "INTERACTION_REQUIRED", text: "A visible mat-dialog-container is blocking AI Studio." };
  } else if (keyState === "paid_key") {
    failure = { kind: "PAID_KEY_ACTIVE", text: "AI Studio is using an API key." };
  } else if (keyState !== "no_api_key") {
    failure = { kind: "BILLING_UNVERIFIED", text: "button.paid-api-key-button is not aria-label \"No API key selected\" / innerText key_off." };
  } else if (!pro) {
    failure = { kind: "BILLING_UNVERIFIED", text: "PRO was not found on button.account-switcher-button." };
  } else if (MODEL_ID == null && requestedModel != null) {
    failure = { kind: "MODEL_UNAVAILABLE", text: "The expected AI Studio model is not one of the verified model IDs." };
  } else if (MODEL_ID == null || currentModel == null) {
    failure = { kind: "MODEL_MISMATCH", text: "button.model-selector-card model-name was not found." };
  } else if (currentModel !== MODEL_ID) {
    failure = {
      kind: "MODEL_MISMATCH",
      text: currentModel == null ? "button.model-selector-card model-name was not found." : `span[data-test-id="model-name"] is not exactly ${MODEL_ID}.`,
    };
  } else if (errorText) {
    if (/权限遭拒|检查您的账号权限|permission denied|access denied|not have permission|PERMISSION_DENIED/i.test(errorText)) {
      failure = { kind: "PERMISSION_DENIED", text: errorText };
    } else if (/reached (?:your |the )?(?:usage )?(?:limit|quota)|too many requests|rate limit|(?:usage )?limit resets?|quota exhausted|用量限额重置|已达到.*(?:上限|限额)|请求过多/i.test(errorText)) {
      failure = { kind: "RATE_LIMITED", text: errorText };
    } else if (/model\s+(?:isn['’]t|is\s+not)\s+available\s+right\s+now/i.test(errorText)) {
      // Exact AI Studio terminal copy observed after a verified submit. It is a
      // model availability failure, so a confirmed FAILED recovery may advance
      // to the next model; unrelated model errors must remain terminal.
      failure = { kind: "MODEL_UNAVAILABLE", text: errorText };
    } else {
      failure = { kind: "MODEL_ERROR", text: errorText };
    }
  }

  return {
    billing,
    billing_mode: billing.mode,
    complete: isNewTurn && footer != null && stop == null && failure == null,
    failure,
    isNew: isNewTurn && Boolean(text),
    model: HUMAN_MODEL,
    model_id: MODEL_ID,
    modelEvidence: MODEL_ID != null && currentModel === MODEL_ID ? [{ kind: "model", text: MODEL_ID }] : [],
    provider: "aistudio",
    sendReady: runButton() != null && !runButton().disabled,
    snapshot: {
      completedCount: turns.length,
      count: turns.length,
      latestTurnId,
      text,
    },
    stopVisible: stop != null,
    title: document.title,
    url: location.href,
  };
}

export async function cancelAistudioGeneration() {
  const visible = (element) => element != null &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden";
  const rawText = (element) => (element?.innerText ?? element?.textContent ?? "").trim();
  const label = Array.from(document.querySelectorAll("span.run-button-label"))
    .find((element) => visible(element) && rawText(element) === "Stop") ?? null;
  const button = label?.closest("button") ?? null;
  if (button == null || button.disabled) return false;
  button.click();
  await new Promise((resolve) => setTimeout(resolve, 300));
  return true;
}

export { AISTUDIO_HUMAN_MODEL, AISTUDIO_MODEL_ID };
