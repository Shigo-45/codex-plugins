import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const script = fileURLToPath(import.meta.url);
if (process.argv[2] === "--worker") {
  const { GeminiBrowserBridge } = await import("../src/gemini-browser.mjs");
  const { inspectGeminiPage } = await import("../src/gemini-page.mjs");
  class FixtureBridge extends GeminiBrowserBridge {
    async runAttempt(input, progress) {
      const result = await super.runAttempt({ ...input, destination: process.env.FIXTURE_URL }, progress);
      return { ...result, conversationUrl: `https://gemini.google.com/app/fixture-${process.pid}` };
    }
    async waitForComposer(client) {
      for (let i = 0; i < 200; i++) {
        if ((await client.call(inspectGeminiPage.toString())).composerReady) return;
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      throw new Error("Fixture composer timed out");
    }
  }
  const result = await new FixtureBridge().ask({ prompt: process.argv[3], filePaths: [join(process.env.GEMINI_WEB_BRIDGE_HOME, `${process.argv[3]}.txt`)] });
  console.log(JSON.stringify(result));
} else {
  const root = await mkdtemp(join(tmpdir(), "gemini-concurrency-smoke-"));
  let active = 0, maximum = 0;
  const starts = [], ends = [];
  const html = `<!doctype html><html><body>
    <button aria-label="Open mode selector, current mode Argon">Argon</button>
    <input type="file" accept=".txt"><div contenteditable="true"></div><button aria-label="Send">Send</button><main></main>
    <script>document.querySelector('input[type="file"]').onchange=async e=>{
      window.context=await e.target.files[0].text();
      const chip=document.createElement('div');chip.className='file-preview-chip';chip.innerText=e.target.files[0].name;document.body.append(chip);
    };document.querySelector('[aria-label="Send"]').onclick=async()=>{
      const input=document.querySelector('[contenteditable]'),prompt=input.innerText.trim();
      const query=document.createElement('p');query.className='user-query';query.innerText=prompt;
      document.querySelector('main').append(query);input.replaceWith(input.cloneNode(false));
      const stop=document.createElement('button');stop.setAttribute('aria-label','Stop');document.body.append(stop);
      await fetch('/start?token='+encodeURIComponent(prompt));
      await new Promise(resolve=>setTimeout(resolve,2000));
      const answer=document.createElement('div');answer.className='model-response-text';answer.innerText='ANSWER:'+prompt+'|'+window.context;
      const footer=document.createElement('div');footer.className='response-footer complete';
      document.querySelector('main').append(answer,footer);stop.remove();
      await fetch('/end?token='+encodeURIComponent(prompt));
    };</script></body></html>`;
  const server = createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    if (url.pathname === "/start") { active++; maximum = Math.max(maximum, active); starts.push(url.searchParams.get("token")); }
    else if (url.pathname === "/end") { active--; ends.push(url.searchParams.get("token")); }
    else { res.setHeader("Content-Type", "text/html;charset=utf-8"); res.end(html); return; }
    res.end("ok");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const env = { ...process.env, GEMINI_WEB_BRIDGE_HOME: root, GEMINI_WEB_BRIDGE_PROXY: "", FIXTURE_URL: `http://127.0.0.1:${server.address().port}` };
  try {
    for (const i of [1, 2, 3, 4]) await writeFile(join(root, `worker-${i}.txt`), `context-${i}`);
    const completed = await Promise.allSettled([1, 2, 3, 4].map(i => exec(process.execPath,
      [script, "--worker", `worker-${i}`], { env, timeout: 60000 })));
    for (const outcome of completed) if (outcome.status === "rejected") throw outcome.reason;
    const results = completed.map(item => JSON.parse(item.value.stdout));
    assert.deepEqual(results.map(item => item.answer), [1, 2, 3, 4].map(i => `ANSWER:worker-${i}|context-${i}`));
    assert.equal(maximum, 3); assert.equal(active, 0); assert.equal(new Set(starts).size, 4); assert.equal(ends.length, 4);
    assert.equal(new Set(results.map(item => item.requestId)).size, 4);
    assert.deepEqual(results.map(item => item.attachments[0].name), [1,2,3,4].map(i => `worker-${i}.txt`));
    console.log(JSON.stringify({ passed: true, workers: 4, maxConcurrentGenerations: maximum, starts, ends,
      answers: results.map(item => item.answer) }, null, 2));
  } finally {
    const cli = fileURLToPath(new URL("../dist/gemini-web-cli.mjs", import.meta.url));
    await exec(process.execPath, [cli, "browser-stop"], { env }).catch(() => {});
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
}
