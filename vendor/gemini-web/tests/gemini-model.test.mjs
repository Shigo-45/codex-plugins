import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { ensureGeminiArgon } from "../src/gemini-model.mjs";

function page({ appearsAt = 20000, initialMode = "Argon", menuDelay = 0, optionMode = "4 Argon", selected = false, shortLabel = false, ignoreClick = false, limited = [], disabled = [], quotaText = "用量限额重置时间：10月29日 20:22", radioMenu = false, descriptionFirst = false } = {}) {
  let now = 0, mode = initialMode, openedAt = null, optionClicks = 0;
  const control = {
    getClientRects: () => [1],
    getAttribute: name => name === "data-test-id" ? "bard-mode-menu-button" : null,
    get innerText() { return mode; },
    click() { openedAt = now; },
  };
  const option = {
    getClientRects: () => [1], innerText: descriptionFirst ? "代码与分析\n" + optionMode : optionMode + " Complex tasks",
    getAttribute: name => name === "role" ? (radioMenu ? "menuitemradio" : "option") : name === "aria-checked" && radioMenu ? String(selected) : null,
    classList: { contains: name => !radioMenu && selected && name === "selected" },
    click() { optionClicks++; if (!ignoreClick) { mode = shortLabel ? "Flash" : optionMode; selected = true; } },
  };
  const unavailable = [...new Set([...limited, ...disabled])].map(name => ({
    getClientRects: () => [1], innerText: `${name}\n${limited.includes(name) ? quotaText : "Upgrade to access"}`,
    getAttribute: attr => attr === "role" ? (radioMenu ? "menuitemradio" : "menuitem") : attr === "aria-disabled" ? "true" : null,
    click() { throw new Error("Disabled model must never be clicked"); },
  }));
  const verify = runInNewContext(`(${ensureGeminiArgon.toString()})`, {
    document: { querySelectorAll: selector => selector.includes("aria-haspopup")
      ? (now >= appearsAt ? [control] : [])
      : (openedAt != null && now - openedAt >= menuDelay ? [
        ...(radioMenu ? [{ getClientRects: () => [1], innerText: "思考等级", getAttribute: name => name === "role" ? "menuitem" : null }] : []),
        ...unavailable.filter(() => !radioMenu || selector.includes('"menuitemradio"')),
        ...(![...limited, ...disabled].includes(optionMode) && (!radioMenu || selector.includes('"menuitemradio"')) ? [option] : []),
      ] : []) },
    getComputedStyle: () => ({ visibility: "visible" }),
    Date: { now: () => now },
    setTimeout: (callback, ms) => { now += ms; queueMicrotask(callback); },
  });
  return { verify, elapsed: () => now, opened: () => openedAt != null, optionClicks: () => optionClicks, mode: () => mode };
}

test("model verification accepts a selector hydrating more than 10 seconds after the editor", async () => {
  const p = page();
  assert.equal((await p.verify()).model, "4 Argon");
  assert.equal(p.elapsed(), 20000);
});

test("late model discovery leaves a separate budget for selecting Argon", async () => {
  const p = page({ appearsAt: 119000, initialMode: "Flash", menuDelay: 5000 });
  assert.equal((await p.verify()).model, "4 Argon");
  assert.equal(p.elapsed(), 124000);
});

test("missing model selector times out without clicking or submitting", async () => {
  const p = page({ appearsAt: Infinity });
  const result = await p.verify();
  assert.equal(result.code, "MODEL_UNAVAILABLE");
  assert.match(result.message, /120 seconds/);
  assert.equal(p.elapsed(), 120000);
  assert.equal(p.opened(), false);
});

test("late wrong-model continuation is rejected without changing its model", async () => {
  const p = page({ initialMode: "Flash" });
  assert.equal((await p.verify(true)).code, "MODEL_MISMATCH");
  assert.equal(p.optionClicks(), 0);
  assert.equal(p.mode(), "Flash");
});

test("fresh chats can select and verify 3.8 Flash", async () => {
  const p = page({ appearsAt: 0, optionMode: "3.8 Flash" });
  assert.equal((await p.verify(false, "3.8 Flash")).model, "3.8 Flash");
});

test("fresh and continued chats select and verify 4 Hydrogen", async () => {
  const fresh = page({ appearsAt: 0, initialMode: "Argon", optionMode: "4 Hydrogen" });
  assert.equal((await fresh.verify(false, "4 Hydrogen")).model, "4 Hydrogen");
  assert.equal(fresh.optionClicks(), 1);
  const continued = page({ appearsAt: 0, initialMode: "4 Hydrogen", optionMode: "4 Hydrogen", selected: true });
  assert.equal((await continued.verify(true, "4 Hydrogen")).ok, true);
  const wrong = page({ appearsAt: 0, initialMode: "4 Argon" });
  assert.equal((await wrong.verify(true, "4 Hydrogen")).code, "MODEL_MISMATCH");
});

test("4 Hydrogen quota evidence does not masquerade as 4 Argon", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", limited: ["4 Hydrogen"] });
  const result = await p.verify(false, "4 Hydrogen");
  assert.equal(result.code, "RATE_LIMITED");
  assert.deepEqual(Array.from(result.quota.exhausted_models), ["4 Hydrogen"]);
  assert.equal(result.quota.scope, "model");
});

