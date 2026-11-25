// AI Studio attachment automation. Both exported functions are serialized and
// executed inside the page through CDP, so they are completely self-contained
// and must never reference module-scope values.
//
// The bridge owns the CDP side (DOM.setFileInputFiles and phase persistence).
// This module only provides the two page-side contracts used by
// gemini-browser.uploadAttachments():
//   * prepareAistudioUpload(names) -> selectable input groups or a fail-closed error
//   * readAistudioUploadState(expected) -> filename/size receipts, busy/failure state
//
// No prompt is ever submitted here. If a modal (including AI Studio's upload
// copyright acknowledgement) is visible, or a receipt/processing state is
// ambiguous, the caller must preserve the draft and inspect it with recovery
// instead of replaying the upload or submitting text.

export const AISTUDIO_MAX_UPLOAD_FILES = 10;
export const AISTUDIO_MAX_UPLOAD_BYTES = 100_000_000;

export async function prepareAistudioUpload(inputs = []) {
  const MAX_FILES = 10;
  const MAX_BYTES = 100_000_000;
  const sleep = (ms) => new Promise((resolve) => {
    if (typeof setTimeout === "function") setTimeout(resolve, ms);
    else resolve();
  });
  const fail = (code, message, extra = {}) => ({
    ok: false,
    code,
    message,
    provider: "aistudio",
    failClosed: true,
    retrySafe: false,
    recovery: { action: "inspect_existing_draft", preserveDraft: true, replay: false },
    ...extra,
  });
  const visible = (element) => {
    if (element == null) return false;
    try {
      if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) return false;
      const style = typeof getComputedStyle === "function" ? getComputedStyle(element) : null;
      return style == null || (style.visibility !== "hidden" && style.display !== "none");
    } catch {
      return false;
    }
  };
  const textOf = (element) => String(element?.innerText ?? element?.textContent ?? "").trim();
  const attr = (element, name) => element?.getAttribute?.(name) ?? null;
  const queryAll = (selector) => {
    try {
      return Array.from(document.querySelectorAll(selector));
    } catch {
      const matches = [];
      for (const part of selector.split(",")) {
        try {
          for (const element of document.querySelectorAll(part.trim())) {
            if (!matches.includes(element)) matches.push(element);
          }
        } catch {}
      }
      return matches;
    }
  };
  // Never select files while a dialog is open. AI Studio's upload copyright
  // acknowledgement is user-facing legal UI, so it is reported for manual
  // action rather than accepted automatically.
  const dialogFailure = () => {
    const dialogs = queryAll(
      'mat-dialog-container, .mat-mdc-dialog-container, .mdc-dialog__surface, [role="dialog"]',
    ).filter(visible);
    if (dialogs.length === 0) return null;
    const dialogText = dialogs.map(textOf).join("\n").slice(0, 500);
    const acknowledgementRequired = /copyright|acknowledg|版权|授权|确认上传|upload.{0,40}agree/i.test(dialogText) ||
      dialogs.some((dialog) => /copyright|acknowledg/i.test(
        `${dialog?.id ?? ""} ${attr(dialog, "id") ?? ""} ${attr(dialog, "data-test-id") ?? ""}`,
      ));
    return fail(
      "INTERACTION_REQUIRED",
      acknowledgementRequired
        ? "AI Studio is showing an upload acknowledgement dialog. Review and accept it manually, then inspect the draft; no file was selected and no prompt was submitted."
        : "AI Studio is showing a modal dialog. Dismiss it manually, then inspect the draft; no file was selected and no prompt was submitted.",
      { acknowledgementRequired, modalVisible: true },
    );
  };

  const requested = typeof inputs === "string" ? [inputs] : inputs;
  if (!Array.isArray(requested) || requested.length === 0) {
    return fail("INVALID_INPUT", "Provide at least one explicit attachment filename; no file was selected.");
  }
  if (requested.length > MAX_FILES) {
    return fail("INVALID_INPUT", `Provide at most ${MAX_FILES} attachment filenames; no file was selected.`);
  }
  const entries = [];
  const seen = new Set();
  for (const item of requested) {
    const name = typeof item === "string" ? item : item?.name;
    const rawSize = typeof item?.size === "number" ? item.size : null;
    if (rawSize != null && (!Number.isFinite(rawSize) || rawSize <= 0 || rawSize > MAX_BYTES)) {
      return fail(
        "INVALID_INPUT",
        "Each attachment must be a nonempty file of at most 100,000,000 bytes; no file was selected.",
      );
    }
    const size = rawSize;
    if (typeof name !== "string" || name.length === 0 || name !== name.trim() ||
        name.length > 255 || /[\\/\0]/.test(name) || name === "." || name === "..") {
      return fail("INVALID_INPUT", "Attachment filenames must be nonempty basenames without path separators; no file was selected.");
    }
    const key = name.toLowerCase();
    if (seen.has(key)) {
      return fail("INVALID_INPUT", "Attachment filenames must be unique; no file was selected.");
    }
    seen.add(key);
    entries.push({ name, size });
  }

  const initialDialogFailure = dialogFailure();
  if (initialDialogFailure != null) return initialDialogFailure;

  const allInputs = () => queryAll('input[type="file"]');
  const eligibleInputs = () => allInputs().filter((input) =>
    input?.disabled !== true &&
    input?.webkitdirectory !== true &&
    attr(input, "webkitdirectory") == null,
  );
  const extensionOf = (name) => {
    const index = name.lastIndexOf(".");
    return index > 0 ? name.slice(index).toLowerCase() : "";
  };
  const MIME_BY_EXTENSION = {
    ".avi": "video/x-msvideo",
    ".bmp": "image/bmp",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".htm": "text/html",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".md": "text/markdown",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".py": "text/x-python",
    ".rtf": "application/rtf",
    ".svg": "image/svg+xml",
    ".text": "text/plain",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ts": "text/typescript",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".zip": "application/zip",
  };
  const acceptsName = (input, name) => {
    const accept = String(attr(input, "accept") ?? "").trim();
    if (accept.length === 0) return true;
    const extension = extensionOf(name);
    const mime = MIME_BY_EXTENSION[extension] ?? "";
    return accept.split(",").some((raw) => {
      let token = raw.trim().toLowerCase();
      if (token.length === 0) return false;
      if (token === "*" || token === "*/*") return true;
      if (token.startsWith("*.")) token = token.slice(1);
      if (token.startsWith(".")) return token === extension;
      if (token.endsWith("/*")) return mime.length > 0 && mime.startsWith(token.slice(0, -1));
      return mime.length > 0 && token === mime;
    });
  };
  const coversAll = () => entries.every((entry) =>
    eligibleInputs().some((input) => acceptsName(input, entry.name)),
  );

  // If an input is already mounted, reject a type it does not accept before
  // clicking any upload menu. Opening the picker for a file the page cannot
  // accept only creates avoidable browser state to recover.
  const initialInputs = eligibleInputs();
  if (initialInputs.length > 0) {
    const unsupported = entries.find((entry) =>
      !initialInputs.some((input) => acceptsName(input, entry.name)),
    );
    if (unsupported != null) {
      return fail(
        "UPLOAD_UNSUPPORTED",
        `AI Studio's current file input does not accept "${unsupported.name}". No file was selected and no prompt was submitted.`,
        { stage: "type-validation", unsupported: [unsupported.name] },
      );
    }
  }

  const clickable = (element) => visible(element) &&
    element?.disabled !== true &&
    attr(element, "aria-disabled") !== "true";
  const clickAddMedia = () => {
    // Only the controls evidenced on the live AI Studio prompt page. Broad
    // text/role fallbacks can click unrelated page actions.
    let target = queryAll(
      'button[data-test-id="add-media-button"], [data-test-id="add-media-button"]',
    ).find(clickable) ?? null;
    if (target == null) {
      const exactLabel = "Insert images, videos, audio, or files";
      target = queryAll("button").find((element) =>
        clickable(element) && (attr(element, "aria-label") ?? "").trim() === exactLabel,
      ) ?? null;
    }
    if (target == null) return false;
    target.click();
    return true;
  };
  const waitForCoverage = async (timeoutMs) => {
    if (typeof setTimeout !== "function") return coversAll();
    const deadline = Date.now() + timeoutMs;
    while (!coversAll() && Date.now() < deadline) await sleep(150);
    return coversAll();
  };

  if (!coversAll()) {
    if (clickAddMedia()) await waitForCoverage(5000);
  }

  const rawInputs = allInputs();
  const usable = rawInputs.filter((input) =>
    input?.disabled !== true &&
    input?.webkitdirectory !== true &&
    attr(input, "webkitdirectory") == null,
  );
  if (usable.length === 0) {
    return fail(
      "UPLOAD_UNSUPPORTED",
      "AI Studio's native file input was not found. The draft was preserved and no prompt was submitted.",
      { stage: "input-discovery" },
    );
  }

  const inputRelevance = (input) => {
    const className = attr(input, "class") ?? "";
    const accept = String(attr(input, "accept") ?? "").toLowerCase();
    let score = 0;
    if (/(?:^|\s)file-input(?:$|\s)/.test(className)) score -= 4;
    if (/text\/\*|\.(?:txt|md|csv|pdf|docx?|json)/.test(accept)) score -= 2;
    return score;
  };
  const orderedInputs = [...usable].sort((left, right) => inputRelevance(left) - inputRelevance(right));
  const groups = new Map();
  for (const entry of entries) {
    const input = orderedInputs.find((candidate) => acceptsName(candidate, entry.name)) ?? null;
    if (input == null) {
      return fail(
        "UPLOAD_UNSUPPORTED",
        `AI Studio's current file input does not accept "${entry.name}". No file was selected and no prompt was submitted.`,
        { stage: "type-validation", unsupported: [entry.name] },
      );
    }
    const index = rawInputs.indexOf(input);
    if (index < 0) {
      return fail(
        "UI_CHANGED",
        "AI Studio's file input changed while attachments were being prepared. No file was selected and no prompt was submitted.",
        { stage: "input-index" },
      );
    }
    if (!groups.has(index)) groups.set(index, []);
    groups.get(index).push(entry.name);
  }

  const finalDialogFailure = dialogFailure();
  if (finalDialogFailure != null) return finalDialogFailure;

  return {
    ok: true,
    provider: "aistudio",
    groups: Array.from(groups, ([index, names]) => ({ index, names })),
    expected: entries.map(({ name, size }) => ({ name, size })),
    maxFiles: MAX_FILES,
    maxFileBytes: MAX_BYTES,
  };
}

