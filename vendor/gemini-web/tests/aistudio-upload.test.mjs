import assert from "node:assert/strict";
import test from "node:test";

import { prepareAistudioUpload, readAistudioUploadState } from "../src/aistudio-upload.mjs";
import { createAistudioPage, FakeElement } from "./aistudio-fixture.mjs";

function addUploadInput(page, { accept = "text/*,application/*,image/*,video/*,audio/*" } = {}) {
  const input = new FakeElement("input", {
    attrs: { accept, multiple: "", type: "file" },
    className: "file-input",
  });
  page.document.body.append(input);
  page.elements.uploadInput = input;
  return input;
}

function composerArea(page) {
  if (page.elements.composerArea != null) return page.elements.composerArea;
  const area = new FakeElement("div", { className: "prompt-area" });
  const body = page.document.body;
  const index = body.children.indexOf(page.elements.composer);
  if (index >= 0) body.children.splice(index, 1);
  area.append(page.elements.composer);
  body.append(area);
  page.elements.composerArea = area;
  return area;
}

function addMediaContainer(page) {
  const container = new FakeElement("div", {
    attrs: { "data-test-id": "prompt-media-container" },
  });
  composerArea(page).append(container);
  page.elements.mediaContainer = container;
  return container;
}

function addCard(container, { name, size = "17 B", text = null, visible = true } = {}) {
  const card = new FakeElement("div", {
    className: "file-preview-chip",
    text: text ?? `${name}\n${size}`,
    visible,
  });
  container.append(card);
  return card;
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test("prepare uses the existing native file-input contract and rejects unsupported types before selection", async () => {
  const page = createAistudioPage();
  const input = addUploadInput(page);

  const result = await page.run(prepareAistudioUpload)(["notes.txt", "figure.png"]);
  assert.equal(result.ok, true);
  assert.equal(result.provider, "aistudio");
  assert.deepEqual(plain(result.groups), [{ index: 0, names: ["notes.txt", "figure.png"] }]);
  assert.equal(result.maxFiles, 10);
  assert.deepEqual(plain(result.expected), [
    { name: "notes.txt", size: null },
    { name: "figure.png", size: null },
  ]);

  const addButton = new FakeElement("button", { attrs: { "data-test-id": "add-media-button" } });
  page.document.body.append(addButton);
  const rejected = await page.run(prepareAistudioUpload)(["payload.exe"]);
  assert.equal(rejected.ok, false);
  assert.equal(rejected.code, "UPLOAD_UNSUPPORTED");
  assert.equal(rejected.failClosed, true);
  assert.equal(rejected.stage, "type-validation");
  assert.deepEqual(plain(rejected.unsupported), ["payload.exe"]);
  assert.equal(input.clickCount, 0);
  assert.equal(addButton.clickCount, 0);
  assert.equal(page.elements.run.clickCount, 0);

  const oversized = await page.run(prepareAistudioUpload)([{ name: "large.txt", size: 100_000_001 }]);
  assert.equal(oversized.ok, false);
  assert.equal(oversized.code, "INVALID_INPUT");
  assert.equal(addButton.clickCount, 0);
});

test("prepare clicks only AI Studio's add-media control when the input is not mounted yet", async () => {
  const page = createAistudioPage();
  const button = new FakeElement("button", {
    aria: "Insert images, videos, audio, or files",
    className: "add-media-button",
  });
  page.document.body.append(button);
  let input = null;
  button.onClick = () => {
    input = new FakeElement("input", {
      attrs: { accept: ".txt", type: "file" },
      className: "file-input",
    });
    page.document.body.append(input);
    page.elements.uploadInput = input;
  };

  const result = await page.run(prepareAistudioUpload)(["notes.txt"]);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.groups), [{ index: 0, names: ["notes.txt"] }]);
  assert.equal(button.clickCount, 1);
  assert.equal(input.clickCount, 0);
  assert.equal(page.elements.run.clickCount, 0);
});

