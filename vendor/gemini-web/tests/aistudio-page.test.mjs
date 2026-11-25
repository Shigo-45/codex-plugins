import assert from "node:assert/strict";
import test from "node:test";

import {
  cancelAistudioGeneration,
  ensureAistudioModelAndBilling,
  inspectAistudioPage,
  prepareAistudioInput,
  readAistudioGenerationState,
  submitAistudioPrompt,
} from "../src/aistudio-page.mjs";
import { createAistudioPage, FakeElement } from "./aistudio-fixture.mjs";

test("AI Studio inspect uses the verified selectors and never exposes account text", () => {
  const page = createAistudioPage();
  const result = page.run(inspectAistudioPage)();
  assert.equal(result.provider, "aistudio");
  assert.equal(result.composerReady, true);
  assert.equal(result.runReady, true);
  assert.equal(result.proVisible, true);
  assert.equal(result.noApiKeyVisible, true);
  assert.equal(result.model, "4 Argon");
  assert.equal(result.modelId, "gemini-4-argon");
  assert.equal(result.modalVisible, false);
  assert.ok(!JSON.stringify(result).includes("user@example.com"));
});

test("AI Studio guard returns human model, model_id and subscription billing evidence", async () => {
  const page = createAistudioPage();
  const result = await page.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(result.ok, true);
  assert.equal(result.model, "4 Argon");
  assert.equal(result.model_id, "gemini-4-argon");
  assert.equal(result.billing_mode, "subscription");
  assert.equal(result.billing.mode, "subscription");
  assert.equal(result.billing.pro, true);
  assert.equal(result.billing.apiKeySelected, false);
  assert.ok(result.evidence.some((item) => item.kind === "model" && item.text === "gemini-4-argon"));
  assert.ok(result.evidence.some((item) => item.kind === "pro_badge" && item.text === "PRO"));
  assert.ok(result.evidence.some((item) => item.kind === "api_key" && item.text === "No API key selected"));
  assert.ok(!JSON.stringify(result).includes("user@example.com"));
});

test("serialized guard and submit functions work with omitted default arguments in a fresh VM", async () => {
  const page = createAistudioPage();
  const ensured = await page.run(ensureAistudioModelAndBilling)(false);
  assert.equal(ensured.ok, true);
  assert.equal(ensured.model, "4 Argon");
  page.elements.composer.onEvent = (event) => {
    if (event.type === "input") page.elements.composer.value = "";
  };
  const submitted = await page.run(submitAistudioPrompt)("fresh vm prompt");
  assert.equal(submitted.ok, true);
  assert.equal(submitted.model, "4 Argon");
  assert.equal(submitted.model_id, "gemini-4-argon");
});

test("PRO is read only from the account switcher, not arbitrary page spans", async () => {
  const page = createAistudioPage();
  page.setPro(false);
  page.document.body.append(new FakeElement("span", { text: "PRO", aria: "decoy PRO" }));
  const result = await page.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(result.ok, false);
  assert.equal(result.code, "BILLING_UNVERIFIED");
  assert.ok(page.time.now() >= 60000);
  const namedPro = createAistudioPage({ pro: false });
  namedPro.elements.account.text = "PRO user@example.com";
  namedPro.elements.account.aria = "Google Account PRO";
  const proResult = await namedPro.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(proResult.code, "BILLING_UNVERIFIED");
  assert.ok(!JSON.stringify(proResult).includes("user@example.com"));
});

test("paid, unknown, missing and exact-aria-wrong-innerText key states fail closed", async () => {
  const paid = createAistudioPage({ noApiKey: false, paidKey: true });
  const paidResult = await paid.run(ensureAistudioModelAndBilling)(false);
  assert.equal(paidResult.code, "PAID_KEY_ACTIVE");
  assert.equal(paidResult.billing.mode, "paid_api_key");
  const unknown = createAistudioPage({ noApiKey: false, unknownApiKey: true, pro: true });
  const unknownResult = await unknown.run(ensureAistudioModelAndBilling)(false);
  assert.equal(unknownResult.code, "BILLING_UNVERIFIED");
  assert.equal(unknownResult.billing.mode, "unverified");
  const missing = createAistudioPage({ noApiKey: false });
  assert.equal((await missing.run(ensureAistudioModelAndBilling)(false)).code, "BILLING_UNVERIFIED");
  const wrongText = createAistudioPage();
  wrongText.elements.api.text = "key_on";
  assert.equal((await wrongText.run(ensureAistudioModelAndBilling)(false)).code, "BILLING_UNVERIFIED");
});

