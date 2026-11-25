import {
  cancelAistudioGeneration,
  ensureAistudioModelAndBilling,
  inspectAistudioPage,
  prepareAistudioInput,
  readAistudioGenerationState,
  submitAistudioPrompt,
} from "./aistudio-page.mjs";
import { prepareAistudioUpload, readAistudioUploadState } from "./aistudio-upload.mjs";
import { ensureGeminiModel } from "./gemini-model.mjs";
import {
  cancelGeminiGeneration,
  inspectGeminiPage,
  readGeminiGenerationState,
  submitGeminiPrompt,
} from "./gemini-page.mjs";
import { prepareGeminiUpload, readGeminiUploadState } from "./gemini-upload.mjs";
import { normalizeProvider } from "./providers.mjs";

const ADAPTERS = Object.freeze({
  gemini: Object.freeze({
    attachmentsEnabled: true,
    billingMode: "gemini-web-account",
    cancel: cancelGeminiGeneration,
    ensureReady: ensureGeminiModel,
    inspect: inspectGeminiPage,
    prepareUpload: prepareGeminiUpload,
    readGenerationState: readGeminiGenerationState,
    readUploadState: readGeminiUploadState,
    submit: submitGeminiPrompt,
  }),
  aistudio: Object.freeze({
    attachmentsEnabled: true,
    billingMode: "subscription",
    cancel: cancelAistudioGeneration,
    prepareInput: prepareAistudioInput,
    ensureReady: ensureAistudioModelAndBilling,
    inspect: inspectAistudioPage,
    prepareUpload: prepareAistudioUpload,
    readGenerationState: readAistudioGenerationState,
    readUploadState: readAistudioUploadState,
    submit: submitAistudioPrompt,
  }),
});

export function pageAdapter(provider) {
  return ADAPTERS[normalizeProvider(provider)];
}

export function hasVerifiedUpload(provider) {
  return pageAdapter(provider).attachmentsEnabled === true;
}

export { ADAPTERS as PAGE_ADAPTERS };