test("prepare fails closed on a visible modal and identifies upload acknowledgement without accepting it", async () => {
  const blocked = createAistudioPage({ modalVisible: true });
  const button = new FakeElement("button", { attrs: { "data-test-id": "add-media-button" } });
  blocked.document.body.append(button);
  const blockedResult = await blocked.run(prepareAistudioUpload)(["notes.txt"]);
  assert.equal(blockedResult.ok, false);
  assert.equal(blockedResult.code, "INTERACTION_REQUIRED");
  assert.equal(blockedResult.modalVisible, true);
  assert.equal(blockedResult.failClosed, true);
  assert.equal(button.clickCount, 0);

  const acknowledgement = createAistudioPage();
  acknowledgement.document.body.append(new FakeElement("mat-dialog-container", {
    attrs: { id: "copyright-acknowledgement-dialog" },
    text: "Copyright acknowledgement: continue",
  }));
  const acknowledgementResult = await acknowledgement.run(prepareAistudioUpload)(["notes.txt"]);
  assert.equal(acknowledgementResult.code, "INTERACTION_REQUIRED");
  assert.equal(acknowledgementResult.acknowledgementRequired, true);
  assert.match(acknowledgementResult.message, /manually/);
});

test("read returns filename/size receipts and requires all expected files before ready", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", size: "1.2 KB" });
  addCard(container, { name: "figure.png", size: "17 B" });

  const ready = page.run(readAistudioUploadState)([
    { name: "notes.txt", size: 1229 },
    { name: "figure.png", size: 17 },
  ]);
  assert.equal(ready.ready, true);
  assert.equal(ready.busy, false);
  assert.equal(ready.failure, null);
  assert.equal(ready.status, "READY");
  assert.equal(ready.failClosed, false);
  assert.deepEqual(plain(ready.receipts), [
    { name: "notes.txt", size: 1229, sizeEvidence: "selected_file" },
    { name: "figure.png", size: 17, sizeEvidence: "page" },
  ]);
  assert.deepEqual(plain(ready.missing), []);
  assert.equal(ready.partial, false);
  assert.deepEqual(plain(ready.sizeUnverified), []);
  assert.deepEqual(plain(ready.pageSizeVerified), ["figure.png"]);
  assert.equal(page.elements.run.clickCount, 0);

  const incomplete = page.run(readAistudioUploadState)(["notes.txt", "missing.txt"]);
  assert.equal(incomplete.ready, false);
  assert.equal(incomplete.busy, false);
  assert.equal(incomplete.partial, true);
  assert.equal(incomplete.status, "PARTIAL");
  assert.equal(incomplete.code, "UPLOAD_PARTIAL");
  assert.equal(incomplete.recoveryRequired, true);
  assert.deepEqual(plain(incomplete.missing), ["missing.txt"]);
  assert.deepEqual(plain(incomplete.receipts), [
    { name: "notes.txt", size: 1229, sizeEvidence: "page" },
  ]);
});

test("read waits for processing indicators and only becomes ready after they clear", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", size: "17 B" });
  const spinner = new FakeElement("mat-progress-spinner", { className: "upload-progress" });
  container.append(spinner);

  const processing = page.run(readAistudioUploadState)(["notes.txt"]);
  assert.equal(processing.ready, false);
  assert.equal(processing.busy, true);
  assert.equal(processing.status, "PROCESSING");
  assert.deepEqual(plain(processing.receipts), [
    { name: "notes.txt", size: 17, sizeEvidence: "page" },
  ]);

  spinner.visible = false;
  const ready = page.run(readAistudioUploadState)(["notes.txt"]);
  assert.equal(ready.ready, true);
  assert.equal(ready.busy, false);
  assert.equal(ready.status, "READY");
  assert.equal(page.elements.run.clickCount, 0);
});

