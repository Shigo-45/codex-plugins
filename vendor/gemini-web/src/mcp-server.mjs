#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { GeminiBrowserBridge, MAX_PROMPT_CHARS, normalizeBridgeError } from "./gemini-browser.mjs";
import { authorizationStatus, authorize, listConversations } from "./state-store.mjs";
import { canonicalizeYoutubeUrl } from "./youtube.mjs";
import { fallbackFor } from "./fallback-policy.mjs";

const bridge = new GeminiBrowserBridge();
const ownerThreadId = process.env.CODEX_THREAD_ID ?? null;

const server = new McpServer(
  { name: "gemini-web-bridge", version: "0.2.1" },
  {
    instructions:
      "Use `gemini_web_status` first to inspect session readiness, `providers`, `supportedModels` (Argon, Carbon, Neon, Flash, plus legacy Hydrogen), `attachments` limits (`enabled`, `maxFiles`, `maxFileBytes`), and any `pendingRequest` (including `phase: \"UPLOADING\"`). When calling `gemini_web_ask`, pass `provider: \"aistudio\"` for AI Studio Web (`\"4 Argon\"`/`gemini-4-argon`, `\"4 Hydrogen\"`/`gemini-4-hydrogen`, or `\"3.8 Flash\"`/`gemini-3.8-flash`, with per-submit PRO + `No API key selected` billing verification) or omit `provider` for legacy Gemini Web. New Gemini conversations default to `\"4 Argon\"`; omit `model` on `conversation_id` follow-ups to inherit the saved provider and model, and start a new conversation whenever switching either because silent fallbacks are rejected (`Flash-Lite` and `Pro` do not match `\"3.8 Flash\"`). AI Studio paid-key billing and unverified models fail closed before submission.\n\nFor either Web provider, pass `file_paths` only for explicit absolute paths to task-necessary, authorized regular files (up to 10 non-empty files, max 100,000,000 bytes each; never pass credentials, directories, full vaults, or duplicate filenames, and remember uploaded files are sent to Google). AI Studio uses a dedicated headed Chrome window launched minimized, with a nonzero localhost debugging port and native text/mouse input; CDP work stays in the background and the bridge waits for active tasks before a mode upgrade. AI Studio uploads selected files through its native file input and waits for processing before Run. A completed answer may return conversation_id=null while AI Studio still has a new_chat URL; start a new request rather than resuming a null ID. Set `close_after=true` for one-shot AI Studio work after a confirmed result, and keep it false for conversation follow-ups. If an upload or generation outcome is unknown or `pendingRequest` is present, call `gemini_web_recover` to inspect and preserve its page and release the stale task slot. Independent new requests recover their acquired slot automatically. Three cross-process slots are shared by MCP and CLI; independent conversations use separate pages, excess requests wait, and follow-ups to one conversation serialize. Check concurrencySupported and maxConcurrentRequests=3; use the updated CLI if an existing MCP process lacks these capabilities. Still-generating or temporarily uninspectable requests retain their slot while other slots continue. Use request_id to inspect an archived request again; unresolved conversations remain guarded. Never replay the failed request automatically or use shutdown to clear its state. Check quotaFallbackSupported=true; use the updated CLI when an existing MCP process lacks it. Disabled model options may contain usage-limit reset evidence. RATE_LIMITED preserves model quota evidence; WEB_QUOTA_EXHAUSTED confirms all supported Web models are exhausted. For an authorized write-to-me workflow, automatically continue with the structured fallback route (4 Argon High → 4 Carbon High → 4 Neon xHigh → 3.8 Flash High within CLI → API → AI Studio → Web), preserve the selected context, and report the quota reason and actual model. Settle submitted requests before fallback.",
  },
);

const executionOutputSchema = {
  model: z.string().optional(),
  model_id: z.string().nullable().optional(),
  provider: z.enum(["gemini", "aistudio"]).optional(),
  billing: z.record(z.unknown()).nullable().optional(),
  billing_mode: z.string().optional(),
  conversation_id: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  partial_chars: z.number().int().nonnegative().optional(),
  phase: z.enum(["PRE_SUBMIT", "UPLOADING", "SUBMITTED", "GENERATING"]).nullable().optional(),
  attachments: z.array(z.object({ name: z.string(), size: z.number() })).optional(),
  browser_closed: z.boolean().optional(),
  request_id: z.string().nullable().optional(),
  retry_safe: z.boolean().optional(),
  recovery: z.record(z.unknown()).nullable().optional(),
  quota: z.record(z.unknown()).nullable().optional(),
  upload: z.record(z.unknown()).nullable().optional(),
  fallback: z.record(z.unknown()).nullable().optional(),
  reasoning_effort: z.enum(["high", "xhigh"]).nullable().optional(),
  status: z.enum(["completed", "error"]),
};

