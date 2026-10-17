// Reading a picked PDF, tested without pdf.js.
//
// The parser is injected into `extractPdfText`, so everything that decides what a
// student sees is testable here: which files are refused before parsing, what
// comes out of a document, when the output is too thin to be a document at all,
// and that a failure to parse is not mistaken for an empty page.
//
// The numbers in these tests are not invented. The scan case is a real 42-page
// reading on this machine that yielded 83 characters, and the transcript case is a
// real one-page PDF that yielded 2,609.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PDF_MAX_BYTES,
  PDF_MAX_PAGES,
  PDF_MIN_CHARS_PER_PAGE,
  cleanPageText,
  extractPdfText,
  extractionProblem,
  pdfFileProblem,
} from '../src/pdfText.js'

/** A File-shaped object. Only name, type and size are read by the guards. */
const file = (name = 'transcript.pdf', type = 'application/pdf', size = 1024) => ({
  name,
  type,
  size,
})

/** A pdf.js document proxy whose pages are arrays of text runs. */
function fakeDocument(pages, { numPages = pages.length, pageThrows = 0 } = {}) {
  const calls = { getPage: [], destroyed: false }
  return {
    calls,
    doc: {
      numPages,
      async getPage(number) {
        calls.getPage.push(number)
        if (number === pageThrows) throw new Error('damaged page')
        return {
          async getTextContent() {
            return { items: (pages[number - 1] || []).map((str) => ({ str })) }
          },
        }
      },
      async destroy() {
        calls.destroyed = true
      },
    },
  }
}

test('a file that is not a PDF is refused by name or type, before parsing', () => {
  assert.equal(pdfFileProblem(null), 'missing')
  assert.equal(pdfFileProblem(file('notes.txt', 'text/plain')), 'not-pdf')
  assert.equal(pdfFileProblem(file('scan.pdf', '')), '', 'a PDF with no MIME type is still a PDF')
  assert.equal(pdfFileProblem(file('WEIRD.PDF', 'application/octet-stream')), '')
  assert.equal(pdfFileProblem(file('huge.pdf', 'application/pdf', PDF_MAX_BYTES + 1)), 'too-big')
  assert.equal(pdfFileProblem(file('empty.pdf', 'application/pdf', 0)), 'empty')
  assert.equal(pdfFileProblem(file()), '')
})

test('a refused file is never handed to the parser', async () => {
  let loaded = 0
  const load = async () => {
    loaded += 1
    return fakeDocument([['text']]).doc
  }
  assert.equal((await extractPdfText(file('notes.txt', 'text/plain'), load)).error, 'not-pdf')
  assert.equal((await extractPdfText(file('big.pdf', 'application/pdf', PDF_MAX_BYTES + 1), load)).error, 'too-big')
  assert.equal(loaded, 0)
})

test('pages are flattened to one line each and joined in order', async () => {
  const { doc } = fakeDocument([
    ['Bridgewater', '-', 'Raritan', 'High', 'School'],
    ['GPA', '4.0'],
  ])
  const result = await extractPdfText(file(), async () => doc)
  assert.equal(result.text, 'Bridgewater - Raritan High School\n\nGPA 4.0')
  assert.equal(result.pages, 2)
  assert.equal(result.chars, result.text.length)
  assert.equal(result.truncated, false)
})

test('a document longer than we can send is capped, and stops being parsed early', async () => {
  const long = Array.from({ length: 40 }, () => ['x'.repeat(2000)])
  const { doc, calls } = fakeDocument(long)
  const result = await extractPdfText(file(), async () => doc, 12000)
  assert.equal(result.text.length, 12000)
  assert.equal(result.truncated, true)
  assert.ok(result.chars > result.text.length, 'how much there was is still reported')
  // A 40-page document should not be parsed to the end for text that is cut off:
  // it stops once it has twice what it could send.
  assert.ok(calls.getPage.length < long.length, `walked ${calls.getPage.length} of 40 pages`)
})

test('a page cap stops a pathological document, and says it was not all read', async () => {
  const pages = Array.from({ length: PDF_MAX_PAGES }, () => ['one short page of real text'])
  const { doc, calls } = fakeDocument(pages, { numPages: 10000 })
  const result = await extractPdfText(file(), async () => doc)
  assert.equal(calls.getPage.length, PDF_MAX_PAGES)
  assert.equal(result.pages, PDF_MAX_PAGES)
  assert.equal(
    result.truncated,
    true,
    'a document with more pages than we opened must not be described as read in full',
  )
})

test('a scan is called a scan, not summarised from nothing', async () => {
  // The real case: 42 pages of images, 83 characters of text.
  assert.equal(extractionProblem(42, 83), 'scan')
  assert.equal(extractionProblem(3, 30), 'scan')
  // Below three pages there is not enough evidence to shame the student's file.
  assert.equal(extractionProblem(2, 30), '')
  assert.equal(extractionProblem(1, 20), '')
  // A page of slides is thin but real.
  assert.equal(extractionProblem(10, 10 * PDF_MIN_CHARS_PER_PAGE), '')
  assert.equal(extractionProblem(10, 1000), '')
  assert.equal(extractionProblem(4, 0), 'no-text')
})

test('a document with no text at all says so, rather than answering emptily', async () => {
  const { doc } = fakeDocument([[], [], []])
  assert.equal((await extractPdfText(file(), async () => doc)).error, 'no-text')
})

test('a scan comes back as an error the panel can explain', async () => {
  const pages = Array.from({ length: 42 }, () => ['page 1 of 42'])
  const { doc } = fakeDocument(pages)
  const result = await extractPdfText(file(), async () => doc)
  assert.equal(result.error, 'scan')
  assert.equal(result.pages, 42)
})

test('a locked PDF is its own sentence, and a broken one its own', async () => {
  const locked = Object.assign(new Error('need a password'), { name: 'PasswordException' })
  assert.equal((await extractPdfText(file(), async () => { throw locked })).error, 'password')
  assert.equal((await extractPdfText(file(), async () => { throw new Error('corrupt') })).error, 'failed')
})

test('a page that blows up mid-walk is a failure, and the document is still freed', async () => {
  const { doc, calls } = fakeDocument([['fine'], ['also fine']], { pageThrows: 2 })
  const result = await extractPdfText(file(), async () => doc)
  assert.equal(result.error, 'failed')
  assert.equal(calls.destroyed, true, 'an open document holds the whole file in memory')
})

test('the document is closed even when everything worked', async () => {
  const { doc, calls } = fakeDocument([['a real page of text '.repeat(40)]])
  await extractPdfText(file(), async () => doc)
  assert.equal(calls.destroyed, true)
})

test('cleanPageText collapses runs without eating the words', () => {
  assert.equal(cleanPageText('  a \n\n b\t\tc  '), 'a b c')
  assert.equal(cleanPageText(null), '')
  assert.equal(cleanPageText(undefined), '')
})