test("read reports a partial upload without claiming success and preserves recovery semantics", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "first.txt", size: "8 B" });

  const partial = page.run(readAistudioUploadState)(["first.txt", "second.txt"]);
  assert.equal(partial.ready, false);
  assert.equal(partial.busy, false);
  assert.equal(partial.partial, true);
  assert.equal(partial.failure, null);
  assert.equal(partial.code, "UPLOAD_PARTIAL");
  assert.equal(partial.recoveryRequired, true);
  assert.equal(partial.failClosed, true);
  assert.equal(partial.recovery.preserveDraft, true);
  assert.equal(partial.recovery.replay, false);
  assert.deepEqual(plain(partial.missing), ["second.txt"]);
  assert.deepEqual(plain(partial.receipts), [
    { name: "first.txt", size: 8, sizeEvidence: "page" },
  ]);

  addCard(container, { name: "second.txt", size: "9 B" });
  const ready = page.run(readAistudioUploadState)(["first.txt", "second.txt"]);
  assert.equal(ready.ready, true);
  assert.equal(ready.partial, false);
});

test("read surfaces explicit upload failures and receipt size mismatches as terminal fail-closed states", () => {
  const page = createAistudioPage();
  const alert = new FakeElement("div", {
    attrs: { role: "alert" },
    text: "Upload failed: file is too large",
  });
  page.document.body.append(alert);
  const failed = page.run(readAistudioUploadState)(["notes.txt"]);
  assert.equal(failed.ready, false);
  assert.equal(failed.code, "UPLOAD_FAILED");
  assert.equal(failed.status, "FAILED");
  assert.equal(failed.recoveryRequired, true);
  assert.equal(failed.failClosed, true);
  assert.match(failed.failure, /failed/);
  assert.match(failed.failure, /too large/);

  alert.visible = false;
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", size: "2 KB" });
  const mismatch = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(mismatch.ready, false);
  assert.equal(mismatch.code, "UPLOAD_FAILED");
  assert.equal(mismatch.reason, "RECEIPT_MISMATCH");
  assert.equal(plain(mismatch.sizeMismatch)[0].actual, 2048);
  assert.equal(plain(mismatch.sizeMismatch)[0].expected, 17);
});

test("read requires a visible acknowledgement dialog to be handled manually", () => {
  const page = createAistudioPage();
  page.document.body.append(new FakeElement("mat-dialog-container", {
    attrs: { id: "copyright-acknowledgement-dialog" },
    text: "Copyright acknowledgement required",
  }));
  const state = page.run(readAistudioUploadState)(["notes.txt"]);
  assert.equal(state.ready, false);
  assert.equal(state.code, "INTERACTION_REQUIRED");
  assert.equal(state.acknowledgementRequired, true);
  assert.equal(state.status, "INTERACTION_REQUIRED");
  assert.equal(state.recoveryRequired, true);
  assert.match(state.failure, /manual/);
});

test("filename matching uses boundaries so a.txt is not satisfied by ba.txt", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "ba.txt", size: "10 B" });

  const missing = page.run(readAistudioUploadState)(["a.txt"]);
  assert.equal(missing.ready, false);
  assert.equal(missing.partial, false);
  assert.deepEqual(plain(missing.missing), ["a.txt"]);

  addCard(container, { name: "a.txt", size: "11 B" });
  const both = page.run(readAistudioUploadState)(["a.txt", "ba.txt"]);
  assert.equal(both.ready, true);
  assert.deepEqual(plain(both.receipts), [
    { name: "a.txt", size: 11, sizeEvidence: "page" },
    { name: "ba.txt", size: 10, sizeEvidence: "page" },
  ]);
});

test("an empty or unknown attachment state is never reported as ready", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  const unknown = page.run(readAistudioUploadState)(["not-rendered.txt"]);
  assert.equal(unknown.ready, false);
  assert.equal(unknown.busy, false);
  assert.equal(unknown.status, "UNKNOWN");
  assert.equal(unknown.code, "OUTCOME_UNKNOWN");
  assert.equal(unknown.unknown, true);
  assert.equal(unknown.recoveryRequired, true);
  assert.equal(unknown.failClosed, true);
  assert.equal(container.children.length, 0);
});

