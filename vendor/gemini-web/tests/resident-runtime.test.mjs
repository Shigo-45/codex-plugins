import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { EventEmitter } from "node:events";

process.env.GEMINI_WEB_BRIDGE_HOME = await mkdtemp(join(tmpdir(), "resident-locks-"));
const { PersistentBrowserRuntime, launchBrowserProcess } = await import("../src/browser-runtime.mjs");
const { GeminiBrowserBridge, GeminiBridgeError } = await import("../src/gemini-browser.mjs");
const { getConversation, saveConversation } = await import("../src/state-store.mjs");
const { ensureGeminiArgon } = await import("../src/gemini-model.mjs");

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "resident-test-"));
  t.after(() => rm(root, {recursive:true,force:true}));
  const f = {root, profile:join(root,"profile"), alive:false, pid:2000, port:3000,
    launches:0, creates:0, closes:0, fronts:0, sends:0, cancel:0, pages:[], proxy:[], navigations:[], modelOK:true, prompts:[],
    backgroundTargets:[], httpNew:0, boundsUpdates:[], windowState:"normal", refuseMinimize:false,
    hiddenTargetLists:0, windowLookupFailures:0, invalidCreatedSocket:false, throwAfterCreate:false,
    failPageEnable:false, closedTargets:[],
    aistudioProbes:0, geminiProbes:0, aistudioGeneration:null, aistudioSends:0, aistudioPrompts:[], aistudioReady:null,
    aistudioSubmitUnknown:false, inputEvents:[]};
  f.socket = () => `ws://127.0.0.1:${f.port}/devtools/browser/owned-${f.pid}`;
  f.page = () => ({id:`page-${++f.creates}`,type:"page",url:"about:blank",
    webSocketDebuggerUrl:`ws://127.0.0.1:${f.port}/devtools/page/${f.creates}`});
  const deps = {
    root, profile:f.profile, platform:"darwin",
    pause:async()=>{}, proxy:()=>f.proxy,
    identity:async pid => f.alive && pid===f.pid ? `start-${pid}` : null,
    launch:(_exe,args)=>{
      f.launchArgs=args;
      f.alive=true;f.launches++;f.pid++;f.port++;f.pages=args.includes("--no-startup-window")?[]:[f.page()];
      writeFileSync(join(f.profile,"DevToolsActivePort"),`${f.port}\n/devtools/browser/owned-${f.pid}\n`);
      return f.pid;
    },
    request:async(_port,path)=>{
      if(!f.alive)throw new Error("ECONNREFUSED");
      if(path==="/json/version")return {webSocketDebuggerUrl:f.foreign ? f.socket()+"-foreign":f.socket()};
      if(path==="/json/list"){
        if(f.hiddenTargetLists>0 && f.lastCreatedTarget){f.hiddenTargetLists--;return f.pages.filter(p=>p.id!==f.lastCreatedTarget);}
        return f.pages;
      }
      if(path.startsWith("/json/new")){f.httpNew++;const p=f.page();f.pages.push(p);return p;}
      if(path.startsWith("/json/close/")){
        const id=decodeURIComponent(path.slice("/json/close/".length));f.closedTargets.push(id);
        f.pages=f.pages.filter(p=>p.id!==id);return true;
      }
      throw new Error(`Unexpected test endpoint ${path}`);
    },
    connect:async url=>({
      send:async(method,args)=>{
        if (method.startsWith("Input.")) f.inputEvents.push({ method, ...args });
        if(method==="Browser.close"){f.closes++;f.alive=false;}
        if(method==="Target.createTarget"){
          f.backgroundTargets.push(args);const p=f.page();f.lastCreatedTarget=p.id;
          if(f.invalidCreatedSocket)p.webSocketDebuggerUrl="ws://127.0.0.1:1/devtools/page/foreign";
          f.pages.push(p);if(f.throwAfterCreate)throw new Error("create response lost");return {targetId:p.id};
        }
        if(method==="Target.closeTarget"){f.closedTargets.push(args.targetId);f.pages=f.pages.filter(p=>p.id!==args.targetId);return {success:true};}
        if(method==="Browser.getWindowForTarget"){
          if(f.windowLookupFailures>0){f.windowLookupFailures--;throw new Error("window not ready");}
          return {windowId:1,bounds:{windowState:f.windowState}};
        }
        if(method==="Browser.setWindowBounds"){
          f.boundsUpdates.push(args);if(!f.refuseMinimize)f.windowState=args.bounds.windowState;return {};
        }
        if(method==="Page.bringToFront")f.fronts++;
        if(method==="Page.enable" && f.failPageEnable)throw new Error("page setup failed");
        if(method==="Page.navigate")f.navigations.push(args.url);
        return {};
      },
      call:async function(declaration,args){
        if(declaration.includes("function ensureGeminiModel"))return f.modelResult ?? (f.modelOK?{ok:true}:{ok:false,code:"MODEL_MISMATCH",message:"Wrong model"});
        if(declaration.includes("function submitGeminiPrompt")){
          f.sends++;this.prompt=args[0];f.prompts.push(args[0]);
          if(f.disconnectSubmit)throw new Error("WebSocket connection closed");
          f.pages.find(p=>p.webSocketDebuggerUrl===url).url="https://gemini.google.com/app/fixture";
          return {url:"https://gemini.google.com/app/fixture",before:{}};
        }
        if(declaration.includes("function ensureAistudioModelAndBilling")) {
          return f.aistudioReady ?? {ok:true,model:"4 Argon",model_id:"gemini-4-argon",billing_mode:"subscription",
            billing:{apiKeySelected:false,evidence:[{kind:"pro_badge",text:"PRO"}],mode:"subscription",pro:true}};
        }
        if(declaration.includes("function prepareAistudioInput")) return {ok:true};
        if(declaration.includes("function submitAistudioPrompt")) {
          f.aistudioSends++;f.aistudioPrompts.push(args[0]);
          const page=f.pages.find(p=>p.webSocketDebuggerUrl===url);
          if(f.aistudioSubmitUnknown){page.url="https://aistudio.google.com/prompts/ai-unknown";
            return {ok:false,clicked:true,code:"OUTCOME_UNKNOWN",message:"synthetic unconfirmed AI Studio click",url:page.url};}
          page.url="https://aistudio.google.com/prompts/ai-submitted";
          return {ok:true,clicked:false,nativeClick:{x:100,y:150},before:{count:0,completedCount:0,latestTurnId:null},url:page.url,
            model:"4 Argon",model_id:"gemini-4-argon",billing_mode:"subscription",
            billing:{apiKeySelected:false,evidence:[{kind:"pro_badge",text:"PRO"}],mode:"subscription",pro:true}};
        }
        if(declaration.includes("function cancelAistudioGeneration")){f.aistudioCancel=(f.aistudioCancel??0)+1;return true;}
        if(declaration.includes("function readAistudioGenerationState")) {
          f.aistudioProbes++;
          return f.aistudioGeneration ?? {
            billing: { apiKeySelected: false, evidence: [], mode: "subscription", pro: true },
            billing_mode: "subscription",
            complete: false,
            failure: null,
            isNew: false,
            model: "4 Argon",
            model_id: "gemini-4-argon",
            provider: "aistudio",
            snapshot: { count: 0, completedCount: 0, latestTurnId: null, text: "" },
            stopVisible: false,
            url: f.pages.find(p=>p.webSocketDebuggerUrl===url)?.url,
          };
        }
        if(declaration.includes("function readGeminiGenerationState")) {
          f.geminiProbes++;
          if (f.probeFails && f.sends > 0) throw new Error("WebSocket connection closed");
          return f.generation ?? { url: f.pages.find(p=>p.webSocketDebuggerUrl===url)?.url,
            snapshot: {count:f.sends, completedCount:f.sends, text:f.sends ? "answer" : ""},
            isNew:f.sends>0, stopVisible:Boolean(f.disconnectSubmit && f.sends), failure:null };
        }
        if(declaration.includes("function cancelGeminiGeneration")){f.cancel++;return true;}
        throw new Error("Unexpected test page function");
      },
      close(){},
    }),
  };
  f.runtime=(overrides={})=>{
    const runtime=new PersistentBrowserRuntime({...deps,...overrides});
    runtime.findBrowser=async()=>"/fake/chrome";
    return runtime;
  };
  return f;
}

