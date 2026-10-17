// Reading the page, tested without a browser.
//
// The failure these exist for is the one the action log showed: every real turn
// arrived with no page title and no page text, because the panel depended on an
// `activeTab` grant that only exists for the thirty seconds after clicking the
// toolbar icon. A read that fails for want of a permission is recoverable, and
// the panel now says so and offers the grant - so the three things worth testing
// are which reads are recoverable, what asking returns, and that a read is still
// never attempted unless the student pointed at the page.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PAGE_ACCESS_ORIGINS,
  PAGE_TEXT_MAX_CHARS,
  documentExportUrl,
  isCanvasOnly,
  pageAccessGranted,
  pageReadingSupported,
  readPageText,
  requestPageAccess,
  utteranceReadsPage,
} from '../src/pageContext.js'

/** A chrome whose answers each test decides. Everything is optional by design. */
function stubChrome({
  hasPermissions = true,
  contains = async () => true,
  request = async () => true,
  tab = { id: 7, title: 'Photosynthesis — Wikipedia', url: 'https://en.wikipedia.test/wiki/x' },
  extract = [{ result: { text: 'Plants convert light into sugar.', length: 31, source: 'main' } }],
  injectError = null,
} = {}) {
  const calls = { contains: 0, request: null, injected: null }
  globalThis.chrome = {
    tabs: { query: async () => [tab] },
    scripting: hasPermissions
      ? {
          executeScript: async (args) => {
            calls.injected = args
            if (injectError) throw new Error(injectError)
            return extract
          },
        }
      : undefined,
    permissions: {
      contains: async (query) => {
        calls.contains += 1
        return contains(query)
      },
      request: async (query) => {
        calls.request = query
        return request(query)
      },
    },
  }
  return calls
}

const withoutChrome = () => {
  delete globalThis.chrome
}

test('a phrase that points at the page is a page read', () => {
  for (const text of [
    'analyze this page',
    'analyse this page',
    'summarize this',
    'summarise this article',
    'give me the gist of this',
    'what does this say',
    'what does this page say',
    'explain the page I am reading',
    'read this out loud',
  ]) {
    assert.equal(utteranceReadsPage(text), true, `"${text}" should read the page`)
  }
})

test('a phrase that does not point at the page is not one', () => {
  for (const text of [
    "what's due this week",
    'explain entropy',
    'summarize it',
    'analyze my essay',
    'plan my week',
    'remember my midterm is on the 12th',
    '',
    null,
  ]) {
    assert.equal(utteranceReadsPage(text), false, `"${text}" should not read the page`)
  }
})

test('outside an extension nothing claims to be supported', () => {
  withoutChrome()
  assert.equal(pageReadingSupported(), false)
})

test('access is reported, not assumed', async () => {
  stubChrome({ contains: async () => false })
  assert.equal(await pageAccessGranted(), false)

  stubChrome()
  assert.equal(await pageAccessGranted(), true)

  // A permissions API that throws must not break a turn: the panel asks, and
  // being wrong about the state costs one extra prompt rather than an error.
  stubChrome({ contains: async () => { throw new Error('no permissions API') } })
  assert.equal(await pageAccessGranted(), false)

  withoutChrome()
  assert.equal(await pageAccessGranted(), false)
})

test('asking for access asks for both schemes and reports the answer', async () => {
  const calls = stubChrome({ request: async () => true })
  assert.deepEqual(await requestPageAccess(), { granted: true })
  assert.deepEqual(calls.request, { origins: PAGE_ACCESS_ORIGINS })

  stubChrome({ request: async () => false })
  assert.deepEqual(await requestPageAccess(), { granted: false, error: 'denied' })
})

test('a refused gesture is a sentence, not a silent failure', async () => {
  // Chrome throws when request() is called outside a click. The panel turns this
  // into "use the button", so the code has to survive the throw to say anything.
  stubChrome({ request: async () => { throw new Error('must be called during a user gesture') } })
  const result = await requestPageAccess()
  assert.equal(result.granted, false)
  assert.equal(result.error, 'gesture')
})

test('asking is impossible without a browser, and says so', async () => {
  withoutChrome()
  assert.deepEqual(await requestPageAccess(), { granted: false, error: 'unsupported' })
  stubChrome({ hasPermissions: false })
  assert.deepEqual(await requestPageAccess(), { granted: false, error: 'unsupported' })
})