test("prepare routes files to multiple native inputs while preserving the bridge's raw input indexes", async () => {
  const page = createAistudioPage();
  const directoryInput = new FakeElement("input", {
    attrs: { accept: "", type: "file", webkitdirectory: "" },
    className: "directory-input",
  });
  const textInput = new FakeElement("input", {
    attrs: { accept: ".txt", type: "file" },
    className: "file-input",
  });
  const imageInput = new FakeElement("input", {
    attrs: { accept: "image/*", type: "file" },
    className: "file-input",
  });
  page.document.body.append(directoryInput, textInput, imageInput);

  const result = await page.run(prepareAistudioUpload)(["notes.txt", "figure.png"]);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.groups), [
    { index: 1, names: ["notes.txt"] },
    { index: 2, names: ["figure.png"] },
  ]);
});

test("read parses each filename's own size when one container lists several files", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  container.text = "notes.txt\n1.2 KB\nfigure.png\n17 B";

  const state = page.run(readAistudioUploadState)([
    { name: "notes.txt", size: 1229 },
    { name: "figure.png", size: 17 },
  ]);
  assert.equal(state.ready, true);
  assert.deepEqual(plain(state.receipts), [
    { name: "notes.txt", size: 1229, sizeEvidence: "selected_file" },
    { name: "figure.png", size: 17, sizeEvidence: "page" },
  ]);
});

test("read rejects empty, duplicate or malformed receipt requests", () => {
  const page = createAistudioPage();
  const empty = page.run(readAistudioUploadState)([]);
  assert.equal(empty.ready, false);
  assert.equal(empty.code, "INVALID_INPUT");
  assert.deepEqual(plain(empty.receipts), []);
  const duplicate = page.run(readAistudioUploadState)(["a.txt", "a.txt"]);
  assert.equal(duplicate.ready, false);
  assert.equal(duplicate.code, "INVALID_INPUT");
});

test("a provisional size during processing is not treated as a terminal mismatch", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", size: "0 B" });
  const spinner = new FakeElement("mat-progress-spinner", { className: "upload-progress" });
  container.append(spinner);

  const processing = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(processing.busy, true);
  assert.equal(processing.ready, false);
  assert.equal(processing.failure, null);
  assert.equal(processing.sizeMismatch.length, 1);

  spinner.visible = false;
  const failed = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(failed.ready, false);
  assert.equal(failed.code, "UPLOAD_FAILED");
  assert.equal(failed.reason, "RECEIPT_MISMATCH");
});

test("a filename containing processing does not keep the upload busy forever", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "processing.txt", size: "17 B" });
  const state = page.run(readAistudioUploadState)(["processing.txt"]);
  assert.equal(state.busy, false);
  assert.equal(state.ready, true);
  assert.deepEqual(plain(state.receipts), [
    { name: "processing.txt", size: 17, sizeEvidence: "page" },
  ]);
});

