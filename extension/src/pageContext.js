// The page the student is looking at is the router's second input, and the
// cheapest useful signal we have: "what's due this week" means something
// different on Canvas than it does on a chemistry Wikipedia page.
//
// There are two entry points in this file and the difference between them is the
// whole privacy story:
//
//   getPageContext()  the page TITLE. Always sent, costs nothing, and cannot
//                     contain anything the student did not already see.
//   readPageText()    the page BODY. Never called on its own — see below.
//
// Two deliberate degradations, because a missing title must never break a turn:
//   1. No chrome.tabs (the panel rendered as a plain web page, which is how we
//      test it) -> no title, turn proceeds.
//   2. The title exists but we were not granted access to it -> chrome simply
//      leaves tab.title undefined. Same handling.
// Neither case is an error, and neither is reported as one.
//
// `activeTab` is granted by clicking the toolbar icon, which is exactly how the
// side panel is opened — so the title is available in normal use without asking
// for the broad `tabs` permission.
//
// That grant does not survive a tab switch or a reopened panel, though, and the
// first version of this file depended on it entirely. The result was a feature
// that worked only in the thirty seconds after clicking the icon: every real
// turn in the action log arrived with no page title and no page text, so
// "analyze this page" was answered blind. Host access is now an *optional*
// permission the student grants once, on a click, when they ask for a page —
// see `requestPageAccess()`. Reading is still never automatic; the grant only
// decides whether a read they asked for can succeed.

/** @returns {Promise<{title?: string}>} context for the router. Never throws. */
export async function getPageContext() {
  try {
    if (typeof chrome === 'undefined' || !chrome.tabs?.query) return {}
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
    const title = typeof tab?.title === 'string' ? tab.title.trim() : ''
    // Long titles are chrome noise ("(3) Inbox — you@school.edu"); the router
    // only needs enough to know what kind of page this is.
    return title ? { title: title.slice(0, 300) } : {}
  } catch {
    return {}
  }
}

// --------------------------------------------------------------- page body --
//
// The page body is read only when the student asks for it, in this turn, by
// doing one of exactly two things:
//
//   1. pressing **Read this page** in the panel (a one-shot arm — it applies to
//      the next question and is then spent), or
//   2. saying something that names the page: "explain this page", "rephrase this
//      paragraph". That is `utteranceReadsPage()` below.
//
// Nothing else calls `readPageText()`, which is what makes "we never read a page
// unasked" a property of the code rather than a promise in a README. The backend
// holds the same rule in `page_requested()` and is the authority: if the two ever
// disagree it refuses the text with a 400 instead of trusting us, and a
// disagreement fails visibly (the turn is answered without the page) rather than
// silently.

// Matches the backend's `_PAGE_REFERENCE` noun list. Two implementations of one
// rule is a smell; it is here because the panel has to decide whether to touch
// the page *before* it has anywhere to send it.
const PAGE_WORDS =
  /\b(?:this|the|that|current|whole|entire)\s+(?:page|article|screen|passage|paragraph|section|pdf|document|doc|text|prompt|problem|question|exercise|assignment)\b|\b(?:what (?:i'?m|im|you'?re|youre) (?:reading|looking at))\b|\b(?:on|from) (?:my|the) screen\b|\bread (?:this|that|it) (?:to me|out loud|aloud)\b|\b(?:analy[sz]e|summari[sz]e|sum up|sk[ie]m|review)\s+(?:this|that|these|those)\b|\bwhat (?:does|is) (?:this|that) (?:say|about)\b|\bthe gist of (?:this|that)\b/i

/**
 * The origins we ask for. Both schemes, because a study page is as likely to be
 * http:// as https://, and requesting them separately would mean two prompts for
 * one intent.
 */
export const PAGE_ACCESS_ORIGINS = ['http://*/*', 'https://*/*']

/**
 * Do we already hold the host access a page read needs?
 *
 * Always false outside an extension, and false before the student grants it.
 * Never throws: a panel that cannot check is a panel that should offer the
 * grant, and the request is idempotent when it turns out to be unnecessary.
 */