test("3.8 Flash continuation accepts its model but rejects Flash-Lite", async () => {
  const flash = page({ appearsAt: 0, initialMode: "3.8 Flash" });
  assert.equal((await flash.verify(true, "3.8 Flash")).ok, true);
  const lite = page({ appearsAt: 0, initialMode: "3.8 Flash-Lite" });
  assert.equal((await lite.verify(true, "3.8 Flash")).code, "MODEL_MISMATCH");
  assert.equal(lite.optionClicks(), 0);
  assert.equal(lite.mode(), "3.8 Flash-Lite");
});

test("disabled quota options retain reset evidence and identify all exhausted Web models", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", limited: ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"] });
  const result = await p.verify();
  assert.equal(result.code, "WEB_QUOTA_EXHAUSTED");
  assert.equal(result.quota.scope, "web");
  assert.deepEqual(Array.from(result.quota.exhausted_models), ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]);
  assert.match(result.message, /10月29日 20:22/);
  assert.equal(p.elapsed(), 0);
  assert.equal(p.optionClicks(), 0);
});

test("a single model quota leaves the next Web model available", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", limited: ["4 Argon"] });
  const result = await p.verify();
  assert.equal(result.code, "RATE_LIMITED");
  assert.equal(result.quota.scope, "model");
  assert.deepEqual(Array.from(result.quota.exhausted_models), ["4 Argon"]);
  assert.equal(p.optionClicks(), 0);
});

test("English quota resets and wrong-mode continuations retain the quota reason", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", limited: ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"], quotaText: "Usage limit resets at 8:22 PM" });
  assert.equal((await p.verify(true)).code, "WEB_QUOTA_EXHAUSTED");
  assert.equal(p.optionClicks(), 0);
});

test("disabled subscription options remain model availability failures", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", disabled: ["4 Argon"] });
  const result = await p.verify();
  assert.equal(result.code, "MODEL_UNAVAILABLE");
  assert.equal(result.quota, undefined);
  assert.equal(p.optionClicks(), 0);
});

test("Flash's shortened toolbar label is verified against the selected full menu version", async () => {
  const fresh = page({ appearsAt: 0, optionMode: "3.8 Flash", shortLabel: true });
  assert.equal((await fresh.verify(false, "3.8 Flash")).ok, true);
  const continued = page({ appearsAt: 0, initialMode: "Flash", optionMode: "3.8 Flash", selected: true });
  assert.equal((await continued.verify(true, "3.8 Flash")).ok, true);
  const otherVersion = page({ appearsAt: 0, initialMode: "Flash", optionMode: "3.8 Flash", selected: false });
  assert.equal((await otherVersion.verify(true, "3.8 Flash")).code, "MODEL_MISMATCH");
  const ignoredClick = page({ appearsAt: 0, initialMode: "Flash", optionMode: "3.8 Flash", selected: false, ignoreClick: true });
  assert.equal((await ignoredClick.verify(false, "3.8 Flash")).code, "MODEL_UNAVAILABLE");
});

test("radio model options remain discoverable beside the thinking-level menuitem", async () => {
  for (const model of ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]) {
    const p = page({ appearsAt: 0, initialMode: "Flash", optionMode: model, shortLabel: model === "3.8 Flash", radioMenu: true });
    assert.equal((await p.verify(false, model)).ok, true);
    assert.equal(p.optionClicks(), 1);
  }
});

test("radio Flash continuation verifies aria-checked without switching models", async () => {
  for (const selected of [true, false]) {
    const p = page({ appearsAt: 0, initialMode: "Flash", optionMode: "3.8 Flash", selected, radioMenu: true });
    const result = await p.verify(true, "3.8 Flash");
    assert.equal(result.ok, selected);
    if (!selected) assert.equal(result.code, "MODEL_MISMATCH");
    assert.equal(p.optionClicks(), 0);
  }
});

test("radio model quotas preserve reset evidence beside the thinking submenu", async () => {
  const p = page({ appearsAt: 0, initialMode: "Flash-Lite", limited: ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"], radioMenu: true });
  const result = await p.verify();
  assert.equal(result.code, "WEB_QUOTA_EXHAUSTED");
  assert.deepEqual(Array.from(result.quota.exhausted_models), ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]);
  assert.equal(p.optionClicks(), 0);
});

// Live menu layout places a localized purpose above the versioned model name.
test("localized description before the model name remains selectable and verifiable", async () => {
  for (const model of ["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]) {
    const p = page({ appearsAt: 0, initialMode: "Flash", optionMode: model,
      descriptionFirst: true, radioMenu: true, shortLabel: model === "3.8 Flash" });
    assert.equal((await p.verify(false, model)).ok, true);
    assert.equal(p.optionClicks(), 1);
  }
  const lite = page({ appearsAt: 0, initialMode: "Flash", optionMode: "3.8 Flash-Lite", descriptionFirst: true, radioMenu: true });
  assert.equal((await lite.verify(false, "3.8 Flash")).code, "MODEL_UNAVAILABLE");
  assert.equal(lite.optionClicks(), 0);
});

for (const model of ["4 Carbon", "4 Neon"]) {
  test(`fresh chats select and verify ${model}`, async () => {
    const p = page({ appearsAt: 0, initialMode: "Argon", optionMode: model });
    assert.equal((await p.verify(false, model)).model, model);
    assert.equal(p.optionClicks(), 1);
  });
}
