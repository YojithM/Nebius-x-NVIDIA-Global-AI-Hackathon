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
  /\b(?:this|the|that|current|whole|entire)\s+(?:page|article|screen|passage|paragraph|section|pdf|document|doc|text|prompt|problem|question|exercise|assignment)\b|\b(?:what (?:i'?m|im|you'?re|youre) (?:reading|looking at))\b|\b(?:on|from) (?:my|the) screen\b|\bread (?:this|that|it) (?:to me|out loud|aloud)\b/i

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
    // Chrome refuses to inject into chrome:// pages, the Web Store, and any tab
    // this extension was not invoked on. `activeTab` is granted by clicking the
    // toolbar icon, so a student who opened the panel from the side-panel button
    // is a legitimate "blocked" — the message tells them what to click.
    return { error: 'blocked', message: String(err?.message || err) }
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