test("macOS headless launches directly while headed launches create a new background app instance", async () => {
  const exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const profile = "--user-data-dir=/tmp/dedicated Chrome Profile";
  const calls = [], endpoints = [];
  const child = new EventEmitter();
  child.pid = 1234;
  child.stderr = new EventEmitter();
  child.stderr.unref = () => {};
  child.unref = () => {};
  const deps = {
    platform: "darwin",
    spawnProcess: (command, args) => {
      calls.push({command,args});
      queueMicrotask(() => child.emit("spawn"));
      return child;
    },
    runOpen: async (command, args) => { calls.push({command,args}); },
    findPid: async (_exe, args) => { assert.ok(args.includes(profile)); return 2345; },
  };
  assert.equal(await launchBrowserProcess(exe, [profile,"--headless=new"], endpoint => endpoints.push(endpoint), deps), 1234);
  assert.equal(calls[0].command, exe);
  child.stderr.emit("data", "DevTools listening on ws://127.0.0.1:4567/devtools/browser/test\n");
  assert.deepEqual(endpoints, ["ws://127.0.0.1:4567/devtools/browser/test"]);
  assert.equal(await launchBrowserProcess(exe, [profile,"--start-minimized"], null, deps), 2345);
  assert.equal(calls[1].command, "/usr/bin/open");
  assert.deepEqual(calls[1].args, ["-n","-g","-a","/Applications/Google Chrome.app","--args",profile,"--start-minimized"]);
});

test("launcher reports process and LaunchServices errors before browser discovery", async () => {
  const exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const child = new EventEmitter();
  const spawnProcess = () => { queueMicrotask(() => child.emit("error", new Error("ENOENT"))); return child; };
  await assert.rejects(() => launchBrowserProcess(exe, ["--headless=new"], null, {platform:"darwin",spawnProcess}),
    error => error.code === "BROWSER_START_FAILED");
  await assert.rejects(() => launchBrowserProcess(exe, [], null, {
    platform:"darwin", runOpen:async()=>{throw new Error("LaunchServices failed");},
    findPid:async()=>{throw new Error("unexpected process discovery");},
  }), error => error.code === "BROWSER_START_FAILED");
});

test("headed startup discovers a fixed debugging port without an active-port file or stderr", async t => {
  const f = await fixture(t);
  const runtime = f.runtime({allocatePort:async()=>f.port+1});
  runtime.activePort = async () => null;
  const port = await runtime.start(undefined, {mode:"headed"});
  assert.equal(port, f.port);
  assert.ok(f.launchArgs.includes(`--remote-debugging-port=${port}`));
  assert.ok(f.launchArgs.includes("--no-startup-window"));
  assert.equal(f.launchArgs.includes("about:blank"), false);
  assert.equal((await runtime.read()).browserMode, "headed");
  await runtime.shutdown();
});

test("resident browser and task target persist across independent bridge instances",async t=>{
  const f=await fixture(t),a=f.runtime();
  const p=await a.start();const first=await a.openTarget(p);first.client.close();
  const b=f.runtime();assert.equal(await b.start(),p);
  const second=await b.openTarget(p);second.client.close();
  assert.equal(first.target.id,second.target.id);assert.equal(f.launches,1);assert.equal(f.creates,1);
  assert.equal((await b.status()).runtimeMode,"headless-resident");
  await b.shutdown();assert.equal(f.closes,1);assert.equal(await b.read(),null);
});

test("closed task target is recreated while browser stays alive",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();
  const first=await r.openTarget(port);first.client.close();f.pages=[];
  const second=await r.openTarget(port);second.client.close();
  assert.notEqual(first.target.id,second.target.id);assert.equal(f.launches,1);assert.equal(f.creates,2);
});

test("dead browser is restarted, without reusing its stale endpoint",async t=>{
  const f=await fixture(t),r=f.runtime(),first=await r.start();
  f.alive=false;const second=await f.runtime().start();
  assert.notEqual(first,second);assert.equal(f.launches,2);assert.equal(f.closes,0);
});

test("foreign endpoint identity fails closed for reuse and shutdown",async t=>{
  const f=await fixture(t),r=f.runtime();await r.start();f.foreign=true;
  await assert.rejects(()=>r.start(),e=>e.code==="BROWSER_OWNERSHIP");
  await assert.rejects(()=>r.shutdown(),e=>e.code==="BROWSER_OWNERSHIP");
  assert.equal(f.closes,0);assert.ok(await r.read());
});

test("proxy changes require explicit shutdown and do not silently reuse old settings",async t=>{
  const f=await fixture(t),r=f.runtime();await r.start();f.proxy=["--proxy-server=http://localhost:1234"];
  await assert.rejects(()=>r.start(),e=>e.code==="BROWSER_CONFIG_CHANGED");
  await r.shutdown();await r.start();assert.equal(f.launches,2);
});

test("runtime metadata is private and corrupt metadata does not control Chrome",async t=>{
  const f=await fixture(t),r=f.runtime();await r.start();
  const {stat}=await import("node:fs/promises");assert.equal((await stat(r.file)).mode&0o777,0o600);
  await writeFile(r.file,"{broken");
  await assert.rejects(()=>r.start(),e=>e.code==="BROWSER_RUNTIME_CORRUPT");assert.equal(f.closes,0);
});