test("model evidence comes only from model-selector-card model-name and must be exact", async () => {
  const wrong = createAistudioPage({ modelId: "gemini-3.8-flash" });
  const result = await wrong.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(result.ok, false);
  assert.equal(result.code, "MODEL_MISMATCH");
  const decoy = createAistudioPage({ modelId: "gemini-3.8-flash" });
  decoy.document.body.append(new FakeElement("span", { text: "gemini-4-argon" }));
  const decoyResult = await decoy.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(decoyResult.code, "MODEL_MISMATCH");
  const unsupported = createAistudioPage();
  assert.equal((await unsupported.run(ensureAistudioModelAndBilling)(false, "5 Neon")).code, "MODEL_UNAVAILABLE");
});

test("AI Studio verifies 4 Hydrogen and 3.8 Flash with exact labels and IDs", async () => {
  for (const [human, id] of [["4 Hydrogen", "gemini-4-hydrogen"], ["3.8 Flash", "gemini-3.8-flash"]]) {
    const page = createAistudioPage({ modelId: id, url: `https://aistudio.google.com/prompts/new_chat?model=${id}` });
    const inspected = page.run(inspectAistudioPage)();
    assert.equal(inspected.model, human);
    assert.equal(inspected.modelId, id);
    const ensured = await page.run(ensureAistudioModelAndBilling)(false, human);
    assert.equal(ensured.ok, true);
    assert.equal(ensured.model, human);
    assert.equal(ensured.model_id, id);
    assert.equal(ensured.billing_mode, "subscription");
    assert.ok(ensured.evidence.some((item) => item.kind === "model" && item.text === id));
    const byId = await page.run(ensureAistudioModelAndBilling)(false, id);
    assert.equal(byId.ok, true);
    assert.equal(byId.model, human);
    page.elements.composer.onEvent = (event) => {
      if (event.type === "input") page.elements.composer.value = "";
    };
    const submitted = await page.run(submitAistudioPrompt)(`${human} prompt`, human);
    assert.equal(submitted.ok, true);
    assert.equal(submitted.model, human);
    assert.equal(submitted.model_id, id);
    assert.equal(submitted.billing_mode, "subscription");
  }
});

test("AI Studio recovery infers the current model when no model is supplied", () => {
  const page = createAistudioPage({ modelId: "gemini-3.8-flash", url: "https://aistudio.google.com/prompts/recovery" });
  page.addTurn({ id: "recovered", text: "recovered answer", footer: true });
  const state = page.run(readAistudioGenerationState)({}, undefined);
  assert.equal(state.complete, true);
  assert.equal(state.failure, null);
  assert.equal(state.model, "3.8 Flash");
  assert.equal(state.model_id, "gemini-3.8-flash");
});

test("AI Studio result state verifies the requested Hydrogen model", () => {
  const page = createAistudioPage({ modelId: "gemini-4-hydrogen" });
  page.addTurn({ id: "hydrogen", text: "hydrogen answer", footer: true });
  const state = page.run(readAistudioGenerationState)({}, "4 Hydrogen");
  assert.equal(state.complete, true);
  assert.equal(state.failure, null);
  assert.equal(state.model, "4 Hydrogen");
  assert.equal(state.model_id, "gemini-4-hydrogen");
  assert.ok(state.modelEvidence.some((item) => item.text === "gemini-4-hydrogen"));
});

test("a visible mat-dialog-container blocks submission before any click", async () => {
  const page = createAistudioPage({ modalVisible: true });
  const guard = await page.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(guard.code, "INTERACTION_REQUIRED");
  page.elements.composer.onEvent = (event) => {
    if (event.type === "input") page.elements.composer.value = "";
  };
  const submit = await page.run(submitAistudioPrompt)("blocked by modal", "4 Argon");
  assert.equal(submit.ok, false);
  assert.equal(submit.clicked, false);
  assert.equal(submit.code, "INTERACTION_REQUIRED");
  assert.equal(page.elements.run.clickCount, 0);
});

test("submit re-verifies model, PRO and no-key immediately before clicking Run", async () => {
  for (const [change, expected] of [
    [(page) => page.setApiKey("paid_key"), "PAID_KEY_ACTIVE"],
    [(page) => page.setPro(false), "BILLING_UNVERIFIED"],
    [(page) => page.setModelId("gemini-3.8-flash"), "MODEL_MISMATCH"],
    [(page) => page.setModal(true), "INTERACTION_REQUIRED"],
  ]) {
    const page = createAistudioPage();
    page.elements.composer.onEvent = (event) => {
      if (event.type === "input") change(page);
    };
    const result = await page.run(submitAistudioPrompt)("recheck prompt", "4 Argon");
    assert.equal(result.ok, false);
    assert.equal(result.clicked, false);
    assert.equal(result.code, expected);
    assert.equal(page.elements.run.clickCount, 0);
  }
});

