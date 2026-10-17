// The one browser-only half of PDF reading: pointing pdf.js at its worker.
//
// Split out from `pdfText.js` on purpose. This file imports the parser and a
// Vite-specific `?url` worker asset, so importing it in Node would fail; the
// extraction logic next door takes the loader as an argument and stays testable.
//
// The worker is bundled rather than fetched from anywhere: an extension page's
// CSP forbids remote scripts, and a worker built from `chrome-extension://` is
// same-origin, so it is allowed. `isEvalSupported: false` keeps pdf.js away from
// `new Function`, which MV3 forbids for the same reason.
//
// Standard font data and cmaps are deliberately *not* configured. They exist for
// rendering and for exotic character maps; text extraction reads the encoding
// tables in the file, and shipping the two directories would add megabytes for
// fonts we never draw. Verified against real documents rather than assumed.

import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist'
// Vite emits this as an asset and gives us its URL; the file never leaves the
// machine, and no code is loaded from a network at run time.
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerSrc

/** Read a File as bytes. -> Uint8Array. */
async function fileBytes(file) {
  const buffer = await file.arrayBuffer()
  return new Uint8Array(buffer)
}

/**
 * Open a picked PDF. -> a pdf.js document proxy, for `extractPdfText` to walk.
 *
 * Kept to the smallest possible surface: the extraction, the caps, the error
 * codes and the scan judgement all live in `pdfText.js`, where they are tested.
 */
export async function loadPdfDocument(file) {
  const data = await fileBytes(file)
  return getDocument({
    data,
    isEvalSupported: false,
    // A picked file is not a URL: nothing here should try to re-fetch it.
    disableAutoFetch: true,
  }).promise
}