class TaskBridge extends GeminiBrowserBridge {
  active=0;maxActive=0;
  async waitForComposer(){}
  async waitForAnswer(client){
    this.active++;this.maxActive=Math.max(this.maxActive,this.active);
    await new Promise(resolve=>setTimeout(resolve,15));this.active--;
    return {answer:client.prompt,conversationUrl:"https://gemini.google.com/app/fixture"};
  }
}

test("asks reuse a page and preserve fresh/continued conversation destinations",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  const first=await b.ask({prompt:"first"});
  const second=await b.ask({prompt:"continued",conversationId:first.conversationId});
  assert.match(first.answer,/first/);assert.match(second.answer,/continued/);
  assert.deepEqual(f.navigations,["https://gemini.google.com/app"]);
  assert.equal(f.launches,1);assert.equal(f.creates,2);assert.equal(f.closes,0);
});

test("AI Studio ask uses its adapter, home route and persists provider/billing", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  b.waitForAnswer=async()=>({answer:"ai answer",billing:{apiKeySelected:false,evidence:[],mode:"subscription",pro:true},
    conversationUrl:"https://aistudio.google.com/prompts/ai-answered"});
  const result=await b.ask({prompt:"ai prompt",provider:"aistudio"});
  assert.equal(result.provider,"aistudio");assert.equal(result.model,"4 Argon");
  assert.equal(result.model_id,"gemini-4-argon");assert.equal(result.billing_mode,"subscription");
  assert.equal(result.billing.mode,"subscription");assert.deepEqual(f.aistudioPrompts,["ai prompt"]);
  assert.equal(f.launchArgs.includes("--headless=new"),false);
  assert.ok(f.launchArgs.includes("--start-minimized"));
  assert.ok(f.launchArgs.includes("--no-startup-window"));
  assert.deepEqual(f.backgroundTargets,[{url:"about:blank",background:true}]);
  assert.equal(f.httpNew,0);
  assert.deepEqual(f.boundsUpdates,[{windowId:1,bounds:{windowState:"minimized"}}]);
  assert.equal(f.fronts,0);
  assert.ok(f.launchArgs.some(arg=>/^--remote-debugging-port=[1-9]/.test(arg)));
  assert.equal((await b.browserStatus()).runtimeMode,"headed-resident");
  assert.equal(f.aistudioSends,1);assert.equal(f.sends,0);
  assert.equal(f.inputEvents[0].method,"Input.insertText");
  assert.equal(f.inputEvents[0].text,"ai prompt");
  assert.deepEqual(f.inputEvents.slice(1).map(event=>event.type),["mouseMoved","mousePressed","mouseReleased"]);
  assert.deepEqual(f.navigations,["https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"]);
  assert.equal((await getConversation(result.conversationId)).provider,"aistudio");
  assert.equal((await b.runtime.status()).pendingRequest,null);
});

test("headed tasks create background targets without changing an existing window", async t => {
  const f=await fixture(t),runtime=f.runtime();
  await runtime.start(undefined,{mode:"headed"});
  const existing=f.page();f.pages.push(existing);
  const task=runtime.taskRuntime(1),port=await task.start(undefined,{mode:"headed"});
  const opened=await task.openTarget(port);opened.client.close();
  assert.deepEqual(f.backgroundTargets,[{url:"about:blank",background:true}]);
  assert.equal(f.httpNew,0);assert.deepEqual(f.boundsUpdates,[]);
  assert.ok(f.pages.some(page=>page.id===existing.id));
});

test("a headed target is closed when its initial window cannot minimize", async t => {
  const f=await fixture(t),runtime=f.runtime();f.refuseMinimize=true;
  const port=await runtime.start(undefined,{mode:"headed"});
  await assert.rejects(()=>runtime.openTarget(port),error=>error.code==="BROWSER_START_FAILED");
  assert.equal(f.httpNew,0);assert.deepEqual(f.pages,[]);
});

test("background target discovery waits for the window and page endpoint", async t => {
  const f=await fixture(t),runtime=f.runtime();f.windowLookupFailures=2;f.hiddenTargetLists=2;
  const port=await runtime.start(undefined,{mode:"headed"});
  const opened=await runtime.openTarget(port);opened.client.close();
  assert.equal(f.windowLookupFailures,0);assert.equal(f.hiddenTargetLists,0);
  assert.equal(opened.target.id,f.lastCreatedTarget);assert.deepEqual(f.closedTargets,[]);
});

test("an invalid created target endpoint is closed without using it", async t => {
  const f=await fixture(t),runtime=f.runtime();f.invalidCreatedSocket=true;
  const port=await runtime.start(undefined,{mode:"headed"});
  await assert.rejects(()=>runtime.openTarget(port),error=>error.code==="BROWSER_OWNERSHIP");
  assert.deepEqual(f.closedTargets,[f.lastCreatedTarget]);assert.deepEqual(f.pages,[]);
});

test("an unknown created target is left intact when its response is lost", async t => {
  const f=await fixture(t),runtime=f.runtime();f.throwAfterCreate=true;
  const port=await runtime.start(undefined,{mode:"headed"});
  await assert.rejects(()=>runtime.openTarget(port),/create response lost/);
  assert.deepEqual(f.closedTargets,[]);assert.equal(f.pages[0].id,f.lastCreatedTarget);
});

test("page setup failure preserves a previously existing target", async t => {
  const f=await fixture(t),runtime=f.runtime();const port=await runtime.start();
  const existing=f.pages[0];f.failPageEnable=true;
  await assert.rejects(()=>runtime.openTarget(port),/page setup failed/);
  assert.deepEqual(f.closedTargets,[]);assert.ok(f.pages.some(page=>page.id===existing.id));
});

test("state write failure closes only the newly created target", async t => {
  const f=await fixture(t),runtime=f.runtime();await runtime.start(undefined,{mode:"headed"});
  const existing=f.page();f.pages.push(existing);
  const task=runtime.taskRuntime(1),port=await task.start(undefined,{mode:"headed"});
  const write=task.write.bind(task);task.write=async state=>{if(state.targetId)throw new Error("state write failed");return write(state);};
  await assert.rejects(()=>task.openTarget(port),/state write failed/);
  assert.deepEqual(f.closedTargets,[f.lastCreatedTarget]);assert.ok(f.pages.some(page=>page.id===existing.id));
});

