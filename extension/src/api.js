import { BACKEND_URL, BACKEND_TOKEN } from './config.js'

const LOCAL = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(BACKEND_URL)

// The origin that has to be in manifest.json host_permissions for a hosted
// backend. A malformed URL is left as-is rather than throwing: this only feeds
// an error message, and a message that itself crashes is worse than useless.
const ORIGIN = (() => {
  try {
    return new URL(BACKEND_URL).origin
  } catch {
    return BACKEND_URL
  }
})()

export class BackendUnreachableError extends Error {
  constructor(cause) {
    super(
      LOCAL
        ? `Can't reach the backend at ${BACKEND_URL}. Start it with: python3 backend/server.py`
        : // A hosted backend fails in three ways that look identical from here,
          // and the third one is the one people lose an hour to: the backend is
          // healthy, the URL is right, and Chrome refused the fetch because the
          // origin was never listed in host_permissions. Naming it is the whole
          // point of this message.
          `Can't reach the deployed backend at ${BACKEND_URL}. It may be stopped, still starting, or the URL may have changed since the build. If it is up, suspect a missing host permission: ${ORIGIN}/* must be listed in manifest.json host_permissions, or Chrome blocks the request before it leaves the browser.`,
    )
    this.name = 'BackendUnreachableError'
    this.cause = cause
  }
}