export async function pageAccessGranted() {
  try {
    if (!chrome?.permissions?.contains) return false
    return Boolean(await chrome.permissions.contains({ origins: PAGE_ACCESS_ORIGINS }))
  } catch {
    return false
  }
}

/**
 * Ask for host access, from a click.
 *
 * Chrome requires a user gesture here, and a spoken question is not one, so this
 * is called from the **Read this page** button and from the Allow button under a
 * reply that could not read the page. The grant covers the sites the student
 * asks about; it does not make any read happen on its own.
 *
 * -> { granted: true } | { granted: false, error: 'denied'|'gesture'|'unsupported' }
 */
export async function requestPageAccess() {
  if (!pageReadingSupported()) return { granted: false, error: 'unsupported' }
  if (!chrome?.permissions?.request) return { granted: false, error: 'unsupported' }
  try {
    const granted = await chrome.permissions.request({ origins: PAGE_ACCESS_ORIGINS })
    return granted ? { granted: true } : { granted: false, error: 'denied' }
  } catch (err) {
    // The usual cause is no user gesture. It is reported rather than swallowed,
    // because the fix in that case is a click, not another question.
    return { granted: false, error: 'gesture', message: String(err?.message || err) }
  }
}

/** Did the student name the page they are looking at? */
export function utteranceReadsPage(text) {
  // No `g` flag: a global regex carries `lastIndex` between calls and would
  // start returning false every other turn.
  return PAGE_WORDS.test(String(text || ''))
}

/** How much of a page we will ever send. The backend caps it at the same number. */
export const PAGE_TEXT_MAX_CHARS = 12000

/** Can this browser do it at all? (False when the panel runs as a plain page.) */
export function pageReadingSupported() {
  return typeof chrome !== 'undefined' && Boolean(chrome.scripting?.executeScript)
}

/**
 * Runs INSIDE the page, so it must be self-contained: Chrome serialises this
 * function's source, and nothing from this module travels with it.
 */
function extractPageText(limit) {
  const root =
    document.querySelector('article, main, [role="main"], #content, .content') ||
    document.body
  if (!root) return { text: '', length: 0, source: 'none' }
  // innerText, not textContent: it is what a reader would see, so it skips
  // <script>, <style> and visually hidden nodes without us having to list them.
  const raw = (root.innerText || '')
    .replace(/[\t\u00a0]+/g, ' ')
    .replace(/ {2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  return {
    text: raw.slice(0, limit),
    length: raw.length,
    source: root === document.body ? 'body' : 'main',
  }
}

/**
 * Read the active tab's text, once, for the turn the student invoked it for.
 *
 * Never throws: every failure comes back as `{ error }` with a code the panel
 * turns into a sentence, because a page we cannot read must not break the turn —
 * the student still gets an answer, just without the page.
 *
 * @returns {Promise<{text: string, title: string, url: string, length: number,
 *                    truncated: boolean}|{error: string, message?: string}>}
 */
export async function readPageText() {
  if (!pageReadingSupported()) return { error: 'unsupported' }

  let tab
  try {
    ;[tab] = await chrome.tabs.query({ active: true, currentWindow: true })
  } catch {
    return { error: 'unsupported' }
  }
  if (!tab || typeof tab.id !== 'number') return { error: 'no-tab' }

  let payload
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractPageText,
      args: [PAGE_TEXT_MAX_CHARS],
    })
    payload = results?.[0]?.result
  } catch (err) {
    // Chrome refuses to inject into chrome:// pages, the Web Store, and any site
    // we hold no permission for. The two cases look identical here, so the flag
    // is what separates "this page cannot be read" from "you have not granted
    // access yet" - the panel offers a fix for the second and an explanation for
    // the first.
    const granted = await pageAccessGranted()
    return {
      error: 'blocked',
      needsPermission: !granted,
      message: String(err?.message || err),
    }
  }

  if (!payload || !payload.text) return { error: 'empty' }
  return {
    text: payload.text,
    title: String(tab.title || '').slice(0, 300),
    url: String(tab.url || '').slice(0, 500),
    length: payload.length,
    truncated: payload.length > payload.text.length,
  }
}
