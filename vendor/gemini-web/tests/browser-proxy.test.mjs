import assert from "node:assert/strict";
import test from "node:test";
import { browserProxyArgs } from "../src/browser-proxy.mjs";

test("proxy is opt-in and never inferred from unrelated environment", () => {
  assert.deepEqual(browserProxyArgs({ HTTPS_PROXY: "http://localhost:1234" }), []);
});
test("explicit direct mode overrides the operating system proxy", () => {
  assert.deepEqual(browserProxyArgs({ GEMINI_WEB_BRIDGE_PROXY: "direct", HTTPS_PROXY: "http://localhost:10808" }), ["--no-proxy-server"]);
  assert.deepEqual(browserProxyArgs({ GEMINI_WEB_BRIDGE_PROXY: "" }), []);
});
test("supported proxy URL produces one Chrome argument", () => {
  assert.deepEqual(browserProxyArgs({ GEMINI_WEB_BRIDGE_PROXY: "http://127.0.0.1:10808" }), ["--proxy-server=http://127.0.0.1:10808"]);
  assert.deepEqual(browserProxyArgs({ GEMINI_WEB_BRIDGE_PROXY: "socks5://localhost:10808" }), ["--proxy-server=socks5://localhost:10808"]);
});
test("invalid or credentialed proxy URL is rejected without reflecting secrets", () => {
  for (const value of ["bad", "file:///tmp/x", "http://user:secret@localhost:8080", "http://localhost:8080/path", "http://localhost:8080/?token=secret"]) {
    assert.throws(() => browserProxyArgs({ GEMINI_WEB_BRIDGE_PROXY: value }), error => !error.message.includes("secret"));
  }
});