test("AI Studio follow-ups reuse the same browser and conversation page", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  b.waitForAnswer=async(_client,_before,_progress,_signal,context)=>({
    answer: context.conversationId ? "second ai answer" : "first ai answer",
    billing:{apiKeySelected:false,evidence:[],mode:"subscription",pro:true},
    conversationUrl:"https://aistudio.google.com/prompts/ai-submitted",
  });
  const first=await b.ask({prompt:"first ai",provider:"aistudio"});
  const second=await b.ask({prompt:"second ai",provider:"aistudio",conversationId:first.conversationId});
  assert.equal(first.conversationId,second.conversationId);
  assert.equal(f.launches,1);
  assert.equal(f.fronts,0);
  assert.deepEqual(f.navigations,["https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"]);
});

test("AI Studio close_after shuts down only the owned browser after a result", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  b.waitForAnswer=async()=>({answer:"closed after result",billing:{apiKeySelected:false,evidence:[],mode:"subscription",pro:true},conversationUrl:"https://aistudio.google.com/prompts/ai-closed"});
  const result=await b.ask({prompt:"close me",provider:"aistudio",closeAfter:true});
  assert.equal(result.browserClosed,true);
  assert.equal(f.closes,1);
  assert.equal(await b.runtime.read(),null);
});

test("unconfirmed AI Studio submission archives an unknown outcome without replay", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  f.aistudioSubmitUnknown=true;
  await assert.rejects(()=>b.ask({prompt:"unknown ai",provider:"aistudio"}),error=>{
    assert.equal(error.code,"OUTCOME_UNKNOWN");assert.equal(error.retrySafe,false);
    assert.equal(error.provider,"aistudio");return true;
  });
  assert.equal(f.aistudioSends,1);assert.equal(f.sends,0);
  const status=await b.browserStatus();
  assert.equal(status.pendingRequest,null);
  assert.equal(status.lastRecovery.provider,"aistudio");
  assert.equal(status.lastRecovery.outcome,"SETTLED_UNKNOWN");
  await b.recoverRequest({requestId:status.lastRecovery.requestId});
  assert.equal(f.aistudioSends,1);
});

test("AI Studio billing guard stops before submission and releases its slot", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  f.aistudioReady={ok:false,code:"BILLING_UNVERIFIED",message:"No API key selected was not visible"};
  await assert.rejects(()=>b.ask({prompt:"blocked billing",provider:"aistudio"}),error=>{
    assert.equal(error.code,"BILLING_UNVERIFIED");assert.equal(error.provider,"aistudio");return true;
  });
  assert.equal(f.aistudioSends,0);assert.equal(f.sends,0);
  const status=await b.browserStatus();
  assert.equal(status.pendingRequest,null);
  assert.equal(status.lastRecovery.provider,"aistudio");
  assert.equal(status.lastRecovery.outcome,"FAILED");
});

test("prompts are submitted verbatim with request identifiers kept in local state", async t => {
  const f = await fixture(t), bridge = new TaskBridge({ runtime: f.runtime() });
  const prompt = "我想周末去上海散步，你能推荐一条轻松的路线吗？";
  const result = await bridge.ask({ prompt, model: "3.8 Flash" });
  assert.deepEqual(f.prompts, [prompt]);
  assert.equal(result.answer, prompt);
  assert.ok(result.requestId);
});

test("four independent asks run three at once with isolated pages and a queued fourth",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  const wait=b.waitForAnswer;
  b.waitForAnswer=async function(client){await new Promise(resolve=>setTimeout(resolve,200));return wait.call(this,client);};
  let running=0,maxRunning=0;const original=b.runAttempt;
  b.runAttempt=async function(...args){running++;maxRunning=Math.max(maxRunning,running);try{return await original.apply(this,args);}finally{running--;}};
  const results=await Promise.all(["one","two","three","four"].map(prompt=>b.ask({prompt})));
  assert.equal(maxRunning,3);assert.deepEqual(results.map(x=>x.answer),["one","two","three","four"]);
  assert.equal(f.launches,1);assert.equal(f.creates,4);
  const status=await b.browserStatus();assert.equal(status.activeRequestCount,0);assert.equal(status.maxConcurrentRequests,3);
});

test("shutdown waits for an in-flight ask to release the shared lock",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  const task=b.ask({prompt:"in-flight"});
  await new Promise(resolve=>setTimeout(resolve,5));const stopped=b.stopBrowser();
  assert.match((await task).answer,/in-flight/);assert.deepEqual(await stopped,{stopped:true,pid:f.pid});
});

test("model mismatch prevents submission and keeps browser available",async t=>{
  const f=await fixture(t);f.modelOK=false;const b=new TaskBridge({runtime:f.runtime()});
  await assert.rejects(()=>b.ask({prompt:"must not send"}),e=>e.code==="MODEL_MISMATCH");
  assert.equal(f.sends,0);assert.equal(f.closes,0);
});

test("pre-submit quota preserves routing evidence and releases its task slot without sending", async t => {
  const f = await fixture(t);
  f.modelResult = { ok: false, code: "WEB_QUOTA_EXHAUSTED", message: "用量限额重置时间：20:22",
    quota: { scope: "web", exhausted_models: ["4 Argon", "4 Hydrogen", "3.8 Flash"] } };
  const b = new TaskBridge({ runtime: f.runtime() });
  const { fallbackFor } = await import("../src/fallback-policy.mjs");
  await assert.rejects(() => b.ask({ prompt: "synthetic quota probe" }), error => {
    assert.equal(error.code, "WEB_QUOTA_EXHAUSTED");
    assert.equal(error.phase, "PRE_SUBMIT");
    assert.equal(error.quota.scope, "web");
    assert.equal(fallbackFor(error).model, null);
    return true;
  });
  assert.equal(f.sends, 0);
  assert.equal((await b.browserStatus()).activeRequestCount, 0);
  assert.equal(f.closes, 0);
});

test("post-submit disconnect archives uncertainty and independent requests continue without replay",async t=>{
  const f=await fixture(t);f.disconnectSubmit=true;const b=new TaskBridge({runtime:f.runtime()});
  let requestId;
  await assert.rejects(()=>b.ask({prompt:"single attempt"}),e=>{requestId=e.requestId;return e.code==="OUTCOME_UNKNOWN";});
  assert.equal(f.sends,1);assert.equal(f.launches,1);assert.equal(f.closes,0);assert.equal(f.cancel,1);
  const status=await b.browserStatus();
  assert.equal(status.pendingRequest,null);assert.equal(status.lastRecovery.outcome,"DETACHED_GENERATING");
  const preserved=status.lastRecovery.targetId;
  f.disconnectSubmit=false;
  const reconnected=new TaskBridge({runtime:f.runtime()});
  assert.equal((await reconnected.ask({prompt:"another request"})).answer,"another request");
  assert.equal(f.sends,2);assert.deepEqual(f.prompts,["single attempt","another request"]);
  assert.equal(f.pages.length,3);assert.ok(f.pages.some(p=>p.id===preserved));
  const record=await b.recoverRequest({requestId});assert.equal(record.requestId,requestId);
});

