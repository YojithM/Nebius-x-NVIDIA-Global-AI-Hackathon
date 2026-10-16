import { useCallback, useEffect, useState } from 'react'
import { fetchLog } from './api.js'

/**
 * The Log tab (Step 10): what the backend just did, newest first.
 *
 * This tab exists for the same reason the Memory and Progress tabs do — it is
 * the answer to "what is this thing keeping?" — with one difference that is
 * worth stating plainly: **there is nothing in here about the student.** Every
 * line is a count or an event: how many facts are remembered, how many
 * characters a page was, which tool ran and whether it worked, what a turn cost.
 * Not the deadline text, not the page's words, not what was typed. That rule was
 * set when the log was created in Step 8; this tab is what makes it visible
 * enough to check.
 *
 * Reading it is also how a stranger can review a deploy without being handed
 * anyone's study life, which is the other half of the same choice.
 */

// The events worth explaining. An unrecognised event is shown as it is rather
// than hidden: a log that drops what it does not understand is worse than one
// that shows something new.
const EVENT_NOTES = {
  turn: 'one request answered',
  morning_brief: 'the day’s brief, logged on its own at the configured hour',
  brief_failed: 'the scheduled brief could not be composed',
  tool_call: 'the plan used a tool',
  agent_wrap_up: 'the plan reached its step ceiling and wrapped up',
  empty_reply: 'the model came back blank and was retried once',
  unauthorized: 'a request without a valid token was refused',
  session_capped: 'a demo visitor reached the question cap',
  approval_asked: 'permission was asked before a Tier-2 action',
  approval_declined: 'you said no to a requested action',
  approval_unknown: 'a go-ahead arrived that had expired or was already used',
  action_blocked: 'a write was refused because nothing asked for it',
  memory_delete: 'a remembered fact was forgotten',
  progress_delete: 'a quiz was deleted from the record',
  page_text_refused: 'page text arrived without a request to read the page',
  turn_failed: 'a request could not be answered',
  quiz_escape: 'a quiz was ended mid-way',
  argument_echo_dropped: 'an argument that just repeated the request was dropped',
  reply_truncated: 'a reply ran out of budget and was asked again, briefly',
  reply_long: 'a reply came back long — complete, so it was left alone',
  spoken_reply_unusable: 'both attempts ran out of budget, so the student was told',
}

// Fields that are interesting in order, with what to call them. Anything not
// named here is still shown, at the end — see `summarise`.
const FIELD_LABELS = [
  ['skill', 'skill'],
  ['skill_source', 'chosen by'],
  ['confirmed', 'confirmed'],
  ['executor_tier', 'ran on'],
  ['cost_usd', 'cost'],
  ['router_cost_usd', 'router cost'],
  ['router_attempts', 'router tries'],
  ['latency_ms', 'ms'],
  ['steps', 'steps'],
  ['tool_names', 'tools'],
  ['tool_failures', 'failed'],
  ['citations', 'cited'],
  ['memory_saved_topic', 'saved to'],
  ['dated', 'dated'],
  ['overdue', 'overdue'],
  ['due_soon', 'due soon'],
  ['undated', 'undated'],
  ['transcript_chars', 'chars said'],
  ['page_text_chars', 'page chars'],
  ['memory_facts', 'facts remembered'],
  ['used', 'used'],
  ['cap', 'cap'],
  ['ok', 'ok'],
  ['reason', 'why'],
  ['detail', 'why'],
  ['recovered', 'recovered'],
]

// Fields that are noise on screen: the timestamp is already the first column,
// the model ids are long and belong to /health, and a false is clearer as an
// absence than as the word "no".
const QUIET = new Set([
  'ts',
  'event',
  'page_title',
  'page_requested',
  'page_invoked',
  'page_text_used',
  'page_truncated',
  'steps_truncated',
  'previous_reply_chars',
  'memory_prompt_chars',
  'agent_truncated',
  'router_tier',
  'router_model',
  'executor_model',
  'needs_confirmation',
])

function formatValue(value) {
  if (typeof value === 'number') {
    if (value === 0) return ''
    // Costs are small enough that 0 would be a lie: $0.000507 is real money.
    return Math.abs(value) < 0.01 ? `$${value.toFixed(6)}` : String(value)
  }
  if (Array.isArray(value)) return value.length ? value.join('+') : ''
  if (value === true) return 'yes'
  if (value === false || value === null || value === undefined || value === '') return ''
  return String(value)
}

/** One line's counts, in a readable order, without the noise. */
function summarise(entry) {
  const seen = new Set()
  const parts = []
  for (const [key, label] of FIELD_LABELS) {
    if (seen.has(key) || key === 'ts' || key === 'event') continue
    if (!(key in entry)) continue
    seen.add(key)
    const text = formatValue(entry[key])
    if (text) parts.push(`${label} ${text}`)
  }
  for (const [key, value] of Object.entries(entry)) {
    if (seen.has(key) || QUIET.has(key)) continue
    const text = formatValue(value)
    if (text) parts.push(`${key.replace(/_/g, ' ')} ${text}`)
  }
  return parts.join(' · ')
}

export default function LogTab() {
  const [state, setState] = useState({ status: 'loading' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      setState({ status: 'ready', data: await fetchLog(50) })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (state.status === 'loading') {
    return (
      <main className="log">
        <p className="empty">Reading the action log…</p>
      </main>
    )
  }

  if (state.status === 'error') {
    return (
      <main className="log">
        <p className="empty">{state.message}</p>
        <button type="button" className="retry" onClick={load}>
          Try again
        </button>
      </main>
    )
  }

  const { entries = [], count = 0, note = '', path = '', max_lines: maxLines = 0 } = state.data

  return (
    <main className="log">
      <header className="log-head">
        <h2>Action log</h2>
        <button type="button" className="retry" disabled={busy} onClick={load}>
          {busy ? 'Reading…' : 'Refresh'}
        </button>
      </header>

      <p className="hint">
        Counts and events only — never the text of a page, a deadline, or anything
        you said. Newest first, up to {maxLines} lines.
      </p>

      {note && <p className="hint">{note}</p>}

      {count === 0 && (
        <p className="empty">
          Nothing logged yet. Ask a question, or wait for the morning brief, and it
          shows up here.
        </p>
      )}

      {count > 0 && (
        <ol className="log-lines">
          {entries.map((entry, index) => (
            <li key={`${entry.ts}-${index}`}>
              <span className="log-ts">{String(entry.ts || '').replace('T', ' ').slice(11) || '—'}</span>
              <span className="log-event">{entry.event}</span>
              <span className="log-detail">{summarise(entry)}</span>
              {EVENT_NOTES[entry.event] && (
                <span className="log-note">{EVENT_NOTES[entry.event]}</span>
              )}
            </li>
          ))}
        </ol>
      )}

      {error && <p className="error">{error}</p>}

      {path && (
        <footer className="memory-foot">
          Appended to <code>{path}</code> — plain JSON lines, one per event, and
          yours to read or delete.
        </footer>
      )}
    </main>
  )
}
