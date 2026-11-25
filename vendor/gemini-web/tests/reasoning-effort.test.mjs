import assert from 'node:assert/strict';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import { ensureReasoningEffort } from '../src/reasoning-effort.mjs';
function page({ label = 'Thinking level', options = ['High', 'xHigh'], selected = 'High', ignoreChange = false } = {}) {
  let current = selected;
  const control = { tagName: 'SELECT', getClientRects: () => [1],
    getAttribute: name => name === 'aria-label' ? label : null,
    get selectedOptions() { return [{ textContent: current }]; },
    options: options.map(textContent => ({ textContent, value: textContent })),
    set value(value) { if (!ignoreChange) current = value; }, dispatchEvent() {} };
  const verify = runInNewContext(`(${ensureReasoningEffort.toString()})`, {
    document: { querySelectorAll: selector => selector.startsWith("button[data-test-id") ? [] : [control] },
    getComputedStyle: () => ({ visibility: 'visible' }), Event: class {},
    setTimeout: fn => queueMicrotask(fn),
  });
  return { verify, current: () => current };
}
test('exact reasoning selection distinguishes high from xhigh', async () => {
  const p = page();
  assert.equal((await p.verify('xhigh')).reasoning_effort, 'xhigh');
  assert.equal(p.current(), 'xHigh');
  assert.equal((await p.verify('high')).reasoning_effort, 'high');
  assert.equal(p.current(), 'High');
});
test('model label and unrelated High text do not establish thinking effort', async () => {
  assert.equal((await page({ label: 'Model 4 Neon High' }).verify('high')).code, 'REASONING_UNAVAILABLE');
});
test('missing xhigh and ignored UI change fail before submission', async () => {
  assert.equal((await page({ options: ['High'] }).verify('xhigh')).ok, false);
  assert.equal((await page({ ignoreChange: true }).verify('xhigh')).ok, false);
});

test('expanded low/high option text never establishes the selected level', async () => {
  const control = { tagName: 'DIV', innerText: 'Thinking level\nLow\nHigh', getClientRects: () => [1],
    getAttribute: name => name === 'aria-label' ? 'Thinking level' : null, click() {} };
  const verify = runInNewContext(`(${ensureReasoningEffort.toString()})`, {
    document: { querySelectorAll: selector => selector.startsWith('select') ? [control] : [] },
    getComputedStyle: () => ({ visibility: 'visible' }), setTimeout: fn => queueMicrotask(fn),
  });
  assert.equal((await verify('high')).ok, false);
});
test('hidden thinking submenu is opened through the known model picker', async () => {
  let menu = false, submenu = false, checked = false;
  const attrs = { getClientRects: () => [1] };
  const picker = { ...attrs, click() { menu = true; } };
  const control = { ...attrs, innerText: 'Thinking level', getAttribute: () => null, click() { submenu = true; } };
  const option = { ...attrs, innerText: 'xHigh', getAttribute: name => name === 'aria-checked' ? String(checked) : null,
    click() { checked = true; } };
  const verify = runInNewContext(`(${ensureReasoningEffort.toString()})`, {
    document: { querySelectorAll: selector => selector.startsWith('button[data-test-id') ? [picker] :
      selector.startsWith('select') ? (menu ? [control] : []) : (submenu ? [option] : []) },
    getComputedStyle: () => ({ visibility: 'visible' }), setTimeout: fn => queueMicrotask(fn),
  });
  assert.equal((await verify('xhigh')).reasoning_effort, 'xhigh');
  assert.equal(checked, true);
});
