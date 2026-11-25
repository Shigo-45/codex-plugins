import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, symlink, truncate, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { validateAttachments, MAX_UPLOAD_BYTES, prepareGeminiUpload, readGeminiUploadState } from "../src/gemini-upload.mjs";

test("attachments validate selected regular files and reject ambiguous or credential paths", async t => {
  const root = await mkdtemp(join(tmpdir(), "gemini-files-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const file = join(root, "context.txt"), secret = join(root, ".env");
  await writeFile(file, "synthetic context");
  await writeFile(secret, "synthetic credential fixture");
  const [selected] = await validateAttachments([file]);
  assert.equal(selected.name, "context.txt");
  assert.equal(selected.size, 17);
  for (const paths of [[file, file], [root], ["relative.txt"], [secret], Array(11).fill(file)]) {
    await assert.rejects(() => validateAttachments(paths), e => e.code === "INVALID_INPUT");
  }
  const link = join(root, "context-link.txt");
  await symlink(secret, link);
  await assert.rejects(() => validateAttachments([link]), /Credential/);
  await truncate(file, 0);
  await assert.rejects(() => validateAttachments([file]), /nonempty/);
  await truncate(file, MAX_UPLOAD_BYTES + 1);
  await assert.rejects(() => validateAttachments([file]), /100 MB/);
});

test("upload preparation routes mixed document/image attachments to compatible inputs", async () => {
  const inputs = [{ accept: ".txt,.pdf" }, { accept: "image/*" }];
  const prepare = runInNewContext(`(${prepareGeminiUpload.toString()})`, {
    document: { querySelectorAll: () => inputs }, Date, setTimeout,
  });
  const result = await prepare(["notes.txt", "figure.png"]);
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.groups)), [{ index: 0, names: ["notes.txt"] }, { index: 1, names: ["figure.png"] }]);
  assert.equal((await prepare(["unsupported.exe"])).ok, false);
});

test("upload readiness requires all filenames and no active upload indicator", () => {
  let busy = true;
  const card = { getClientRects: () => [1], textContent: "notes.txt", getAttribute: () => null, querySelectorAll: () => [] };
  const spinner = { getClientRects: () => [1] };
  const scope = { querySelectorAll: selector => selector.includes("uploader-file-preview") ? [card] : (busy ? [spinner] : []) };
  const read = runInNewContext(`(${readGeminiUploadState.toString()})`, {
    document: { body: scope, querySelector: () => ({ closest: () => scope }), querySelectorAll: () => [] },
    getComputedStyle: () => ({ visibility: "visible" }),
  });
  assert.equal(read(["notes.txt"]).ready, false);
  busy = false;
  assert.equal(read(["notes.txt"]).ready, true);
  assert.equal(read(["missing.txt"]).ready, false);
  card.textContent = "ba.txt";
  assert.equal(read(["a.txt", "ba.txt"]).ready, false);
  assert.equal(read(["ba.txt"]).ready, true);
});