test("submit locates Run by its exact label and returns verified billing evidence", async () => {
  const page = createAistudioPage();
  page.elements.composer.onEvent = (event) => {
    if (event.type === "input") page.elements.composer.value = "";
  };
  const result = await page.run(submitAistudioPrompt)("verified prompt", "4 Argon");
  assert.equal(result.ok, true);
  assert.equal(result.clicked, true);
  assert.equal(result.model, "4 Argon");
  assert.equal(result.model_id, "gemini-4-argon");
  assert.equal(result.billing_mode, "subscription");
  assert.equal(result.billing.pro, true);
  assert.equal(result.billing.apiKeySelected, false);
  assert.equal(page.elements.run.clickCount, 1);
});

test("an unconfirmed click reports OUTCOME_UNKNOWN", async () => {
  const page = createAistudioPage();
  const result = await page.run(submitAistudioPrompt)("unconfirmed prompt", "4 Argon");
  assert.equal(result.ok, false);
  assert.equal(result.clicked, true);
  assert.equal(result.code, "OUTCOME_UNKNOWN");
  assert.equal(page.elements.run.clickCount, 1);
});

test("the latest Model turn uses top-level cmark text, preserves newlines and excludes thoughts", () => {
  const page = createAistudioPage();
  page.addTurn({ id: "turn-a", text: "line1\nline2 **bold**", thoughts: "secret thought" });
  const state = page.run(readAistudioGenerationState)({ count: 0, latestTurnId: null });
  assert.equal(state.snapshot.text, "line1\nline2 **bold**");
  assert.ok(!state.snapshot.text.includes("nested duplicate"));
  assert.ok(!state.snapshot.text.includes("secret thought"));
  assert.equal(state.snapshot.latestTurnId, "turn-a");
  assert.equal(state.model, "4 Argon");
  assert.equal(state.model_id, "gemini-4-argon");
  assert.equal(state.billing_mode, "subscription");
  assert.equal(state.complete, true);
});

test("only the latest Model turn is read and old failure evidence does not pollute a new turn", () => {
  const page = createAistudioPage();
  page.addTurn({ id: "old-failure", text: "", footer: true, error: "old failure" });
  const old = page.run(readAistudioGenerationState)({ count: 1, latestTurnId: "old-failure" });
  assert.equal(old.snapshot.count, 1);
  assert.equal(old.failure, null);
  assert.equal(old.complete, false);
  page.addTurn({ id: "new-good", text: "new answer", footer: true });
  const fresh = page.run(readAistudioGenerationState)({ count: 1, latestTurnId: "old-failure" });
  assert.equal(fresh.snapshot.count, 2);
  assert.equal(fresh.snapshot.text, "new answer");
  assert.equal(fresh.complete, true);
  assert.equal(fresh.failure, null);
});

test("permission-denied model-error is reported immediately and is never treated as success", () => {
  const page = createAistudioPage();
  page.addTurn({
    id: "denied",
    text: "",
    footer: true,
    error: "权限遭拒。请检查您的账号权限和项目设置。",
  });
  const state = page.run(readAistudioGenerationState)({ count: 0, latestTurnId: null });
  assert.equal(state.failure.kind, "PERMISSION_DENIED");
  assert.match(state.failure.text, /权限遭拒/);
  assert.equal(state.complete, false);
});

test("other model errors are explicit MODEL_ERROR failures", () => {
  const page = createAistudioPage();
  page.addTurn({ id: "failure", text: "", footer: true, error: "Something went wrong while generating." });
  const state = page.run(readAistudioGenerationState)({ count: 0, latestTurnId: null });
  assert.equal(state.failure.kind, "MODEL_ERROR");
  assert.equal(state.complete, false);
});

