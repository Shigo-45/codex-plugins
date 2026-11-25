import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";

const execFileAsync = promisify(execFile);
const script = new URL(import.meta.url).pathname;
const childMode = process.argv[2];

if (childMode === "--client") {
  const { PersistentBrowserRuntime } = await import("../src/browser-runtime.mjs");
  const { withFileLock } = await import("../src/operation-lock.mjs");
  const { paths } = await import("../src/state-store.mjs");
  const runtime = new PersistentBrowserRuntime();
  await withFileLock(paths.browserLock, {}, async () => {
    if (process.argv[3] === "stop") {
      console.log(JSON.stringify(await runtime.shutdown()));return;
    }
    const port = await runtime.start();
    const {client,target} = await runtime.openTarget(port);
    try {
      if(process.argv[3]==="set")await client.evaluate('document.body.textContent="resident-one"');
      const text=await client.evaluate("document.body.textContent");
      const userAgent=await client.evaluate("navigator.userAgent");
      assert.equal(await client.evaluate("document.visibilityState"), "visible");
      const state=await runtime.read();
      if(process.argv[3]==="close-target"){
        const browser=await runtime.connect(state.browserSocket);
        try {await browser.send("Target.closeTarget",{targetId:target.id});}finally{browser.close();}
      }
      if(process.argv[3]==="crash"){
        await runtime.verify(state);
        process.kill(-state.pid,"SIGKILL");
        for(let i=0;i<50 && await runtime.owned(state);i++)await new Promise(resolve=>setTimeout(resolve,100));
        assert.equal(await runtime.owned(state),false);
      }
      console.log(JSON.stringify({pid:state.pid,targetId:target.id,port,text,userAgent}));
    } finally {client.close();}
  });
} else {
  const root=await mkdtemp(join(tmpdir(),"gemini-resident-smoke-"));
  const env={...process.env,GEMINI_WEB_BRIDGE_HOME:root,GEMINI_WEB_BRIDGE_PROXY:""};
  const child=async action=>JSON.parse((await execFileAsync(process.execPath,[script,"--client",action],{env,timeout:45000})).stdout);
  let server;
  try {
    const first=await child("set"),second=await child("read");
    assert.equal(second.pid,first.pid);assert.equal(second.targetId,first.targetId);assert.equal(second.text,"resident-one");
    assert.match(first.userAgent,/HeadlessChrome/);
    await child("close-target");
    const rebuilt=await child("read");
    assert.equal(rebuilt.pid,first.pid);assert.notEqual(rebuilt.targetId,first.targetId);
    await child("crash");
    const restarted=await child("read");assert.notEqual(restarted.pid,first.pid);

    // Full DOM submission/reading pipeline against a local, synthetic fixture.
    const html=`<!doctype html><html><body>
      <button aria-label="Open mode selector, current mode Argon">Argon</button>
      <input type="file" accept=".txt" multiple><div contenteditable="true"></div><button aria-label="Send">Send</button><main></main>
      <script>document.querySelector('input[type="file"]').onchange=async e=>{
        window.fixtureAttachment=await e.target.files[0].text();
        const chip=document.createElement('div');chip.className='file-preview-chip';chip.textContent=e.target.files[0].name;document.body.append(chip);
      };document.querySelector('[aria-label="Send"]').onclick=()=>{
        const input=document.querySelector('[contenteditable]');const prompt=input.innerText;
        const user=document.createElement('p');user.className='user-query';user.innerText=prompt;document.querySelector('main').append(user);
        // Match Gemini's continuation hydration: replace the editor while the
        // detached node still contains the submitted text.
        input.replaceWith(input.cloneNode(false));
        const answer=document.createElement('div');answer.className='model-response-text';answer.innerText='ANSWER:'+prompt.trim()+(window.fixtureAttachment?'|'+window.fixtureAttachment:'');
        const footer=document.createElement('div');footer.className='response-footer complete';
        document.querySelector('main').append(answer,footer);
      };</script></body></html>`;
    server=createServer((_req,res)=>{res.setHeader("Content-Type","text/html; charset=utf-8");res.end(html);});
    await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
    const fixtureURL=`http://127.0.0.1:${server.address().port}`;
    process.env.GEMINI_WEB_BRIDGE_HOME=root;process.env.GEMINI_WEB_BRIDGE_PROXY="";
    const {GeminiBrowserBridge}=await import("../src/gemini-browser.mjs");
    const {inspectGeminiPage}=await import("../src/gemini-page.mjs");
    class FixtureBridge extends GeminiBrowserBridge {
      async runAttempt(input,onProgress){
        const result=await super.runAttempt({...input,destination:fixtureURL},onProgress);
        return {...result,conversationUrl:"https://gemini.google.com/app/local-fixture"};
      }
      async waitForComposer(client){
        for(let i=0;i<100;i++){
          if((await client.call(inspectGeminiPage.toString())).composerReady)return;
          await new Promise(resolve=>setTimeout(resolve,50));
        }
        throw new Error("Local fixture composer unavailable");
      }
    }
    const a=new FixtureBridge(),one=await a.ask({prompt:"one"});
    const aStatus=await a.browserStatus();
    const b=new FixtureBridge(),two=await b.ask({prompt:"two",conversationId:one.conversationId});
    const bStatus=await b.browserStatus();
    assert.equal(one.answer,"ANSWER:one");assert.equal(two.answer,"ANSWER:two");
    assert.equal(aStatus.pid,bStatus.pid);assert.equal(aStatus.targetId,bStatus.targetId);
    const contextFile=join(root,'context.txt');await writeFile(contextFile,'synthetic-upload-content');
    const three=await b.ask({prompt:'three',filePaths:[contextFile]});
    assert.equal(three.answer,'ANSWER:three|synthetic-upload-content');
    assert.deepEqual(three.attachments,[{name:'context.txt',size:24}]);
    const stopped=await b.stopBrowser();assert.equal(stopped.stopped,true);
    assert.equal((await b.browserStatus()).connected,false);
    console.log(JSON.stringify({passed:true,first,second,rebuilt,restarted,
      fixtureAnswers:[one.answer,two.answer,three.answer],fixturePid:aStatus.pid,fixtureTarget:aStatus.targetId,stopped},null,2));
  } finally {
    await child("stop").catch(()=>{});
    if(server)await new Promise(resolve=>server.close(resolve));
    await rm(root,{recursive:true,force:true});
  }
}
