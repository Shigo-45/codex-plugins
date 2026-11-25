// Serialized into the task page. Only controls explicitly labelled for reasoning
// are eligible; model names and free page text never establish an effort level.
export async function ensureReasoningEffort(effort) {
  const visible = el => el?.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const text = el => `${el?.getAttribute?.("aria-label") ?? ""} ${el?.innerText ?? ""}`.trim();
  const normalize = value => value.trim().toLowerCase().replace(/[\s_-]+/g, "");
  const levels = effort === "xhigh" ? ["xhigh", "extrahigh", "超高"] : ["high", "高"];
  const exact = value => levels.includes(normalize(value));
  const fail = () => ({ ok: false, code: "REASONING_UNAVAILABLE", message: `Verify an explicit reasoning control set to ${effort} before submission.` });
  if (!["high", "xhigh"].includes(effort)) return fail();
  const controls = () => Array.from(document.querySelectorAll('select, [role="combobox"], button, [role="button"], [role="menuitem"]')).filter(el =>
    visible(el) && /thinking|reasoning|思考|推理/i.test(text(el)));
  const selected = control => {
    if (control.tagName === "SELECT") return Array.from(control.selectedOptions ?? []).some(option => exact(option.textContent ?? ""));
    const value = control.getAttribute?.("aria-valuetext");
    if (value) return exact(value);
    const levels = (control.innerText ?? "").split(/[\n:：]/).map(normalize)
      .filter(value => ["minimal", "low", "medium", "high", "xhigh", "extrahigh", "max", "低", "中", "高", "超高"].includes(value));
    return levels.length === 1 && exact(levels[0]);
  };
  let control = controls().find(el => !el.disabled && el.getAttribute?.("aria-disabled") !== "true");
  if (!control) {
    const picker = Array.from(document.querySelectorAll('button[data-test-id="bard-mode-menu-button"]')).find(visible);
    if (picker) {
      picker.click();
      for (let i = 0; i < 20 && !control; i++) {
        await new Promise(resolve => setTimeout(resolve, 100));
        control = controls().find(el => !el.disabled && el.getAttribute?.("aria-disabled") !== "true");
      }
    }
  }
  if (!control) return fail();
  if (selected(control)) return { ok: true, reasoning_effort: effort };
  let chosen = null;
  if (control.tagName === "SELECT") {
    const option = Array.from(control.options ?? []).find(el => !el.disabled && exact(el.textContent ?? ""));
    if (!option) return fail();
    control.value = option.value;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    control.click();
    for (let i = 0; i < 20; i++) {
      const option = Array.from(document.querySelectorAll('[role="option"], [role="menuitemradio"]')).find(el =>
        visible(el) && !el.disabled && el.getAttribute?.("aria-disabled") !== "true" && exact(el.innerText ?? ""));
      if (option) { chosen = option; option.click(); break; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  }
  for (let i = 0; i < 20; i++) {
    if (controls().some(selected) || chosen?.getAttribute?.("aria-checked") === "true" || chosen?.getAttribute?.("aria-selected") === "true") return { ok: true, reasoning_effort: effort };
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return fail();
}
