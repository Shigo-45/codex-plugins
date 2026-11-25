import assert from 'node:assert/strict';
import test from 'node:test';
import { PersistentBrowserRuntime } from '../src/browser-runtime.mjs';

for (const pending of [
  { model: '4 Hydrogen', conversationUrl: 'https://aistudio.google.com/prompts/new_chat' },
  { conversationUrl: 'https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen' },
]) {
  test(`recovery uses original Hydrogen model (${pending.model ? 'saved model' : 'legacy URL'})`, async () => {
    const socket = 'ws://127.0.0.1:9000/devtools/browser/test';
    let inspectedModel;
    const runtime = new PersistentBrowserRuntime({
      request: async () => [{ id: 'target', type: 'page', webSocketDebuggerUrl: 'ws://127.0.0.1:9000/devtools/page/test' }],
      connect: async () => ({
        call: async (_fn, args) => {
          inspectedModel = args[1];
          return { snapshot: { text: '' }, failure: { kind: 'MODEL_ERROR' }, stopVisible: false };
        },
        close() {},
      }),
    });
    runtime.owned = async () => true;
    runtime.verify = async () => {};
    const result = await runtime.inspectRequest({ port: 9000, browserSocket: socket }, {
      ...pending, provider: 'aistudio', targetId: 'target', browserSocket: socket, phase: 'SUBMITTED',
    });
    assert.equal(inspectedModel, pending.model ?? 'gemini-4-hydrogen');
    assert.equal(result.outcome, 'FAILED');
    assert.equal(result.failure, 'MODEL_ERROR');
  });
}