test('a blocked read says whether a grant would fix it', async () => {
  // No grant: the panel offers "Allow page access and retry".
  stubChrome({
    tab: { id: 7 },
    injectError: 'Cannot access contents of the page',
    contains: async () => false,
  })
  const denied = await readPageText()
  assert.equal(denied.error, 'blocked')
  assert.equal(denied.needsPermission, true)

  // Granted, and still blocked: a chrome:// page or the web store. No button
  // would help, so none is offered.
  stubChrome({
    tab: { id: 7 },
    injectError: 'Cannot access a chrome:// URL',
    contains: async () => true,
  })
  const hopeless = await readPageText()
  assert.equal(hopeless.error, 'blocked')
  assert.equal(hopeless.needsPermission, false)
})

const DOC_URL = 'https://docs.google.com/document/d/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg/edit?tab=t.0'
const SHEET_URL = 'https://docs.google.com/spreadsheets/d/1KdK7bIKoOnUeuWAbloDYkLpBttaH22u-wQ9z85Ec0lo/edit#gid=0'

test('a Google file is read as a document, not as a page', () => {
  assert.equal(
    documentExportUrl(DOC_URL),
    'https://docs.google.com/document/d/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg/export?format=txt',
  )
  assert.equal(
    documentExportUrl(SHEET_URL),
    'https://docs.google.com/spreadsheets/d/1KdK7bIKoOnUeuWAbloDYkLpBttaH22u-wQ9z85Ec0lo/export?format=csv',
  )
  // The /u/<n>/ account prefix is what a multi-account browser produces.
  assert.match(
    documentExportUrl('https://docs.google.com/document/u/2/d/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg/edit') || '',
    /\/document\/d\/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg\/export\?format=txt$/,
  )
  for (const url of [
    'https://docs.google.com/presentation/d/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg/edit',
    'https://example.test/document/d/1fRWUpmqbdOFOuWF_LOHEHkOCsAGW_esVWmTYYH93Asg/edit',
    'https://docs.google.com/document/d/short/edit',
    'https://docs.google.com/',
    '',
    null,
    undefined,
  ]) {
    assert.equal(documentExportUrl(url), null, `${url} should not export`)
  }
})

test('drawn text is refused, and a chart beside an article is not', () => {
  // Measured on a live Google Doc: coverage ~1.0, 1,152 characters of menu bar.
  assert.equal(isCanvasOnly(1, 1152), true)
  assert.equal(isCanvasOnly(0.62, 900), true)
  // An article with a graph: a big canvas, but real prose to go with it.
  assert.equal(isCanvasOnly(0.6, 4000), false)
  assert.equal(isCanvasOnly(0.1, 40), false)
  // Defensive: missing numbers must not be read as "canvas".
  assert.equal(isCanvasOnly(undefined, undefined), false)
})

test('a doc with no grant asks for one instead of injecting', async () => {
  const calls = stubChrome({ tab: { id: 7, url: DOC_URL, title: 'Resume - Google Docs' }, contains: async () => false })
  const read = await readPageText()
  assert.equal(read.error, 'blocked')
  assert.equal(read.needsPermission, true)
  assert.equal(calls.injected, null, 'a doc must not be injected into')
})

test('a granted doc is read through its own text export', async () => {
  const calls = stubChrome({ tab: { id: 7, url: DOC_URL, title: 'Resume - Google Docs' } })
  const before = globalThis.fetch
  let requested = null
  globalThis.fetch = async (url, options) => {
    requested = { url, options }
    // The real export: a byte-order mark, CRLF, trailing whitespace.
    return { ok: true, text: async () => '\uFEFFJane Doe\r\nSkills: Python\r\n\r\n' }
  }
  try {
    const read = await readPageText()
    assert.equal(read.text, 'Jane Doe\nSkills: Python')
    assert.equal(read.source, 'export')
    assert.equal(read.title, 'Resume - Google Docs')
    assert.equal(requested.url, documentExportUrl(DOC_URL))
    assert.equal(requested.options.credentials, 'include', 'a private doc needs the session cookie')
    assert.equal(calls.injected, null, 'the canvas was never read')
  } finally {
    globalThis.fetch = before
  }
})

test('an export that fails is reported, not replaced by the menu bar', async () => {
  stubChrome({ tab: { id: 7, url: DOC_URL, title: 'Resume - Google Docs' } })
  const before = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 401, text: async () => '' })
  try {
    assert.equal((await readPageText()).error, 'export')
  } finally {
    globalThis.fetch = before
  }
  // A throwing fetch, and an empty body, are the same answer: we could not read it.
  globalThis.fetch = async () => { throw new Error('offline') }
  try {
    assert.equal((await readPageText()).error, 'export')
  } finally {
    globalThis.fetch = before
  }
  globalThis.fetch = async () => ({ ok: true, text: async () => '   ' })
  try {
    assert.equal((await readPageText()).error, 'export')
  } finally {
    globalThis.fetch = before
  }
})

