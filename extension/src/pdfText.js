// Reading a PDF the student hands us, without a permission, a server or a guess.
//
// A PDF opened as a tab cannot be read at all: Chrome hands it to its own viewer,
// which draws the pages and puts no text in the DOM (measured: an
// `application/pdf` embed with 0 characters of innerText and no canvas). So the
// bytes have to come from somewhere else, and the one place a local file can come
// from is the student's own hand - a file picker. They already know which document
// they mean; this just lets them say so.
//
// Two things live in this file rather than in the panel:
//
//   1. **The checks that happen before any parsing.** A 400 MB file is refused by
//      size instead of by freezing the panel, and something that is not a PDF is
//      refused by type rather than by a parser error the student cannot act on.
//   2. **The judgement about what came out.** Most PDFs give text; a scan gives
//      almost none, and "almost none" has to be *said* rather than summarised from
//      nothing. Measured on a real 42-page reading that was pages of images: 83
//      characters of text, which is a caption or two.
//
// The parser itself is injected (`load`) so this file can be tested without
// pdf.js, and so the one browser-only line - pointing pdf.js at its worker -
// lives somewhere that does not have to run in Node. See `pdfLoader.js`.

/** Files larger than this are refused before anything is parsed. */
export const PDF_MAX_BYTES = 60 * 1024 * 1024

/** Pages we will walk. A 3,000-page scan is not a study aid, it is a freeze. */
export const PDF_MAX_PAGES = 300

/**
 * Average characters per page below which we call it a scan.
 *
 * A page of prose is 1,500-3,000 characters; a page of slides 100-400; a page of
 * images, 0-20. The floor is deliberately low, because the cost of calling a real
 * reading a scan is that the student pastes it by hand for nothing.
 */
export const PDF_MIN_CHARS_PER_PAGE = 25

/**
 * What is wrong with this file, before we try to parse it. -> '' when nothing is.
 *
 * Type is checked by MIME *and* name: Chrome reports `application/pdf` for a
 * picked PDF, but a file dragged from some places arrives with an empty type, and
 * the extension is the thing the student can see.
 */
export function pdfFileProblem(file) {
  if (!file) return 'missing'
  const name = String(file.name || '')
  const type = String(file.type || '')
  const looksPdf = type === 'application/pdf' || /\.pdf$/i.test(name)
  if (!looksPdf) return 'not-pdf'
  if (Number(file.size) > PDF_MAX_BYTES) return 'too-big'
  if (Number(file.size) === 0) return 'empty'
  return ''
}

/** One page of a PDF's text: flattened, because a prompt gets one line per page. */
export function cleanPageText(raw) {
  return String(raw || '')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * Is what came out of the parser worth sending? -> '' or an error code.
 *
 * The two failures are different sentences for the student: `no-text` means the
 * file has no text layer at all, `scan` means it has one so thin that it is
 * captions on pictures. Both are answered with "I cannot read images", and neither
 * is allowed to reach a model as if it were the document.
 */
export function extractionProblem(pages, chars) {
  if (!chars) return 'no-text'
  if (Number(pages) >= 3 && chars / Number(pages) < PDF_MIN_CHARS_PER_PAGE) return 'scan'
  return ''
}

/**
 * Pull the text out of a picked PDF. -> { text, pages, chars, truncated } or { error }.
 *
 * `load` is `async (file) => pdfDocumentInstance` - see `pdfLoader.js`. It is a
 * parameter because this module must be testable in Node and because the worker
 * wiring is a bundler concern, not a reading one.
 */
export async function extractPdfText(file, load, maxChars = 12000) {
  const problem = pdfFileProblem(file)
  if (problem) return { error: problem }

  let doc
  try {
    doc = await load(file)
  } catch (err) {
    // pdf.js names its own failures; a password is the one the student can fix.
    const name = String(err?.name || '')
    return { error: name === 'PasswordException' ? 'password' : 'failed' }
  }

  const declared = Number(doc?.numPages) || 0
  const pageCount = Math.min(declared, PDF_MAX_PAGES)
  const pages = []
  let total = 0
  let stoppedEarly = false
  try {
    for (let number = 1; number <= pageCount; number += 1) {
      const page = await doc.getPage(number)
      const content = await page.getTextContent()
      // pdf.js hands back positioned runs, not sentences: joining them with a
      // space is what everyone does, and it is right far more often than trying
      // to rebuild lines from coordinates.
      const text = cleanPageText((content?.items || []).map((item) => item?.str || '').join(' '))
      pages.push(text)
      total += text.length
      // Stop early once there is far more than we could ever send: a 300-page
      // book should not be parsed to the end for text that gets cut off anyway.
      if (total > maxChars * 2) {
        stoppedEarly = true
        break
      }
    }
  } catch {
    return { error: 'failed' }
  } finally {
    // Freed whether or not the walk finished: these documents hold the whole
    // file in memory.
    try {
      await doc?.destroy?.()
    } catch {
      /* a document that cannot be closed is already out of our hands */
    }
  }

  const body = pages.join('\n\n').trim()
  const chars = body.length
  const problem2 = extractionProblem(pages.length, chars)
  if (problem2) return { error: problem2, pages: pages.length, chars }

  return {
    text: body.slice(0, maxChars),
    pages: pages.length,
    chars,
    // True for any of the three ways we did not read the whole file: the text was
    // cut to the limit, the walk stopped because there was already more than the
    // limit, or the document has more pages than we will open. Reporting only the
    // first would let a 10,000-page document be described as "read in full".
    truncated: chars > maxChars || stoppedEarly || declared > pageCount,
  }
}
