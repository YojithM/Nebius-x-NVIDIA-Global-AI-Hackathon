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