test("read ignores historical ms-chat-turn attachments and their status", () => {
  const page = createAistudioPage();
  const historyTurn = new FakeElement("ms-chat-turn");
  const historyContainer = new FakeElement("div", {
    attrs: { "data-test-id": "prompt-media-container" },
  });
  historyContainer.append(new FakeElement("div", {
    className: "file-preview-chip",
    text: "history.txt\n17 B\nLoading...",
  }));
  historyTurn.append(historyContainer);
  page.document.body.append(historyTurn);

  const historyOnly = page.run(readAistudioUploadState)(["history.txt"]);
  assert.equal(historyOnly.ready, false);
  assert.equal(historyOnly.busy, false);
  assert.equal(historyOnly.status, "UNKNOWN");
  assert.deepEqual(plain(historyOnly.missing), ["history.txt"]);
  assert.deepEqual(plain(historyOnly.receipts), []);

  const currentContainer = addMediaContainer(page);
  addCard(currentContainer, { name: "current.txt", size: "17 B" });
  const current = page.run(readAistudioUploadState)(["current.txt"]);
  assert.equal(current.ready, true);
  assert.equal(current.busy, false);
  assert.deepEqual(plain(current.receipts), [
    { name: "current.txt", size: 17, sizeEvidence: "page" },
  ]);

  const stillHistorical = page.run(readAistudioUploadState)(["history.txt"]);
  assert.equal(stillHistorical.ready, false);
  assert.deepEqual(plain(stillHistorical.missing), ["history.txt"]);
});

test("read waits for the current card's real Loading... status to end", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  const card = new FakeElement("div", {
    className: "attachment-card",
    text: "notes.txt\nLoading...",
  });
  const statusRow = new FakeElement("div", { className: "status-and-tokens" });
  const tokenStatus = new FakeElement("ms-token-status");
  const status = new FakeElement("span", {
    attrs: { "data-test-id": "status" },
    text: "Loading...",
  });
  tokenStatus.append(status);
  statusRow.append(tokenStatus);
  card.append(statusRow);
  container.append(card);

  const processing = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(processing.ready, false);
  assert.equal(processing.busy, true);
  assert.equal(processing.status, "PROCESSING");
  assert.equal(processing.failure, null);
  assert.deepEqual(plain(processing.receipts), [
    { name: "notes.txt", size: 17, sizeEvidence: "selected_file" },
  ]);

  status.text = "Ready";
  card.text = "notes.txt\nReady";
  const ready = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(ready.ready, true);
  assert.equal(ready.busy, false);
  assert.equal(ready.status, "READY");
  assert.deepEqual(plain(ready.receipts), [
    { name: "notes.txt", size: 17, sizeEvidence: "selected_file" },
  ]);
  assert.deepEqual(plain(ready.sizeUnverified), []);
  assert.deepEqual(plain(ready.pageSizeVerified), []);
});

test("a card without bytes keeps selected-file size evidence without claiming page verification", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", text: "notes.txt" });

  const state = page.run(readAistudioUploadState)([{ name: "notes.txt", size: 17 }]);
  assert.equal(state.ready, true);
  assert.equal(state.busy, false);
  assert.equal(state.failure, null);
  assert.deepEqual(plain(state.receipts), [
    { name: "notes.txt", size: 17, sizeEvidence: "selected_file" },
  ]);
  assert.deepEqual(plain(state.sizeUnverified), []);
  assert.deepEqual(plain(state.pageSizeVerified), []);
});

test("without page or selected-file bytes, ready does not invent a page size", () => {
  const page = createAistudioPage();
  const container = addMediaContainer(page);
  addCard(container, { name: "notes.txt", text: "notes.txt" });

  const state = page.run(readAistudioUploadState)(["notes.txt"]);
  assert.equal(state.ready, true);
  assert.deepEqual(plain(state.receipts), [
    { name: "notes.txt", size: null, sizeEvidence: null },
  ]);
  assert.deepEqual(plain(state.sizeUnverified), ["notes.txt"]);
  assert.deepEqual(plain(state.pageSizeVerified), []);
});

test("prepare supports the live multiple-input wildcard accept matrix", async () => {
  const page = createAistudioPage();
  const input = addUploadInput(page);
  assert.equal(input.getAttribute("multiple"), "");

  const result = await page.run(prepareAistudioUpload)([
    "notes.txt",
    "brief.pdf",
    "figure.png",
    "clip.mp4",
    "voice.mp3",
  ]);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.groups), [{
    index: 0,
    names: ["notes.txt", "brief.pdf", "figure.png", "clip.mp4", "voice.mp3"],
  }]);
});