/** Headers for one request, adding our backend token when one was built in. */
function headers(extra) {
  const merged = { ...extra }
  if (BACKEND_TOKEN) merged.Authorization = `Bearer ${BACKEND_TOKEN}`
  return merged
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${BACKEND_URL}${path}`, {
      ...options,
      headers: headers(options.headers),
    })
  } catch (cause) {
    // fetch only rejects on network-level failure, which here almost always
    // means the backend simply isn't running yet.
    throw new BackendUnreachableError(cause)
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    let message = detail
    try {
      message = JSON.parse(detail).error ?? detail
    } catch {
      /* not JSON, use the raw body */
    }
    // A 401 is a setup mistake worth naming precisely: the backend is reachable
    // and enforcing a token, which means the build either has none or the wrong
    // one. Otherwise the student sees "Backend returned 401" and has to guess.
    if (response.status === 401) {
      message = BACKEND_TOKEN
        ? 'the backend rejected this build\'s token (rebuild with the current VITE_BACKEND_TOKEN)'
        : 'this backend requires a token (rebuild with VITE_BACKEND_TOKEN set)'
    }
    throw new Error(`Backend returned ${response.status}: ${String(message).slice(0, 200)}`)
  }

  return response.json()
}

/**
 * Send one spoken turn to the backend and get the assistant's reply.
 *
 * The body is the router's input contract, so the names matter:
 *   transcript      the student's words (required, non-empty)
 *   page_title      what they are looking at, when we could read it (optional)
 *   history         the last few messages, for context
 *   session_summary a note of the conversation older than `history`, written by
 *                   /summarize and held by the panel - see summarizeSession()
 *   page_text       the page BODY, only when the student invoked a page read
 *   page_invoked    "the student asked for the page this turn" (never sticky)
 *   previous_reply  our own last reply, so "say that more simply" has a referent
 *
 * Page text is special: the backend refuses it with a 400 unless this turn
 * carries evidence the student asked for it (`page_invoked`, or an utterance that
 * names the page), and it ignores it for any skill that did not declare it needs
 * the page. So an accidental call still cannot leak a page into a prompt.
 *
 * Returns the backend's JSON: { reply, skill, args, needs_confirmation,
 * confirmation, page_text_chars, page_text_used, tier, model, cost_usd,
 * latency_ms, steps, max_steps, tools, sources, steps_truncated, skill_saved,
 * cost_warning, search_configured, session, spend, router: {...} }, where each
 * entry of
 * `sources` is { title, url } — the pages the plan actually read, in the order
 * that `[n]` in the reply refers to them. It is empty on every turn that did not
 * search, and it is the only thing in this payload the panel turns into a link:
 * a URL the model typed into its reply is prose. Each entry of `tools` is
 * { tool, ok, args_chars, result_chars, reason } — the plan's tool calls, with
 * counts rather than contents.
 *
 * Three of those fields are Step 10's, and two arrived with Step 11:
 *   confirmation  { id, skill, prompt, expires_in_seconds } when the backend is
 *                 waiting on permission, null otherwise. Answer it with
 *                 answerApproval() — the id is what makes a "yes" run the action
 *                 that was shown rather than whatever gets said next.
 *
 *                 One kind of question carries more: when `kind` is "skill"
 *                 (Step 11), the backend has drafted a whole skill file and is
 *                 waiting for the student to read it. Then `path` is where it
 *                 would be saved and `draft` is the file's exact text — the same
 *                 bytes that get written on a yes, so a preview that differed
 *                 from what is saved is not possible.
 *   skill_saved   { name, path, skills, errors } when a skill file was written on
 *                 this turn — the library it landed in, including whether the
 *                 reload found any broken file. Null on every other reply.
 *   cost_warning  { cost_usd, ceiling_usd, over_by_usd } when this turn cost more
 *                 than `warn_over_usd_per_request` — the number in the config that
 *                 nothing read until Step 11. The sentence saying so is already
 *                 appended to the reply, because the reply is what gets spoken;
 *                 this is the same fact in numbers, for the meta line and the log.
 *                 Null on every turn that stayed under the ceiling.
 *   session       { demo, used, remaining, cap } — this visitor's question
 *                 budget, which only exists in /demo mode.
 *   spend         { turns, calls, cost_usd, credit_usd, remaining_credit_usd,
 *                 by_tier, ... } — what the backend has spent, from the provider's
 *                 own usage numbers rather than an estimate.
 */
export function sendTurn(
  {
    transcript,
    history = [],
    sessionSummary,
    pageTitle,
    pageText,
    pageUrl,
    pageInvoked,
    previousReply,
  },
  signal,
) {
  const body = { transcript, history }
  // Omitted when there is none: a turn with no note is a turn that has not
  // outgrown its message window yet, and the backend treats absence as empty.
  if (sessionSummary) body.session_summary = String(sessionSummary).slice(0, 1200)
  // Omitted rather than sent as null/"" — the backend treats a missing title as
  // "no page context", and an empty string as a string it has to accept anyway.
  if (pageTitle) body.page_title = pageTitle
  if (previousReply) body.previous_reply = String(previousReply).slice(0, 4000)
  if (pageText) {
    body.page_text = pageText
    body.page_invoked = Boolean(pageInvoked)
    if (pageUrl) body.page_url = pageUrl
  }

  return request('/turn', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

export function checkHealth(signal) {
  return request('/health', { signal })
}

/**
 * Fold a few messages that have fallen out of the window into the running note.
 *
 * Its own endpoint rather than a field on `/turn`: this describes turns the
 * student is already finished with, so it can happen after the answer arrives,
 * on its own time, and a slow or failed note never delays a reply. The note is
 * held by the panel and sent back with every turn, so the backend still keeps no
 * session - and a failure changes nothing, because the messages stay in the
 * panel and are tried again next turn. -> { summary, messages, cost_usd, ... }
 */
export function summarizeSession({ summary, messages }, signal) {
  const body = { messages }
  if (summary) body.summary = String(summary).slice(0, 1200)
  return request('/summarize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

/**
 * Ask the backend to say this reply, and get audio back.
 *
 * Not a `request()` call: that helper parses JSON, and a successful answer here
 * *is* an MP3. The key that makes this expensive lives on the backend, so this is
 * the only way the panel can have a better voice than Chrome's own - and the
 * reason a failure is thrown rather than swallowed is that the caller has a real
 * fallback to take (`speech.js` speaks it locally instead).
 *
 * Returns { blob, chars, truncated, cached }. `truncated` means the backend spoke
 * only the first part of a long reply, and saying so is the difference between a
 * feature and a lie. `cached` means this audio cost nothing because the same words
 * were spoken a moment ago.
 */
export async function speakReply(text, signal) {
  let response
  try {
    response = await fetch(`${BACKEND_URL}/speak`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text: String(text ?? '') }),
      signal,
    })
  } catch (cause) {
    throw new BackendUnreachableError(cause)
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    let message = detail
    try {
      message = JSON.parse(detail).error ?? detail
    } catch {
      /* not JSON, use the raw body */
    }
    throw new Error(String(message || `Backend returned ${response.status}`).slice(0, 200))
  }

  const blob = await response.blob()
  return {
    blob,
    chars: Number(response.headers.get('X-Cadence-Spoken-Chars') || 0),
    truncated: response.headers.get('X-Cadence-Spoken-Truncated') === '1',
    cached: response.headers.get('X-Cadence-Spoken-Cached') === '1',
  }
}

/**
 * Answer a question the backend asked: a go-ahead, or a refusal.
 *
 * Sent as its own request rather than as the next utterance, because that is what
 * it is. The backend re-runs the stored decision on a yes (`confirm`), so the
 * action that happens is the one that was shown — routing the word "yes" as a
 * fresh request would let the model decide again, possibly differently.
 *
 * Returns the same payload as sendTurn(). A refusal still comes back as a
 * reply, in words, with a cost of exactly zero.
 */
export function answerApproval(approvalId, approved, signal) {
  const body = approved ? { confirm: approvalId } : { cancel: approvalId }
  return request('/turn', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

/**
 * The action log: what this backend just did, newest first.
 *
 * Counts and events only. The log has never held the student's words — not the
 * page they read, not what they typed, not their deadline's text — and the test
 * for it reads the endpoint back to check rather than trusting the write site.
 *
 * Returns { entries, count, max_lines, note, path }: each entry is
 * { ts, event, ...counts }, and `note` says when older lines exist on disk but
 * were not read, which is the normal case for a log that grows forever.
 */
export function fetchLog(limit = 50, signal) {
  return request(`/log?limit=${encodeURIComponent(limit)}`, { signal })
}

/**
 * Everything the assistant remembers, grouped for the Memory tab.
 * Returns the backend's JSON: { topics, facts, total, errors, directory }
 */
export function fetchMemory(signal) {
  return request('/memory', { signal })
}

/**
 * Forget one fact. The backend replies with the whole refreshed state, so there
 * is never a second round trip to find out what is left.
 *
 * Deleting is idempotent: forgetting something that is already gone is a 200
 * with `removed: false`, not an error. The most likely cause is a double click
 * or an edit made by hand in the file itself, and neither deserves a red box.
 */
export function deleteMemoryFact(topic, factId, signal) {
  const query = new URLSearchParams({ topic, fact: factId })
  return request(`/memory?${query.toString()}`, { method: 'DELETE', signal })
}

/**
 * What the student has studied, and how it went.
 *
 * Returns { active, sessions, total_sessions, weakest, totals, declared_weak,
 * errors, directory }: the open quiz if there is one, recent quizzes with their
 * questions, per-topic accuracy, and the weak topics they told us in their own
 * words. Two different kinds of evidence, deliberately not merged.
 */
export function fetchProgress(signal) {
  return request('/progress', { signal })
}

/**
 * What is on the student's Google Calendar (Step 12), read live.
 *
 * Kept off `/brief` on purpose: the brief is composed in code from files on this
 * machine and cannot fail, while this one talks to Google and can. A slow or
 * unreachable calendar delays its own section instead of the deadlines beside it.
 *
 * Returns { connected, configured, read_only, scope, scope_summary, calendar,
 * horizon_days, events, lines, counts, cost_usd, source, error?, explanation? }.
 *
 * `connected: false` has two very different causes and both booleans are present
 * so the panel never has to guess which one it is looking at: `configured: false`
 * means the server has no OAuth client at all (a setup problem, not a button),
 * while `configured: true` means the student just has not connected yet.
 */
export function fetchCalendar(signal) {
  return request('/calendar', { signal })
}

/**
 * Delete one quiz from the record. The backend answers with the whole refreshed
 * state, exactly like a memory delete, so nothing has to be guessed at.
 *
 * Idempotent: a quiz that is already gone returns 200 with `removed: false`.
 */
export function deleteQuizSession(sessionId, signal) {
  const query = new URLSearchParams({ session: sessionId })
  return request(`/progress?${query.toString()}`, { method: 'DELETE', signal })
}

/** Wipe the whole study record. There is no undo, so the UI asks first. */
export function clearProgress(signal) {
  return request('/progress?all=1', { method: 'DELETE', signal })
}