test('a long export is capped, like any other page text', async () => {
  stubChrome({ tab: { id: 7, url: DOC_URL, title: 'Long' } })
  const before = globalThis.fetch
  globalThis.fetch = async () => ({ ok: true, text: async () => 'x'.repeat(PAGE_TEXT_MAX_CHARS + 500) })
  try {
    const read = await readPageText()
    assert.equal(read.text.length, PAGE_TEXT_MAX_CHARS)
    assert.equal(read.truncated, true)
  } finally {
    globalThis.fetch = before
  }
})

test('a PDF in the viewer is named as a PDF, not as an empty page', async () => {
  // The shape measured in Chrome: application/pdf embed, no text, no canvas.
  stubChrome({
    tab: { id: 7, url: 'https://arxiv.org/pdf/1706.03762', title: 'attention.pdf' },
    extract: [{ result: { text: '', length: 0, source: 'none', canvasCoverage: 0, pdf: true } }],
  })
  assert.equal((await readPageText()).error, 'pdf')

  // A genuinely blank page is still the vaguer answer, because it is not a PDF.
  stubChrome({
    tab: { id: 7, url: 'https://example.test/blank', title: 'blank' },
    extract: [{ result: { text: '', length: 0, source: 'none', canvasCoverage: 0, pdf: false } }],
  })
  assert.equal((await readPageText()).error, 'empty')
})

test('a canvas-drawn page is refused before the model sees it', async () => {
  // The exact shape of the Google Docs failure, one layer down: a big canvas and
  // innerText that holds the toolbar.
  stubChrome({
    tab: { id: 7, url: 'https://slides.example.test/deck', title: 'Slides' },
    extract: [{ result: { text: 'File Edit View 1 2 3 4', length: 21, source: 'body', canvasCoverage: 0.97 } }],
  })
  assert.equal((await readPageText()).error, 'canvas')
})

test('a page with a chart canvas still reads as a page', async () => {
  stubChrome({
    tab: { id: 7, url: 'https://example.test/article', title: 'Article' },
    extract: [
      {
        result: {
          text: 'Prose about the results. '.repeat(200),
          length: 5000,
          source: 'main',
          canvasCoverage: 0.55,
        },
      },
    ],
  })
  const read = await readPageText()
  assert.equal(read.error, undefined)
  assert.ok(read.text.startsWith('Prose about the results.'))
})

test('a granted read returns the text, its true length and the url', async () => {
  const calls = stubChrome()
  const read = await readPageText()
  assert.equal(read.text, 'Plants convert light into sugar.')
  assert.equal(read.length, 31)
  assert.equal(read.truncated, false)
  assert.equal(read.url, 'https://en.wikipedia.test/wiki/x')
  assert.equal(calls.injected.target.tabId, 7)
  assert.equal(read.needsPermission, undefined)
})

test('a file on disk is refused, and no Allow button is dangled', async () => {
  // Measured in a browser: a PDF is an application/pdf embed with 0 characters of
  // innerText, so even with file access switched on there is nothing to read. The
  // panel must therefore not offer a permission that cannot help.
  const calls = stubChrome({
    tab: { id: 7, url: 'file:///Users/someone/Documents/transcript.pdf', title: 'transcript.pdf' },
  })
  const read = await readPageText()
  assert.equal(read.error, 'file')
  assert.equal(read.needsPermission, undefined, 'a grant cannot make a local file readable')
  assert.equal(calls.injected, null)
})

test('an empty page and a missing tab are different failures from a blocked one', async () => {
  stubChrome({ extract: [{ result: { text: '', length: 0, source: 'none' } }] })
  assert.equal((await readPageText()).error, 'empty')

  stubChrome({ tab: {} })
  assert.equal((await readPageText()).error, 'no-tab')

  withoutChrome()
  assert.equal((await readPageText()).error, 'unsupported')
})

test('a tab that cannot be queried does not throw into the turn', async () => {
  stubChrome()
  globalThis.chrome.tabs.query = async () => { throw new Error('no tabs API') }
  assert.equal((await readPageText()).error, 'unsupported')
})