// Cross-process slot and conversation locks in the bridge coordinate requests.
function enqueue(operation) { return operation(); }

function textResult(value, structuredContent = undefined, isError = false) {
  return {
    content: [{ type: "text", text: value }],
    ...(structuredContent == null ? {} : { structuredContent }),
    isError,
  };
}

function errorResult(error) {
  const code = typeof error?.code === "string" ? error.code : "UNEXPECTED";
  const recovery = {
    BRIDGE_BUSY: "Wait for the active Gemini operation to finish, then retry if still useful.",
    CANCELLED: "The operation was cancelled; generation was stopped where possible and the resident browser was retained.",
    CONVERSATION_NOT_FOUND: "Start a fresh Gemini conversation or list known conversations.",
    INTERACTION_REQUIRED: "Open the dedicated login window, complete verification, close it, then decide whether to retry.",
    LOGIN_REQUIRED: "Open the dedicated login window, sign in, close it, then retry.",
    OUTCOME_UNKNOWN: "Do not blindly resend. Inspect or start a fresh conversation only if Codex judges it useful.",
    RATE_LIMITED: "Continue the authorized writing workflow using the structured fallback route. Settle any submitted request first; retain the quota evidence and report the route change.",
    WEB_QUOTA_EXHAUSTED: "The supported Web models reached their usage limit. Preserve the receipts and report exhaustion of CLI → API → AI Studio → Web routes.",
    UI_CHANGED: "Update Gemini Web Bridge; Gemini's page structure may have changed.",
    BROWSER_OWNERSHIP: "The browser identity could not be verified. Do not close or attach to an unrelated browser; inspect the dedicated runtime state.",
    BROWSER_CONFIG_CHANGED: "Call gemini_web_shutdown explicitly, then start a new request with the updated proxy configuration.",
    MODEL_UNAVAILABLE: "The requested model was not verified. The prompt was not submitted; inspect model availability.",
    MODEL_MISMATCH: "Start a fresh conversation to change models/providers; the existing conversation was not submitted to another model.",
    PROVIDER_MISMATCH: "This local conversation belongs to another Web provider. Start a fresh conversation for the requested provider.",
    BILLING_UNVERIFIED: "AI Studio did not show both a visible PRO subscription badge and \"No API key selected\". The prompt/result was rejected; do not retry with paid-key billing.",
    PAID_KEY_ACTIVE: "AI Studio is using an API key instead of the verified Pro subscription. The prompt/result was rejected; do not retry until \"No API key selected\" is restored.",
    API_KEY_REQUIRED: "AI Studio requested API-key billing. The prompt/result was rejected; no paid-key request was made.",
    PERMISSION_DENIED: "AI Studio returned a permission-denied model turn after submission. Do not retry or replay blindly; check the account/project permissions and report the environment blocker.",
    ACCESS_DENIED: "AI Studio returned an access-denied model turn after submission. Do not retry or replay blindly; check the account/project permissions and report the environment blocker.",
    MODEL_ERROR: "AI Studio returned an explicit model-error turn after submission. Inspect the reported error; do not treat it as success or replay blindly.",
    UPLOAD_FAILED: "An attachment failed to upload. The prompt was not submitted; inspect the draft and file before retrying.",
    UPLOAD_UNSUPPORTED: "A selected file type or upload control is unavailable or not verified. The prompt was not submitted.",
  }[code] ?? (
    error?.retrySafe
      ? "The prompt was not submitted; one deliberate retry is safe."
      : "Do not blindly resubmit. Codex should decide whether a follow-up or fresh conversation is appropriate."
  );
  return textResult(
    [`Gemini Web operation failed [${code}]: ${error?.message ?? "Unknown error"}`, `Recovery: ${recovery}`].join("\n"),
    {
      billing: error?.billing ?? null,
      conversation_id: error?.conversationId ?? null,
      error_code: code,
      partial_chars: error?.partialChars ?? 0,
      phase: error?.phase ?? null,
      provider: error?.provider ?? "gemini",
      request_id: error?.requestId ?? null,
      recovery: error?.recovery ?? null,
      upload: error?.upload ?? null,
      quota: error?.quota ?? null,
      fallback: fallbackFor(error),
      retry_safe: error?.retrySafe === true,
      status: "error",
    },
    true,
  );
}

async function notify(extra, message, progress) {
  const progressToken = extra._meta?.progressToken;
  if (progressToken === undefined) return;
  await extra.sendNotification({
    method: "notifications/progress",
    params: { progressToken, progress, total: 100, message },
  });
}

