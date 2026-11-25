import { realpath, stat } from "node:fs/promises";
import { basename, isAbsolute } from "node:path";

export const MAX_UPLOAD_FILES = 10;
export const MAX_UPLOAD_BYTES = 100_000_000;

const invalid = message => Object.assign(new Error(message), { code: "INVALID_INPUT" });
const sensitive = path => /(?:^|\/)(?:\.env(?:[.-][^/]*)?|\.ssh|\.gnupg|cookies|login data|credentials(?:\.[^/]*)?|id_rsa|id_ed25519)(?:\/|$)/i.test(path) ||
  /\.(?:pem|key|p12|pfx)$/i.test(path) || /\/\.codex\/auth\.json$/i.test(path);

export async function validateAttachments(filePaths = []) {
  if (!Array.isArray(filePaths) || filePaths.length > MAX_UPLOAD_FILES) throw invalid("Provide at most 10 explicit file paths.");
  const files = [], seen = new Set(), names = new Set();
  for (const input of filePaths) {
    if (typeof input !== "string" || !isAbsolute(input) || sensitive(input)) throw invalid("Attachments require absolute paths to selected non-credential files.");
    let path, info;
    try { path = await realpath(input); info = await stat(path); }
    catch { throw invalid("An attachment is missing or unreadable."); }
    if (sensitive(path)) throw invalid("Credential files cannot be uploaded.");
    if (!info.isFile() || info.size === 0 || info.size > MAX_UPLOAD_BYTES) throw invalid("Each attachment must be a nonempty regular file of at most 100 MB.");
    const name = basename(path);
    if (seen.has(path) || names.has(name)) throw invalid("Attachment paths and filenames must be unique.");
    seen.add(path); names.add(name);
    files.push({ path, name, size: info.size, mtimeMs: info.mtimeMs });
  }
  return files;
}

// Self-contained page functions: serialized through CDP.
export async function prepareGeminiUpload(names) {
  const visible = e => e?.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
  const inputs = () => Array.from(document.querySelectorAll('input[type="file"]')).filter(e => !e.webkitdirectory);
  if (inputs().length === 0) {
    const button = Array.from(document.querySelectorAll('button')).find(e => visible(e) && /上传和工具|添加文件|upload.*tools|add files/i.test(e.getAttribute("aria-label") ?? ""));
    if (!button) return { ok: false, message: "Gemini upload control was not found." };
    button.click();
  }
  const deadline = Date.now() + 15000;
  while (inputs().length === 0 && Date.now() < deadline) await new Promise(r => setTimeout(r, 100));
  const candidates = inputs(), groups = new Map();
  const image = /\.(?:png|jpe?g|gif|webp|bmp|heic|heif)$/i;
  for (const name of names) {
    const ext = "." + name.split(".").at(-1).toLowerCase();
    const input = candidates.find(e => !e.disabled && (!e.accept || e.accept.split(",").some(type =>
      type.trim().toLowerCase() === ext || (type.trim() === "image/*" && image.test(name)))));
    if (!input) return { ok: false, message: "A selected attachment type is not supported by the current Gemini upload controls." };
    const index = Array.from(document.querySelectorAll('input[type="file"]')).indexOf(input);
    if (!groups.has(index)) groups.set(index, []);
    groups.get(index).push(name);
  }
  return { ok: true, groups: Array.from(groups, ([index, names]) => ({ index, names })) };
}

export function readGeminiUploadState(names) {
  const visible = e => e?.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
  const scope = document.querySelector('[role="textbox"]')?.closest('input-container') ?? document.querySelector('input-area-v2') ?? document.body;
  const cards = Array.from(scope.querySelectorAll('uploader-file-preview, uploader-image-preview, uploader-video-preview, .file-preview-chip, .image-preview-chip, [data-test-id="attachment-chip"]')).filter(visible);
  const labels = cards.map(card => {
    const described = [card, ...card.querySelectorAll('[aria-describedby]')].flatMap(e => (e.getAttribute('aria-describedby') ?? '').split(/\s+/)).filter(Boolean).map(id => document.getElementById(id)?.textContent ?? '');
    return new Set([...described, card.textContent, card.getAttribute('title'), card.getAttribute('data-filename'),
      ...Array.from(card.querySelectorAll('[title]')).map(e => e.getAttribute('title'))].filter(Boolean).map(text => text.trim()));
  });
  const failure = Array.from(document.querySelectorAll('[role="alert"], .mat-mdc-snack-bar-container, .upload-error')).filter(visible)
    .map(e => e.innerText ?? '').find(text => /failed|cannot|can't|limit|quota|error|失败|无法|不能|上限|限额|错误/i.test(text)) ?? null;
  const busy = Array.from(scope.querySelectorAll('[role="progressbar"], mat-progress-spinner, [aria-busy="true"], .upload-progress')).some(visible);
  const used = new Set();
  const allPresent = names.every(name => {
    const index = labels.findIndex((values, i) => !used.has(i) && values.has(name));
    if (index < 0) return false;
    used.add(index); return true;
  });
  return { ready: !failure && !busy && allPresent, busy, failure };
}