test("interrupted submitted request is archived before a dead browser restarts",async t=>{
  const f=await fixture(t),r=f.runtime();await r.start();
  await r.markRequest({requestId:"pending",phase:"SUBMITTED",conversationUrl:"https://gemini.google.com/app/fixture"});f.alive=false;
  await r.start();assert.equal(f.launches,2);assert.equal((await r.read()).inFlight,undefined);
  assert.equal((await r.recoveryHistory())[0].reason,"browser-gone");
});

test("cancellation during model verification prevents submission",async t=>{
  const f=await fixture(t),controller=new AbortController(),runtime=f.runtime();
  const connect=runtime.connect;
  runtime.connect=async url=>{const c=await connect(url);const call=c.call;c.call=async function(fn,args){const result=await call.call(this,fn,args);if(fn.includes("function ensureGeminiModel"))controller.abort();return result;};return c;};
  const b=new TaskBridge({runtime});
  await assert.rejects(()=>b.ask({prompt:"cancelled",signal:controller.signal}),e=>e.code==="CANCELLED");
  assert.equal(f.sends,0);assert.equal((await runtime.status()).pendingRequest,null);
});

test("visible login transition stops owned headless runtime first",async t=>{
  const f=await fixture(t),b=new GeminiBrowserBridge({runtime:f.runtime()});await b.startBrowser();
  // Test the transition boundary without launching a real visible window.
  b.findBrowser=async()=>null;
  await assert.rejects(()=>b.spawnLoginBrowser(),/No compatible browser/);
  assert.equal(f.closes,1);assert.equal(await b.runtime.read(),null);
});

test("unconfirmed attachment upload is preserved as a draft and frees later independent requests", async t => {
  const f = await fixture(t), bridge = new GeminiBrowserBridge({ runtime: f.runtime() });
  bridge.waitForComposer = async () => {};
  let uploads = 0;
  bridge.uploadAttachments = async () => {
    uploads++;
    throw new GeminiBridgeError("OUTCOME_UNKNOWN", "upload result unknown", { phase: "UPLOADING" });
  };
  const file = join(f.root, "context.txt"); await writeFile(file, "synthetic context");
  await assert.rejects(() => bridge.ask({ prompt: "test", filePaths: [file] }), e => e.code === "OUTCOME_UNKNOWN" && e.phase === "UPLOADING");
  assert.equal(uploads, 1); assert.equal(f.sends, 0);
  assert.equal((await bridge.runtime.read()).inFlight, undefined);
  assert.equal((await bridge.runtime.recoveryHistory())[0].outcome, "UPLOAD_ABANDONED");
  const next = new TaskBridge({ runtime: f.runtime() });
  assert.equal((await next.ask({ prompt: "another" })).answer, "another");
  assert.equal(uploads, 1); assert.equal(f.sends, 1);
});

test("model page function selects Argon for fresh chats and refuses wrong-model continuation",async()=>{
  let mode="Flash",opened=false;
  const control={getClientRects:()=>[1],getAttribute:()=>"Open model picker",get innerText(){return mode;},click(){opened=true;}};
  const option={getClientRects:()=>[1],innerText:"4 Argon Complex tasks",click(){mode="Argon";}};
  const fn=runInNewContext(`(${ensureGeminiArgon.toString()})`,{
    document:{querySelectorAll:selector=>selector.includes("aria-haspopup")?[control]:(opened?[option]:[])},
    getComputedStyle:()=>({visibility:"visible"}),setTimeout,Date,
  });
  assert.equal((await fn(true)).code,"MODEL_MISMATCH");assert.equal(mode,"Flash");
  assert.equal((await fn(false)).model,"4 Argon");assert.equal(mode,"Argon");
});

test("model verification waits for a selector that hydrates after the composer",async()=>{
  let observations=0;
  const control={getClientRects:()=>[1],getAttribute:()=>"打开模式选择器，当前模式为“Argon”",innerText:"Argon"};
  const fn=runInNewContext(`(${ensureGeminiArgon.toString()})`,{
    document:{querySelectorAll:()=>++observations<3?[]:[control]},
    getComputedStyle:()=>({visibility:"visible"}),setTimeout:callback=>queueMicrotask(callback),Date,
  });
  assert.equal((await fn(false)).model,"4 Argon");
  assert.equal(observations,3);
});


test("legacy marker with a live MCP owner is inspected and detached while preserving its page",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  const old=(await r.read()).targetId;
  await r.markRequest({requestId:"legacy",phase:"SUBMITTED",conversationUrl:"https://gemini.google.com/app"});
  const record=await r.recoverPending();
  assert.equal(record.outcome,"SETTLED_UNKNOWN");assert.equal(record.targetId,old);
  assert.equal((await r.read()).inFlight,undefined);
  const next=await r.openTarget(port);assert.notEqual(next.target.id,old);
  assert.equal(f.pages.length,2);assert.equal(f.sends,0);assert.equal(f.cancel,0);
});

test("completion evidence releases a request and stores no prompt or answer text",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"complete",phase:"SUBMITTED",before:{count:0,completedCount:0}});
  f.generation={url:"https://gemini.google.com/app/completed",snapshot:{text:"private answer",count:1,completedCount:1},isNew:true,stopVisible:false};
  const record=await r.recoverPending();assert.equal(record.outcome,"COMPLETED");
  await r.assertConversationAvailable(record.conversationUrl);
  const source=await readFile(join(f.root,"recovered-requests.json"),"utf8");
  assert.ok(!source.includes("private answer"));assert.equal(record.partialChars,14);
});

test("unavailable page is quarantined and same-conversation continuation stays guarded",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"lost",phase:"SUBMITTED",conversationUrl:"https://gemini.google.com/app/lost"});
  f.sends=1;f.probeFails=true;
  const record=await r.recoverPending();assert.equal(record.outcome,"OUTCOME_UNKNOWN");
  await assert.rejects(()=>r.assertConversationAvailable(record.conversationUrl),e=>e.code==="OUTCOME_UNKNOWN"&&e.requestId==="lost");
  await r.assertConversationAvailable("https://gemini.google.com/app");
  f.probeFails=false;
  assert.equal((await new TaskBridge({runtime:r}).ask({prompt:"independent"})).answer,"independent");
});

test("rate limits, cancellation and generation timeouts release their request slot",async t=>{
  for(const code of ["RATE_LIMITED","CANCELLED","GENERATION_TIMEOUT","RESPONSE_STALLED","NO_RESPONSE"]){
    const f=await fixture(t),r=f.runtime(),b=new TaskBridge({runtime:r});
    b.waitForAnswer=async()=>{throw new GeminiBridgeError(code,"fixture failure",{phase:"SUBMITTED"});};
    await assert.rejects(()=>b.ask({prompt:code}),e=>e.code===code);
    assert.equal((await r.status()).pendingRequest,null);
    assert.equal((await new TaskBridge({runtime:r}).ask({prompt:"next"})).answer,"next");
    assert.equal(f.sends,2);assert.equal(f.closes,0);
  }
});