async function requireAuthorization() {
  const authorization = await authorizationStatus();
  if (authorization.authorized) return null;
  return textResult(
    "Using Gemini Web or AI Studio Web sends the selected task prompt, context, and any explicitly selected `file_paths` attachments to Google (`gemini.google.com` or `aistudio.google.com`). Obtain user authorization once before sending if the task is not already covered by an active writing delegation; once authorized, attach only task-necessary regular files without re-prompting for routine documents, and never upload credentials, keys, cookies, full conversation transcripts, or entire directories.",
    undefined,
    true,
  );
}

server.registerTool(
  "gemini_web_status",
  {
    description: "Check local Gemini Web consent, browser availability, recovery, and three-slot concurrency status including activeRequests, activeRequestCount and targetIds.",
    inputSchema: {},
  },
  async () => {
    const [authorization, browser] = await Promise.all([
      authorizationStatus(),
      bridge.browserStatus(),
    ]);
    return textResult(JSON.stringify({ authorization, browser }, null, 2));
  },
);

server.registerTool(
  "gemini_web_recover",
  {
    description: "Inspect a leftover request under the shared operation lock, preserve its original page and recovery metadata, and release its task slot for independent new requests. Pass request_id to re-inspect a recovered request. Never resubmits a prompt or stops the browser.",
    inputSchema: { request_id: z.string().min(1).max(100).optional() },
  },
  async ({ request_id }, extra) => {
    try {
      const recovery = await enqueue(() => bridge.recoverRequest({ requestId: request_id, signal: extra.signal }));
      return textResult(JSON.stringify({ recovery, status: "completed" }));
    } catch (error) { return errorResult(normalizeBridgeError(error)); }
  },
);

server.registerTool(
  "gemini_web_shutdown",
  {
    description: "Wait for the active bridge request, close only its verified dedicated resident browser, and clear runtime ownership metadata. Does not close normal Chrome windows or erase login state/conversations.",
    inputSchema: {},
  },
  async (_args, extra) => {
    try {
      const result = await enqueue(() => bridge.stopBrowser(extra.signal));
      return textResult(JSON.stringify({ ...result, status: "completed" }));
    } catch (error) { return errorResult(normalizeBridgeError(error)); }
  },
);

server.registerTool(
  "gemini_web_authorize",
  {
    description:
      "Record user authorization for sending task-necessary prompts and selected file attachments to Google via Gemini Web in the current session.",
    inputSchema: {
      confirmed: z.literal(true).describe("Must be true after explicit user confirmation."),
    },
  },
  async ({ confirmed }, extra) => {
    if (confirmed !== true) return textResult("Authorization was not confirmed.", undefined, true);
    const value = await authorize(extra.signal);
    return textResult(`Gemini Web authorized at ${value.authorizedAt}.`);
  },
);