export function readAistudioUploadState(inputs = []) {
  const visible = (element) => {
    if (element == null) return false;
    try {
      if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) return false;
      const style = typeof getComputedStyle === "function" ? getComputedStyle(element) : null;
      return style == null || (style.visibility !== "hidden" && style.display !== "none");
    } catch {
      return false;
    }
  };
  const textOf = (element) => String(element?.innerText ?? element?.textContent ?? "").trim();
  const attr = (element, name) => element?.getAttribute?.(name) ?? null;
  const queryAll = (selector, root = document) => {
    try {
      return Array.from(root.querySelectorAll(selector));
    } catch {
      const matches = [];
      for (const part of selector.split(",")) {
        try {
          for (const element of root.querySelectorAll(part.trim())) {
            if (!matches.includes(element)) matches.push(element);
          }
        } catch {}
      }
      return matches;
    }
  };
  const composer = queryAll('textarea[formcontrolname="promptText"]').find(visible) ??
    queryAll('textarea[aria-label="Enter a prompt"]').find(visible) ?? null;
  const insideTurn = (element) => element?.closest?.("ms-chat-turn") != null;
  const containsTurn = (element) => {
    if (element == null || typeof element.querySelectorAll !== "function") return false;
    try {
      return element.querySelectorAll("ms-chat-turn").length > 0;
    } catch {
      return false;
    }
  };
  const ancestorChain = (element) => {
    const chain = [];
    let node = element;
    while (node != null) {
      chain.push(node);
      node = node.parentElement;
    }
    return chain;
  };
  const commonAncestor = (left, right) => {
    const rightChain = new Set(ancestorChain(right));
    for (const node of ancestorChain(left)) if (rightChain.has(node)) return node;
    return null;
  };
  const isCurrentUploadScope = (element) => {
    if (element == null || insideTurn(element)) return false;
    if (composer == null) return true;
    if (insideTurn(composer)) return false;
    const common = commonAncestor(element, composer);
    // History turns can sit in a broad page container shared with the current
    // composer. A media root under that shared container is not this draft's.
    return common != null && !containsTurn(common);
  };
  let composerScope = null;
  if (composer != null) {
    composerScope = composer;
    while (
      composerScope.parentElement != null &&
      composerScope.parentElement !== document.body &&
      composerScope.parentElement !== document.documentElement &&
      !containsTurn(composerScope.parentElement)
    ) {
      composerScope = composerScope.parentElement;
    }
  }
  const requested = typeof inputs === "string" ? [inputs] : inputs;
  const expected = [];
  const seen = new Set();
  if (Array.isArray(requested)) {
    for (const item of requested) {
      const name = typeof item === "string" ? item : item?.name;
      if (typeof name !== "string" || name.length === 0) continue;
      const key = name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      expected.push({
        name,
        size: typeof item?.size === "number" && Number.isFinite(item.size) && item.size >= 0 ? item.size : null,
      });
    }
  }

  const base = {
    provider: "aistudio",
    phase: "UPLOADING",
    expected: expected.map(({ name, size }) => ({ name, size })),
  };
  const failed = (code, message, extra = {}) => ({
    ...base,
    ready: false,
    busy: false,
    failure: message,
    code,
    failClosed: true,
    retrySafe: false,
    recoveryRequired: true,
    recovery: { action: "inspect_existing_draft", preserveDraft: true, replay: false },
    ...extra,
  });
  if (!Array.isArray(requested) || requested.length === 0 || expected.length !== requested.length) {
    return {
      ...failed(
        "INVALID_INPUT",
        "Upload readiness requires one or more unique explicit attachment filenames; no prompt was submitted.",
      ),
      attachments: [],
      missing: [],
      partial: false,
      receipts: [],
      sizeMismatch: [],
      sizeUnverified: [],
      pageSizeVerified: [],
      status: "FAILED",
    };
  }

  const lower = (value) => String(value).toLowerCase();
  const boundary = (character) => character === "" ||
    /[\s"'`()[\]{},;:<>|/\\，。；：、]/.test(character);
  const containsName = (text, name) => {
    if (!text || !name) return false;
    const haystack = lower(text);
    const needle = lower(name);
    let from = 0;
    while (from <= haystack.length - needle.length) {
      const index = haystack.indexOf(needle, from);
      if (index < 0) return false;
      const before = index === 0 ? "" : haystack[index - 1];
      const after = index + needle.length >= haystack.length ? "" : haystack[index + needle.length];
      if (boundary(before) && boundary(after)) return true;
      from = index + 1;
    }
    return false;
  };
  const exactAttributeName = (element) => {
    for (const name of ["data-filename", "data-file-name", "data-name", "filename"]) {
      const value = attr(element, name);
      if (value != null && value.trim().length > 0) return value.trim();
    }
    return null;
  };
  const matchesName = (element, name) => {
    const exact = exactAttributeName(element);
    if (exact != null && lower(exact) === lower(name)) return true;
    for (const candidate of [attr(element, "title"), attr(element, "aria-label"), attr(element, "alt")]) {
      if (candidate != null && containsName(candidate, name)) return true;
    }
    if (containsName(textOf(element), name)) return true;
    if (typeof element.querySelectorAll === "function") {
      for (const child of queryAll("[title], [aria-label], [alt]", element)) {
        for (const candidate of [attr(child, "title"), attr(child, "aria-label"), attr(child, "alt")]) {
          if (candidate != null && containsName(candidate, name)) return true;
        }
      }
    }
    return false;
  };

  const parseNumber = (raw) => {
    const normalized = String(raw).replace(/\s+/g, "");
    let value = normalized;
    if (value.includes(",") && value.includes(".")) value = value.replace(/,/g, "");
    else if (/^\d{1,3}(?:,\d{3})+$/.test(value)) value = value.replace(/,/g, "");
    else value = value.replace(",", ".");
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const parseSize = (value) => {
    if (value == null) return null;
    const text = String(value).trim();
    if (text.length === 0) return null;
    if (/^\d+(?:[.,]\d+)?$/.test(text)) {
      const bytes = parseNumber(text);
      return bytes == null ? null : { bytes: Math.round(bytes), text, unitBytes: 1, decimals: 0 };
    }
    const match = text.match(/(\d+(?:[.,]\d+)?)\s*(bytes?|b|kib|kb|mib|mb|gib|gb|tib|tb)\b/i);
    if (match == null) return null;
    const number = parseNumber(match[1]);
    if (number == null) return null;
    const unit = match[2].toLowerCase();
    const unitBytes = unit === "b" || unit === "byte" || unit === "bytes"
      ? 1
      : unit === "kb" || unit === "kib" ? 1024
        : unit === "mb" || unit === "mib" ? 1024 ** 2
          : unit === "gb" || unit === "gib" ? 1024 ** 3
            : 1024 ** 4;
    const decimalMatch = /[.,](\d+)$/.exec(match[1]);
    const decimals = decimalMatch != null && !/^\d{3}$/.test(decimalMatch[1]) ? decimalMatch[1].length : 0;
    return {
      bytes: Math.round(number * unitBytes),
      text: `${match[1]} ${match[2]}`,
      unitBytes,
      decimals,
    };
  };
  const parseNearName = (value, name) => {
    if (value == null) return null;
    const text = String(value);
    const index = lower(text).indexOf(lower(name));
    if (index < 0) return null;
    let end = Math.min(text.length, index + name.length + 180);
    // A media container can list several files in one text node. Stop before
    // the next expected filename so the previous file's size is not reused.
    for (const other of expected) {
      if (lower(other.name) === lower(name)) continue;
      const otherIndex = lower(text).indexOf(lower(other.name), index + name.length);
      if (otherIndex >= 0 && otherIndex < end) end = otherIndex;
    }
    const after = parseSize(text.slice(index + name.length, end));
    if (after != null) return after;
    return parseSize(text.slice(Math.max(0, index - 120), index));
  };
  const extractSize = (element, name) => {
    for (const attribute of ["data-size-bytes", "data-bytes", "data-file-size", "data-size", "size"]) {
      const parsed = parseSize(attr(element, attribute));
      if (parsed != null) return parsed;
    }
    for (const attribute of ["title", "aria-label"]) {
      const value = attr(element, attribute);
      if (value == null) continue;
      const near = parseNearName(value, name);
      if (near != null) return near;
      if (!containsName(value, name)) {
        const parsed = parseSize(value);
        if (parsed != null) return parsed;
      }
    }
    return parseNearName(textOf(element), name);
  };

  // Only the current prompt's media container is evidence for this upload.
  // Historical turns can render the same prompt-media-container markup.
  const mediaRoots = queryAll('[data-test-id="prompt-media-container"]')
    .filter(isCurrentUploadScope);
  const candidates = [];
  const addCandidate = (element) => {
    if (element != null && !candidates.includes(element)) candidates.push(element);
  };
  for (const root of mediaRoots) {
    addCandidate(root);
    for (const child of Array.from(root.children ?? [])) addCandidate(child);
  }
  const candidateScore = (element) => {
    const attributeText = ["title", "aria-label", "data-filename", "data-file-name"]
      .map((name) => attr(element, name) ?? "")
      .join(" ");
    return `${textOf(element)} ${attributeText}`.length;
  };
  candidates.sort((left, right) => candidateScore(left) - candidateScore(right));
  const visibleCandidates = candidates.filter(visible);

  const inputSizeByName = new Map();
  for (const input of queryAll('input[type="file"]')) {
    const files = input?.files;
    if (files == null) continue;
    const length = Number(files.length) || 0;
    for (let index = 0; index < length; index += 1) {
      const file = files[index] ?? (typeof files.item === "function" ? files.item(index) : null);
      if (file == null || typeof file.name !== "string") continue;
      if (typeof file.size === "number" && Number.isFinite(file.size) && file.size >= 0) {
        inputSizeByName.set(lower(file.name), file.size);
      }
    }
  }

  const receipts = [];
  const missing = [];
  const sizeUnverified = [];
  const sizeMismatch = [];
  const pageSizeVerified = [];
  for (const entry of expected) {
    const matches = visibleCandidates.filter((candidate) => matchesName(candidate, entry.name));
    if (matches.length === 0) {
      missing.push(entry.name);
      continue;
    }
    let pageSize = extractSize(matches[0], entry.name);
    if (pageSize == null) {
      for (const candidate of matches.slice(1)) {
        pageSize = extractSize(candidate, entry.name);
        if (pageSize != null) break;
      }
    }
    const inputSize = inputSizeByName.get(lower(entry.name)) ?? null;
    // The bridge has already stat()ed and selected this source file. A page
    // without a byte count must not lose that evidence or pretend the page
    // verified bytes. Only an explicit byte count on the card is page evidence.
    const selectedSize = entry.size ?? inputSize ?? null;
    let resolvedSize = null;
    let sizeEvidence = null;
    if (selectedSize != null) {
      resolvedSize = selectedSize;
      sizeEvidence = pageSize != null && pageSize.unitBytes === 1 ? "page" : "selected_file";
    } else if (pageSize != null) {
      resolvedSize = pageSize.bytes;
      sizeEvidence = "page";
    }
    if (selectedSize == null && pageSize == null) sizeUnverified.push(entry.name);
    if (pageSize != null && pageSize.unitBytes === 1) pageSizeVerified.push(entry.name);
    if (selectedSize != null && pageSize != null) {
      const tolerance = pageSize.unitBytes === 1
        ? 0
        : Math.max(1, Math.ceil((pageSize.unitBytes * 10 ** -pageSize.decimals) / 2));
      if (Math.abs(pageSize.bytes - selectedSize) > tolerance) {
        sizeMismatch.push({
          actual: pageSize.bytes,
          displayed: pageSize.text,
          expected: selectedSize,
          name: entry.name,
        });
      }
    }
    receipts.push({ name: entry.name, size: resolvedSize, sizeEvidence });
  }

  const progressSelector = [
    "mat-progress-spinner",
    "mat-progress-bar",
    '[role="progressbar"]',
    '[aria-busy="true"]',
    ".upload-progress",
    ".file-upload-progress",
    ".mat-mdc-progress-spinner",
    ".mat-progress-spinner",
  ].join(",");
  const statusScopes = [...new Set([
    ...mediaRoots.filter(visible),
    ...visibleCandidates,
    ...(composerScope != null ? [composerScope] : []),
  ])];
  const busyElements = [];
  for (const scope of statusScopes) busyElements.push(...queryAll(progressSelector, scope));
  const uniqueBusy = [...new Set(busyElements)].filter(visible);
  const busyClassPattern = /(?:^|[\s_-])(?:uploading|processing|preparing|pending|loading)(?:$|[\s_-])/i;
  const busyByAttribute = uniqueBusy.length > 0 || visibleCandidates.some((candidate) => {
    const metadata = [
      attr(candidate, "class"),
      attr(candidate, "id"),
      attr(candidate, "data-test-id"),
      attr(candidate, "aria-label"),
      attr(candidate, "title"),
    ].join(" ");
    return busyClassPattern.test(metadata);
  });
  const busyTextPattern = /(?:\b(?:uploading|processing|preparing|loading)\b(?!\s*\.+\w)|正在上传|上传中|处理中|正在处理|加载中|载入中|準備中)/i;
  // Text status belongs to the current attachment card. Only fall back to the
  // turn-free composer scope when no current card exists yet.
  const textScopes = [...mediaRoots.filter(visible), ...visibleCandidates];
  if (textScopes.length === 0 && composerScope != null) textScopes.push(composerScope);
  const busyByStatusText = textScopes.some((scope) => busyTextPattern.test(textOf(scope)));
  const busy = busyByAttribute || busyByStatusText;

  const errorPattern = /(?:failed|error|unsupported|too large|exceeds|limit|quota|rejected|invalid|unavailable|went wrong|失败|错误|无法|不支持|过大|上限|限额|无效)/i;
  const uploadErrorPattern = /(?:upload|file|attachment|document|image|video|audio|附件|文件|上传)[^\n]{0,120}(?:failed|error|unsupported|too large|exceeds|limit|quota|rejected|invalid|unavailable|went wrong|失败|错误|不支持|过大|上限|限额|无效)|(?:failed|error|unable|cannot|can't)[^\n]{0,120}(?:upload|file|attachment|上传|文件|附件)|上传失败|无法上传|文件过大|不支持的文件|上传出错/i;
  let errorText = null;
  // Global snackbars are legitimate current-page errors, but historical turn
  // errors must never make the current upload look failed.
  const errorElements = queryAll(
    '[role="alert"], .upload-error, .file-upload-error, .upload-failure, .file-error, .mat-mdc-snack-bar-container, [data-test-id*="upload-error"], [data-test-id*="file-error"]',
  ).filter((element) => visible(element) && !insideTurn(element));
  for (const element of errorElements) {
    const text = textOf(element);
    if (text.length > 0 && errorPattern.test(text)) {
      errorText = text.slice(0, 500);
      break;
    }
  }
  if (errorText == null) {
    for (const scope of statusScopes) {
      const text = textOf(scope);
      if (text.length > 0 && uploadErrorPattern.test(text)) {
        errorText = text.slice(0, 500);
        break;
      }
    }
  }

  const dialogs = queryAll(
    'mat-dialog-container, .mat-mdc-dialog-container, .mdc-dialog__surface, [role="dialog"]',
  ).filter(visible);
  let dialogText = null;
  if (dialogs.length > 0) {
    dialogText = dialogs.map(textOf).join("\n").slice(0, 500);
    const acknowledgementRequired = /copyright|acknowledg|版权|授权|确认上传|upload.{0,40}agree/i.test(dialogText) ||
      dialogs.some((dialog) => /copyright|acknowledg/i.test(
        `${dialog?.id ?? ""} ${attr(dialog, "id") ?? ""} ${attr(dialog, "data-test-id") ?? ""}`,
      ));
    return failed(
      "INTERACTION_REQUIRED",
      acknowledgementRequired
        ? "AI Studio requires manual acknowledgement of the upload terms. The draft and any accepted files were preserved; inspect it with recovery and do not replay the upload."
        : "A visible dialog is blocking AI Studio attachment processing. The draft was preserved; inspect it with recovery and do not replay the upload.",
      {
        acknowledgementRequired,
        attachments: receipts,
        busy,
        missing,
        modalVisible: true,
        partial: receipts.length > 0 && missing.length > 0,
        receipts,
        sizeMismatch,
        sizeUnverified,
        pageSizeVerified,
        status: "INTERACTION_REQUIRED",
      },
    );
  }

  if (errorText != null) {
    return failed(
      "UPLOAD_FAILED",
      `AI Studio reported an attachment failure: ${errorText}. The draft was preserved; inspect it with recovery and do not replay the upload.`,
      {
        attachments: receipts,
        busy,
        missing,
        partial: receipts.length > 0 && missing.length > 0,
        receipts,
        reason: "UPLOAD_ERROR",
        sizeMismatch,
        sizeUnverified,
        pageSizeVerified,
        status: "FAILED",
      },
    );
  }
  if (sizeMismatch.length > 0 && !busy) {
    const first = sizeMismatch[0];
    return failed(
      "UPLOAD_FAILED",
      `AI Studio's receipt for "${first.name}" reports ${first.displayed} (${first.actual} bytes), not the selected ${first.expected} bytes. The draft was preserved; no prompt was submitted.`,
      {
        attachments: receipts,
        busy,
        missing,
        partial: receipts.length > 0 && missing.length > 0,
        receipts,
        reason: "RECEIPT_MISMATCH",
        sizeMismatch,
        sizeUnverified,
        pageSizeVerified,
        status: "FAILED",
      },
    );
  }

  const partial = receipts.length > 0 && missing.length > 0;
  const unknown = !busy && receipts.length === 0 && expected.length > 0;
  const ready = !busy && !partial && !unknown && missing.length === 0 &&
    receipts.length === expected.length;
  const status = ready ? "READY" : busy ? "PROCESSING" : partial ? "PARTIAL" : unknown ? "UNKNOWN" : "INCOMPLETE";
  const recoveryRequired = status === "PARTIAL" || status === "UNKNOWN";
  return {
    ...base,
    ready,
    busy,
    failure: null,
    code: status === "PARTIAL" ? "UPLOAD_PARTIAL" : status === "UNKNOWN" ? "OUTCOME_UNKNOWN" : null,
    failClosed: !ready,
    retrySafe: false,
    recoveryRequired,
    ...(recoveryRequired
      ? { recovery: { action: "inspect_existing_draft", preserveDraft: true, replay: false } }
      : {}),
    attachments: receipts,
    missing,
    partial,
    receipts,
    sizeMismatch,
    sizeUnverified,
    pageSizeVerified,
    status,
    unknown,
  };
}