test("recovery waits for an active ask before examining its request marker",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  let entered,release;
  const started=new Promise(resolve=>{entered=resolve;});
  const gate=new Promise(resolve=>{release=resolve;});
  b.waitForAnswer=async client=>{entered();await gate;return {answer:client.prompt,conversationUrl:"https://gemini.google.com/app/fixture"};};
  const answer=b.ask({prompt:"active"});await started;
  let recovered=false;
  const recovery=b.recoverRequest().then(value=>{recovered=true;return value;});
  await new Promise(resolve=>setTimeout(resolve,15));
  assert.equal(recovered,false);assert.equal((await b.browserStatus()).activeRequestCount,1);
  release();assert.equal((await answer).answer,"active");assert.equal(await recovery,null);
});

test("recovery archive write failure retains the marker for a later recovery",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"disk-failure",phase:"SUBMITTED"});
  r.saveRecovery=async()=>{throw new Error("disk full");};
  await assert.rejects(()=>r.recoverPending(),/disk full/);
  assert.equal((await r.read()).inFlight.requestId,"disk-failure");
});


test("recovery keeps confirmed terminal evidence after its browser disappears",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"terminal",phase:"SUBMITTED",before:{count:0,completedCount:0}});
  f.generation={url:"https://gemini.google.com/app/terminal",snapshot:{text:"answer",count:1,completedCount:1},isNew:true,stopVisible:false};
  assert.equal((await r.recoverPending()).outcome,"COMPLETED");
  f.alive=false;
  assert.equal((await r.recoverPending("terminal")).outcome,"COMPLETED");
  await r.assertConversationAvailable("https://gemini.google.com/app/terminal");
});

test("a malformed pending provider errors explicitly and keeps the request marker",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();
  const opened=await r.openTarget(port);opened.client.close();
  await r.markRequest({requestId:"bad-provider",provider:"openai",phase:"SUBMITTED",
    conversationUrl:"https://aistudio.google.com/prompts/bad-provider"});
  await assert.rejects(()=>r.recoverPending(),error=>error.code==="INVALID_INPUT"&&/provider/.test(error.message));
  const state=await r.read();
  assert.equal(state.inFlight.requestId,"bad-provider");
  assert.equal(state.inFlight.provider,"openai");
});

test("AI Studio pending requests recover through the AI Studio adapter and preserve billing evidence",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();
  const opened=await r.openTarget(port);opened.client.close();
  f.pages[0].url="https://aistudio.google.com/prompts/studio-pending";
  await r.markRequest({requestId:"ai-pending",provider:"aistudio",phase:"SUBMITTED",
    conversationUrl:"https://aistudio.google.com/prompts/studio-pending",before:{count:0,completedCount:0}});
  f.aistudioGeneration={provider:"aistudio",complete:true,url:"https://aistudio.google.com/prompts/studio-pending",
    snapshot:{text:"private ai answer",count:1,completedCount:1,latestTurnId:"ai-turn"},isNew:true,stopVisible:false,failure:null,
    billing:{apiKeySelected:false,evidence:[{kind:"pro_badge",text:"PRO"}],mode:"subscription",pro:true}};
  const record=await r.recoverPending();
  assert.equal(record.provider,"aistudio");assert.equal(record.outcome,"COMPLETED");
  assert.equal(record.billing.mode,"subscription");assert.equal(f.aistudioProbes,1);assert.equal(f.geminiProbes,0);
  const history=JSON.parse(await readFile(join(f.root,"recovered-requests.json"),"utf8"));
  assert.equal(history.at(-1).provider,"aistudio");assert.ok(!JSON.stringify(history).includes("private ai answer"));
  await r.assertConversationAvailable(record.conversationUrl);
});

test("AI Studio recovery records a billing guard failure without resubmitting",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();
  const opened=await r.openTarget(port);opened.client.close();
  f.pages[0].url="https://aistudio.google.com/prompts/studio-billing";
  await r.markRequest({requestId:"ai-billing",provider:"aistudio",phase:"SUBMITTED",
    conversationUrl:"https://aistudio.google.com/prompts/studio-billing",before:{count:0,completedCount:0}});
  f.aistudioGeneration={provider:"aistudio",url:"https://aistudio.google.com/prompts/studio-billing",
    snapshot:{text:"partial",count:1,completedCount:0},isNew:true,stopVisible:false,
    failure:{kind:"PAID_KEY_ACTIVE",text:"Using API key"}};
  const record=await r.recoverPending();
  assert.equal(record.provider,"aistudio");assert.equal(record.outcome,"FAILED");
  assert.equal(record.failure,"PAID_KEY_ACTIVE");assert.equal(f.sends,0);assert.equal(f.cancel,0);
});

test("AI Studio failure on the new-chat route preserves target metadata without inventing a conversation URL",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();
  const opened=await r.openTarget(port);opened.client.close();
  const home="https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon";
  f.pages[0].url=home;
  await r.markRequest({requestId:"ai-home-failure",provider:"aistudio",phase:"SUBMITTED",
    conversationUrl:home,before:{count:0,completedCount:0,latestTurnId:null}});
  f.aistudioGeneration={provider:"aistudio",url:home,
    snapshot:{count:1,completedCount:1,latestTurnId:"denied",text:""},isNew:false,stopVisible:false,
    failure:{kind:"PERMISSION_DENIED",text:"权限遭拒。请检查您的账号权限和项目设置。"},
    billing:{apiKeySelected:false,evidence:[],mode:"subscription",pro:true}};
  const record=await r.recoverPending();
  assert.equal(record.outcome,"FAILED");assert.equal(record.failure,"PERMISSION_DENIED");
  assert.equal(record.provider,"aistudio");assert.equal(record.conversationUrl,home);
  assert.equal(record.targetId,opened.target.id);assert.ok(record.browserSocket);
  await r.assertConversationAvailable(home);
});

test("missing recovery target releases its slot without closing another page",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"missing",phase:"SUBMITTED"});f.pages=[];
  assert.equal((await r.recoverPending()).reason,"target-gone");
  assert.equal((await new TaskBridge({runtime:r}).ask({prompt:"next"})).answer,"next");
  assert.equal(f.closes,0);
});