server.registerTool(
  "gemini_web_login",
  {
    description:
      "Open a visible Chrome window without debugging flags using the dedicated profile, wait for the user to close it, then verify login for the selected provider. Use provider=aistudio for AI Studio session refresh after permission errors. The user completes sign-in and CAPTCHA.",
    inputSchema: { provider: z.enum(["gemini", "aistudio"]).optional() },
  },
  async ({ provider }, extra) => {
    try {
      const result = await enqueue(() =>
        bridge.launchHumanLogin(
          { signal: extra.signal, wait: true, provider },
          (message, progress) => notify(extra, message, progress),
        ),
      );
      return textResult(result.message);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "gemini_web_ask",
  {
    description:
      "Send a prompt to Gemini Web or AI Studio Web in a new or existing conversation. Pass `provider` `\"gemini\"` (legacy default) or `\"aistudio\"`. Both providers support `\"4 Argon\"`, `\"4 Carbon\"`, `\"4 Neon\"`, and `\"3.8 Flash\"` (with legacy Hydrogen pins); AI Studio verifies the selected model as the exact `gemini-4-argon`, `gemini-4-hydrogen`, or `gemini-3.8-flash` ID and rejects unverified models before submission. AI Studio responses return the requested `model`, its exact `model_id`, and `billing_mode: \"subscription\"`. Every AI Studio submission re-verifies the exact model-name, PRO on the account switcher, and `No API key selected`/key_off immediately before clicking Run; paid/unknown API-key billing fails closed. For either provider, optionally attach up to 10 non-empty regular files (max 100,000,000 bytes each) via explicit absolute `file_paths`; files are uploaded to Google before the prompt is submitted. AI Studio uses a dedicated headed Chrome runtime launched minimized and keeps CDP work in the background; the bridge waits for active tasks before a mode upgrade. AI Studio uploads selected files through its native file input and waits for processing before Run. A completed answer may return conversation_id=null while AI Studio still has a new_chat URL; start a new request rather than resuming a null ID. Set `close_after=true` for a one-shot AI Studio task when the owned browser should be closed after a confirmed result; leave it false for follow-ups that reuse the conversation. Successful results return verified `provider`, `model`, `model_id`, `billing_mode`, `billing` evidence, and `attachments` (`{name, size}`), while rejecting credential paths, directories, and duplicate filenames.",
    inputSchema: {
      conversation_id: z.string().min(8).max(80).optional(),
      provider: z.enum(["gemini", "aistudio"]).optional().describe("Web provider; defaults to gemini, and an existing conversation inherits its saved provider."),
      prompt: z.string().min(1).max(MAX_PROMPT_CHARS),
      model: z.enum(["4 Argon", "4 Carbon", "4 Neon", "4 Hydrogen", "3.8 Flash"]).optional(),
      file_paths: z.array(z.string().min(1)).max(10).optional(),
      reasoning_effort: z.enum(["high", "xhigh"]).optional(),
      close_after: z.boolean().optional().describe("Close the owned AI Studio browser after a confirmed result; keep false for conversation follow-ups."),
    },
    outputSchema: executionOutputSchema,
  },
  async ({ conversation_id: conversationId, provider, prompt, model, reasoning_effort: reasoningEffort, file_paths: filePaths, close_after: closeAfter }, extra) => {
    const authorizationError = await requireAuthorization();
    if (authorizationError != null) return authorizationError;
    try {
      const result = await enqueue(() =>
        bridge.ask(
          { conversationId, ownerThreadId, provider, prompt, model, reasoningEffort, filePaths, closeAfter, signal: extra.signal },
          (message, progress) => notify(extra, message, progress),
        ),
      );
      return textResult(result.answer, {
        billing: result.billing,
        billing_mode: result.billing_mode,
        model: result.model,
        reasoning_effort: result.reasoning_effort ?? null,
        model_id: result.model_id,
        provider: result.provider,
        attachments: result.attachments,
        browser_closed: result.browserClosed === true,
        conversation_id: result.conversationId,
        error_code: null,
        partial_chars: 0,
        phase: "GENERATING",
        request_id: result.requestId,
        retry_safe: false,
        status: "completed",
      });
    } catch (error) {
      console.error(`[gemini-web-bridge] ${error.code ?? "UNEXPECTED"}: ${error.message}`);
      return errorResult(error);
    }
  },
);

server.registerTool(
  "gemini_web_list_conversations",
  {
    description:
      "List local Gemini conversation handles and timestamps. Metadata only; prompts and answers are not stored. Use current scope by default and all only when the user explicitly asks to recover another thread's conversation.",
    inputSchema: {
      scope: z.enum(["current", "all"]).default("current"),
    },
    outputSchema: {
      conversations: z.array(z.object({
        conversation_id: z.string(),
        created_at: z.string(),
        last_used_at: z.string(),
        legacy: z.boolean(),
        owner_thread_id: z.string().nullable(),
        provider: z.enum(["gemini", "aistudio"]),
      })),
      status: z.literal("completed"),
    },
  },
  async ({ scope }) => {
    const conversations = (await listConversations({ ownerThreadId, scope })).map((value) => ({
      conversation_id: value.conversationId,
      created_at: value.createdAt,
      last_used_at: value.lastUsedAt,
      legacy: value.legacy,
      owner_thread_id: value.ownerThreadId,
      provider: value.provider,
    }));
    return textResult(JSON.stringify(conversations, null, 2), {
      conversations,
      status: "completed",
    });
  },
);

server.registerTool(
  "analyze_youtube",
  {
    description:
      "Deprecated compatibility tool for v0.1 YouTube workflows. Prefer gemini_web_ask, which lets Codex choose fresh or continued conversations and handle any number of public URLs.",
    inputSchema: {
      language: z.string().min(2).max(20).default("zh-CN"),
      question: z.string().min(1).max(8_000),
      url: z.string().url(),
    },
    outputSchema: executionOutputSchema,
  },
  async ({ language, question, url }, extra) => {
    const authorizationError = await requireAuthorization();
    if (authorizationError != null) return authorizationError;
    try {
      canonicalizeYoutubeUrl(url);
      const result = await enqueue(() =>
        bridge.analyze(
          { language, question, signal: extra.signal, url },
          (message, progress) => notify(extra, message, progress),
        ),
      );
      return textResult(
        ["Deprecated analyze_youtube result", `Video: ${result.video.url}`, "", result.answer].join("\n"),
        {
          conversation_id: result.conversationId,
          error_code: null,
          partial_chars: 0,
          phase: "GENERATING",
          request_id: result.requestId,
          retry_safe: false,
          status: "completed",
        },
      );
    } catch (error) {
      console.error(`[gemini-web-bridge] ${error.code ?? "UNEXPECTED"}: ${error.message}`);
      return errorResult(error);
    }
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[gemini-web-bridge] MCP server ready on stdio");
