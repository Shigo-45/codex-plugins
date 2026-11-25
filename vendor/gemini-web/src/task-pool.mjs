import { createHash } from "node:crypto";
import { join } from "node:path";
import { acquireFileLock, withFileLock } from "./operation-lock.mjs";
import { isHomeUrl, normalizeProvider } from "./providers.mjs";

export const MAX_WEB_TASKS = 3;
export function canonicalConversationUrl(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname.replace(/\/$/, "")}`;
  } catch { return value; }
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

export class WebTaskPool {
  constructor(runtime) { this.runtime = runtime; }
  slotLock(slot) { return join(this.runtime.root, "locks", `task-${slot}`); }

  async exclusive(signal, operation) {
    const releases = [];
    try {
      for (let slot = 1; slot <= MAX_WEB_TASKS; slot++) {
        releases.push(await acquireFileLock(this.slotLock(slot), { signal, label: "Gemini Web maintenance", staleAfterMs: Infinity }));
      }
      return await withFileLock(join(this.runtime.root, "locks", "browser"), { signal }, operation);
    } finally { for (const release of releases.reverse()) await release(); }
  }

  async prepare(signal) {
    const current = await this.runtime.read();
    if (!current || current.version >= 3 && current.poolVersion === 1 && !current.inFlight) return;
    await withFileLock(join(this.runtime.root, "locks", "pool-migration"), { signal, pollMs: 10 }, async () => {
      const latest = await this.runtime.read();
      if (!latest || latest.version >= 3 && latest.poolVersion === 1 && !latest.inFlight) return;
      await this.exclusive(signal, async () => {
        const state = await this.runtime.read();
        if (!state) return;
        if (state.poolVersion === 1 && !state.inFlight) {
          // Fence provider-unaware writers after all legacy slot holders finish.
          // Preserve task pages and quarantined records during the format upgrade.
          if (state.version < 3) await this.runtime.write(state);
          return;
        }
        await this.runtime.recoverPending();
        const parent = await this.runtime.read();
        const candidates = (await this.runtime.recoveryHistory()).filter(record =>
          !record.slotId && record.browserSocket === parent.browserSocket &&
          ["DETACHED_GENERATING", "OUTCOME_UNKNOWN"].includes(record.outcome));
        const active = [];
        for (const candidate of candidates) {
          const record = await this.runtime.inspectRequest(parent, candidate);
          await this.runtime.saveRecovery(record);
          if (record.outcome === "DETACHED_GENERATING" || record.outcome === "OUTCOME_UNKNOWN" && record.reason === "page-unavailable") active.push(record);
        }
        if (active.length > MAX_WEB_TASKS) throw Object.assign(new Error("Legacy pages still occupy more than three tasks. Inspect them before admitting new work."), { code: "OUTCOME_UNKNOWN" });
        for (let index = 0; index < active.length; index++) {
          const task = this.runtime.taskRuntime(index + 1);
          const saved = { ...active[index], slotId: index + 1 };
          await task.saveRecovery(saved);
          await task.write({ ...parent, targetId: null, quarantined: saved });
        }
        parent.poolVersion = 1;
        await this.runtime.write(parent);
      });
    });
  }

  async run({ signal, conversationUrl, provider = "gemini" }, operation) {
    const resolvedProvider = normalizeProvider(provider);
    const run = async () => {
      await this.prepare(signal);
      const deadline = Date.now() + 10 * 60_000;
      while (Date.now() < deadline) {
        if (signal?.aborted) throw Object.assign(new Error("Gemini request cancelled while queued."), { code: "CANCELLED" });
        for (let slot = 1; slot <= MAX_WEB_TASKS; slot++) {
          let release;
          try {
            release = await acquireFileLock(this.slotLock(slot), { signal, timeoutMs: 20, pollMs: 10, staleAfterMs: Infinity });
          } catch (error) { if (error.code === "LOCK_TIMEOUT") continue; throw error; }
          const scoped = this.runtime.taskRuntime(slot);
          try {
            await scoped.recoverPending();
            if ((await scoped.read())?.quarantined) continue;
            try { return await operation(scoped); }
            finally { await scoped.recoverPending(); }
          } finally { await release(); }
        }
        await delay(100);
      }
      throw Object.assign(new Error("Timed out waiting for a Gemini Web task slot."), { code: "LOCK_TIMEOUT" });
    };
    if (!conversationUrl || isHomeUrl(resolvedProvider, conversationUrl)) return run();
    const key = createHash("sha256").update(canonicalConversationUrl(conversationUrl)).digest("hex");
    return withFileLock(join(this.runtime.root, "locks", `conversation-${key}`),
      { signal, label: "Gemini conversation", staleAfterMs: Infinity }, run);
  }
}