test("same-conversation follow-ups serialize even through handles with different URL parameters",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  const first=await b.ask({prompt:"first"});b.maxActive=0;
  const alias=await saveConversation({conversationUrl:"https://gemini.google.com/app/fixture?source=alias#part",model:"4 Argon"});
  const results=await Promise.all([first.conversationId,alias].map((conversationId,i)=>b.ask({prompt:i?"third":"second",conversationId})));
  assert.equal(b.maxActive,1);assert.deepEqual(results.map(x=>x.answer),["second","third"]);
});

test("different bridge instances share the same three task slots",async t=>{
  const f=await fixture(t),bridges=Array.from({length:5},()=>new TaskBridge({runtime:f.runtime()}));
  let active=0,maxActive=0;
  for(const b of bridges){const run=b.runAttempt;b.runAttempt=async function(...args){active++;maxActive=Math.max(maxActive,active);try{await new Promise(resolve=>setTimeout(resolve,150));return await run.apply(this,args);}finally{active--;}};}
  await Promise.all(bridges.map((b,i)=>b.ask({prompt:String(i)})));
  assert.equal(maxActive,3);assert.equal(f.launches,1);
});

test("legacy generating request reserves one of the three slots during migration",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"legacy-running",phase:"SUBMITTED"});
  f.generation={url:"https://gemini.google.com/app/legacy",snapshot:{text:"partial",count:1,completedCount:0},isNew:true,stopVisible:true};
  const b=new TaskBridge({runtime:r});await b.pool.prepare();
  assert.equal((await r.taskRuntime(1).read()).quarantined.requestId,"legacy-running");
  const results=await Promise.all([b.ask({prompt:"a"}),b.ask({prompt:"b"})]);
  assert.deepEqual(results.map(x=>x.answer),["a","b"]);
  assert.equal((await r.taskRuntime(1).read()).quarantined.requestId,"legacy-running");
  assert.equal((await r.read()).version,3);
});

test("one failed task preserves other active slots and their answers",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  let entered=0,allEntered;
  const started=new Promise(resolve=>{allEntered=resolve;});
  b.waitForAnswer=async client=>{
    entered++;if(entered===3)allEntered();await started;
    if(client.prompt==="failure")throw new GeminiBridgeError("GENERATION_TIMEOUT","fixture failure",{phase:"SUBMITTED"});
    await new Promise(resolve=>setTimeout(resolve,80));
    return {answer:client.prompt,conversationUrl:"https://gemini.google.com/app/fixture"};
  };
  const outcomes=await Promise.allSettled(["first","failure","third"].map(prompt=>b.ask({prompt})));
  assert.equal(outcomes[0].value.answer,"first");assert.equal(outcomes[1].reason.code,"GENERATION_TIMEOUT");
  assert.equal(outcomes[2].value.answer,"third");assert.equal(f.cancel,1);assert.equal(f.closes,0);
  assert.equal((await b.browserStatus()).activeRequestCount,0);
});

test("a queued fourth task can be cancelled without touching three running tasks",async t=>{
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  let entered=0,allEntered,release;
  const started=new Promise(resolve=>{allEntered=resolve;}),gate=new Promise(resolve=>{release=resolve;});
  b.waitForAnswer=async client=>{entered++;if(entered===3)allEntered();await gate;return {answer:client.prompt,conversationUrl:"https://gemini.google.com/app/fixture"};};
  const first=Promise.all(["one","two","three"].map(prompt=>b.ask({prompt})));await started;
  const controller=new AbortController();const queued=b.ask({prompt:"four",signal:controller.signal});
  controller.abort();await assert.rejects(()=>queued,e=>e.code==="CANCELLED");
  assert.equal((await b.browserStatus()).activeRequestCount,3);release();
  assert.deepEqual((await first).map(x=>x.answer),["one","two","three"]);assert.equal(f.sends,3);assert.equal(f.cancel,0);
});

test("migration accounts for generating pages already archived by the old bridge",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"old-archive",phase:"SUBMITTED"});
  f.generation={url:"https://gemini.google.com/app/old",snapshot:{text:"partial",count:1,completedCount:0},isNew:true,stopVisible:true};
  await r.recoverPending();
  const legacy=await r.read();delete legacy.poolVersion;legacy.version=1;
  await writeFile(r.file,JSON.stringify(legacy));
  const b=new TaskBridge({runtime:r});await b.pool.prepare();
  assert.equal((await r.taskRuntime(1).read()).quarantined.requestId,"old-archive");
  assert.equal((await r.read()).poolVersion,1);
});

test("simultaneous first calls migrate once and then occupy three slots",async t=>{
  const f=await fixture(t),r=f.runtime();await r.start();
  const legacy=await r.read();delete legacy.poolVersion;legacy.version=1;
  await writeFile(r.file,JSON.stringify(legacy));
  const b=new TaskBridge({runtime:r});let active=0,maximum=0;
  const run=b.runAttempt;
  b.runAttempt=async function(...args){active++;maximum=Math.max(maximum,active);try{await new Promise(resolve=>setTimeout(resolve,150));return await run.apply(this,args);}finally{active--;}};
  await Promise.all(["a","b","c"].map(prompt=>b.ask({prompt})));
  assert.equal(maximum,3);assert.equal((await r.read()).poolVersion,1);
});

test("a fresh chat waits for the home route before accepting an old visible composer",async t=>{
  const f=await fixture(t),b=new GeminiBrowserBridge({runtime:f.runtime()});let calls=0;
  const client={call:async()=>({composerReady:true,signedOut:false,url:++calls<=4?"https://gemini.google.com/app/old":"https://gemini.google.com/app"})};
  const page=await b.waitForComposer(client,null,null,"https://gemini.google.com/app");
  assert.equal(page.url,"https://gemini.google.com/app");assert.equal(calls,8);
});

test("AI Studio polling does not accept partial text without a completed footer",async t=>{
  const f=await fixture(t),b=new GeminiBrowserBridge({runtime:f.runtime()});
  const probeDone=new Error("probe completed without accepting partial text");let calls=0;
  const client={call:async()=>{
    if(++calls===8)throw probeDone;
    return {isNew:true,complete:false,stopVisible:false,snapshot:{count:1,completedCount:1,text:"partial"}};
  }};
  await assert.rejects(()=>b.waitForAnswer(client,{count:0,completedCount:0},null,null,
    {provider:"aistudio",model:"4 Argon"}),error=>error===probeDone);
  assert.equal(calls,8);
});

test("AI Studio recovery requires completion evidence beyond a new model turn",async t=>{
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  await r.markRequest({requestId:"ai-partial",provider:"aistudio",phase:"SUBMITTED",before:{count:0,completedCount:0}});
  f.aistudioGeneration={provider:"aistudio",isNew:true,complete:false,stopVisible:false,
    snapshot:{count:1,completedCount:1,text:"partial"},url:"https://aistudio.google.com/prompts/partial"};
  assert.equal((await r.recoverPending()).outcome,"SETTLED_UNKNOWN");
  f.aistudioGeneration.complete=true;
  assert.equal((await r.recoverPending("ai-partial")).outcome,"COMPLETED");
  assert.equal(f.closes,0);
});