test("the live AI Studio model-unavailable copy is MODEL_UNAVAILABLE", () => {
  const page = createAistudioPage({ modelId: "gemini-4-hydrogen" });
  page.addTurn({
    id: "unavailable",
    text: "",
    footer: true,
    error: "Model isn't available right now. Please wait a minute and try again.",
  });
  const state = page.run(readAistudioGenerationState)({}, "4 Hydrogen");
  assert.equal(state.failure.kind, "MODEL_UNAVAILABLE");
  assert.match(state.failure.text, /Model isn't available right now/);
  assert.equal(state.complete, false);
});

test("completion requires a footer on the latest Model turn and no visible Stop", () => {
  const page = createAistudioPage();
  page.addTurn({ id: "no-footer", text: "answer", footer: false });
  assert.equal(page.run(readAistudioGenerationState)({}).complete, false);
  page.addTurn({ id: "with-footer", text: "answer", footer: true });
  assert.equal(page.run(readAistudioGenerationState)({}).complete, true);
  page.setStop(true);
  const stopped = page.run(readAistudioGenerationState)({});
  assert.equal(stopped.stopVisible, true);
  assert.equal(stopped.complete, false);
});

test("completion does not depend on Run remaining enabled", () => {
  const page = createAistudioPage();
  page.addTurn({ id: "done", text: "answer", footer: true });
  page.setRunEnabled(false);
  page.elements.run.attrs["aria-disabled"] = "true";
  const state = page.run(readAistudioGenerationState)({});
  assert.equal(state.complete, true);
  assert.equal(state.sendReady, false);
});

test("result processing fails closed on model, PRO and API-key evidence drift", () => {
  const paid = createAistudioPage({ noApiKey: false, paidKey: true });
  paid.addTurn({ id: "paid", text: "answer", footer: true });
  assert.equal(paid.run(readAistudioGenerationState)({}).failure.kind, "PAID_KEY_ACTIVE");
  const noPro = createAistudioPage({ pro: false });
  noPro.addTurn({ id: "no-pro", text: "answer", footer: true });
  assert.equal(noPro.run(readAistudioGenerationState)({}).failure.kind, "BILLING_UNVERIFIED");
  const wrongModel = createAistudioPage({ modelId: "gemini-3.8-flash" });
  wrongModel.addTurn({ id: "wrong-model", text: "answer", footer: true });
  assert.equal(wrongModel.run(readAistudioGenerationState)({}).failure.kind, "MODEL_MISMATCH");
});

test("cancel clicks the exact Stop label and returns false when only Run exists", async () => {
  const page = createAistudioPage({ stopVisible: true });
  assert.equal(await page.run(cancelAistudioGeneration)(), true);
  assert.equal(page.elements.run.clickCount, 1);
  const idle = createAistudioPage();
  assert.equal(await idle.run(cancelAistudioGeneration)(), false);
});


test("AI Studio dismisses its known cookie notice while preserving terms dialogs", async () => {
  const page = createAistudioPage();
  const notice = new FakeElement("div", {className:"glue-cookie-notification-bar visible",
    text:"Google AI Studio uses cookies from Google to deliver its services."});
  const accept = new FakeElement("button", {className:"glue-cookie-notification-bar__accept", text:"OK, got it"});
  accept.onClick = () => { notice.visible = false; };
  notice.append(accept); page.document.body.append(notice);
  const ready = await page.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(ready.ok, true); assert.equal(accept.clickCount, 1);
  const terms = createAistudioPage({modalVisible:true});
  const blocked = await terms.run(ensureAistudioModelAndBilling)(false, "4 Argon");
  assert.equal(blocked.code, "INTERACTION_REQUIRED");
  assert.equal(terms.elements.modal.clickCount, 0);
});

test("native AI Studio input focuses and selects the composer", () => {
  const page=createAistudioPage();
  page.elements.composer.focus=()=>{page.document.activeElement=page.elements.composer;};
  let selected=false;
  page.elements.composer.select=()=>{selected=true;};
  assert.equal(page.run(prepareAistudioInput)().ok,true);
  assert.equal(selected,true);
});

test("native AI Studio submission verifies text and click geometry without synthetic events", async () => {
  const page=createAistudioPage();
  const run=page.elements.runButton;
  // Resolve the button from the verified label, matching the production page.
  const button=run ?? page.document.querySelector("span.run-button-label").closest("button");
  button.scrollIntoView=()=>{};
  button.getBoundingClientRect=()=>({x:10,y:20,width:80,height:40});
  page.document.elementFromPoint=()=>button;
  page.elements.composer.value="native prompt";
  const result=await page.run(submitAistudioPrompt)("native prompt","4 Argon",true);
  assert.equal(result.ok,true);
  assert.equal(result.clicked,false);
  assert.equal(result.nativeClick.x,50);
  assert.equal(button.clickCount,0);
  assert.equal(page.elements.composer.events.length,0);
  page.document.elementFromPoint=()=>null;
  assert.equal((await page.run(submitAistudioPrompt)("native prompt","4 Argon",true)).code,"INTERACTION_REQUIRED");
  assert.equal((await page.run(submitAistudioPrompt)("different","4 Argon",true)).code,"UI_CHANGED");
});

for (const [model, modelId] of [["4 Carbon", "gemini-4-carbon"], ["4 Neon", "gemini-4-neon"]]) {
  test(`AI Studio verifies exact ${model} identity at readiness and submission`, async () => {
    const page = createAistudioPage({ modelId });
    assert.equal(page.run(inspectAistudioPage)().model, model);
    assert.equal((await page.run(ensureAistudioModelAndBilling)(false, model)).model_id, modelId);
    page.elements.composer.onEvent = event => {
      if (event.type === "input") page.elements.composer.value = "";
    };
    const submitted = await page.run(submitAistudioPrompt)("synthetic writing", model);
    assert.equal(submitted.ok, true);
    assert.equal(submitted.model_id, modelId);
  });
}