test("v2 provider-state upgrade waits for old task holders and preserves quarantined pages",async t=>{
  const {acquireFileLock}=await import("../src/operation-lock.mjs");
  const f=await fixture(t),r=f.runtime(),port=await r.start();await r.openTarget(port);
  const legacy={...await r.read(),version:2,poolVersion:1};
  await writeFile(r.file,JSON.stringify(legacy));
  const task=r.taskRuntime(1),quarantined={requestId:"preserved-ai",provider:"aistudio",phase:"SUBMITTED"};
  await writeFile(task.file,JSON.stringify({...legacy,quarantined}));
  const b=new GeminiBrowserBridge({runtime:r});
  const release=await acquireFileLock(b.pool.slotLock(2),{});
  let done=false;const upgrade=b.pool.prepare().then(()=>{done=true;});
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.equal(done,false);assert.equal((await r.read()).version,2);
  await release();await upgrade;
  const migrated=await r.read();assert.equal(migrated.version,3);
  assert.equal(migrated.browserSocket,legacy.browserSocket);assert.equal(migrated.targetId,legacy.targetId);
  assert.deepEqual((await task.read()).quarantined,quarantined);
  assert.equal(f.launches,1);assert.equal(f.closes,0);
});


test("AI Studio uploads actual selected bytes and waits for processing before returning", async t => {
  const f = await fixture(t);
  const file = join(f.root, "context.txt"); await writeFile(file, "selected context");
  const {validateAttachments} = await import("../src/gemini-upload.mjs");
  const files = await validateAttachments([file]);
  const expected = [{name:"context.txt",size:16}];
  let polls = 0; const selected = [];
  const client = {
    call: async (declaration,args) => {
      assert.deepEqual(args, [expected]);
      if (declaration.includes("function prepareAistudioUpload")) return {ok:true,groups:[{index:0,names:["context.txt"]}]};
      assert.match(declaration,/function readAistudioUploadState/);
      polls++;
      return {ready:polls >= 3,busy:polls < 3,failure:null,attachments:expected};
    },
    send: async (method,args) => {
      if (method === "Runtime.evaluate") return {result:{objectId:"file-input"}};
      if (method === "DOM.setFileInputFiles") selected.push(...args.files);
      return {};
    },
  };
  await new GeminiBrowserBridge({runtime:{}}).uploadAttachments(client,files,null,"aistudio");
  assert.deepEqual(selected,[files[0].path]); assert.equal(polls,4);
});

test("AI Studio upload preserves interaction errors and partial receipts", async t => {
  const f = await fixture(t);
  const file = join(f.root, "context.txt"); await writeFile(file, "selected context");
  const {validateAttachments} = await import("../src/gemini-upload.mjs");
  const files = await validateAttachments([file]);
  const upload = {ready:false,busy:false,failure:"Review upload acknowledgement",code:"INTERACTION_REQUIRED",
    partial:true,attachments:[{name:"context.txt",size:16}],missing:["other.pdf"]};
  const client = {
    call: async declaration => declaration.includes("function prepareAistudioUpload")
      ? {ok:true,groups:[{index:0,names:["context.txt"]}]} : upload,
    send: async method => method === "Runtime.evaluate" ? {result:{objectId:"file-input"}} : {},
  };
  await assert.rejects(()=>new GeminiBrowserBridge({runtime:{}}).uploadAttachments(client,files,null,"aistudio"), error=>{
    assert.equal(error.code,"INTERACTION_REQUIRED");assert.equal(error.phase,"UPLOADING");
    assert.equal(error.upload,upload);assert.equal(error.retrySafe,false);return true;
  });
});


test("AI Studio login opens and verifies the requested provider in the dedicated profile", async t => {
  const f = await fixture(t); const bridge = new GeminiBrowserBridge({runtime:f.runtime()});
  let requested;
  bridge.spawnLoginBrowser = async (_signal,provider) => { requested = provider; return {unref(){}}; };
  const opened = await bridge.launchHumanLogin({provider:"aistudio",wait:false});
  assert.equal(requested,"aistudio");assert.equal(opened.provider,"aistudio");
  let verified;
  bridge.waitForComposer = async (_client,_progress,_signal,url,provider)=>{verified={url,provider};};
  assert.equal(await bridge.verifyLogin(null,null,"aistudio"),true);
  assert.equal(verified.provider,"aistudio");
  assert.equal(verified.url,"https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon");
  assert.deepEqual(f.navigations,[verified.url]);
});

 test("AI Studio complete answers survive an unsaved new-chat URL", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  b.waitForAnswer=async()=>({answer:"OK",conversationUrl:"https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon"});
  const result=await b.ask({prompt:"reply OK",provider:"aistudio"});
  assert.equal(result.answer,"OK");
  assert.equal(result.conversationId,null);
  assert.equal(f.aistudioSends,1);
  assert.equal((await b.runtime.status()).pendingRequest,null);
});

test("AI Studio upgrades an idle headless runtime while preserving Gemini results", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  await b.ask({prompt:"gemini first"});
  assert.equal((await b.browserStatus()).runtimeMode,"headless-resident");
  b.waitForAnswer=async()=>({answer:"OK",conversationUrl:"https://aistudio.google.com/prompts/new_chat"});
  await b.ask({prompt:"ai next",provider:"aistudio"});
  assert.equal(f.closes,1);assert.equal(f.launches,2);
  assert.equal((await b.browserStatus()).runtimeMode,"headed-resident");
});

test("AI Studio mode upgrade waits for an active Gemini task to finish", async t => {
  const f=await fixture(t),b=new TaskBridge({runtime:f.runtime()});
  let release, started;
  const gate=new Promise(resolve=>{release=resolve;});
  const entered=new Promise(resolve=>{started=resolve;});
  b.waitForAnswer=async(_client,_before,_progress,_signal,context)=>{
    if(context.provider==="gemini") {started();await gate;}
    return {answer:"OK",conversationUrl:context.provider==="gemini" ? "https://gemini.google.com/app/fixture" : "https://aistudio.google.com/prompts/new_chat"};
  };
  const first=b.ask({prompt:"active Gemini"});
  await entered;
  const second=b.ask({prompt:"queued AI Studio",provider:"aistudio"});
  await new Promise(resolve=>setTimeout(resolve,80));
  assert.equal(f.closes,0);assert.equal(f.launches,1);
  release();
  const results=await Promise.all([first,second]);
  assert.equal(results[0].answer,"OK");assert.equal(results[1].answer,"OK");
  assert.equal(f.closes,1);assert.equal(f.launches,2);
});
