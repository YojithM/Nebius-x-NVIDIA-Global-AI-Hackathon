// Session memory: when the older conversation should be compacted into a note.
//
// The panel keeps the whole conversation for the length of a sitting, because the
// screen cannot: the transcript is capped at eight turns, and the messages that
// fall off the end are exactly the ones a later question may refer to. So they
// are sent to the backend in small batches and replaced by a note.
//
// Three decisions live here, and they are all about cost rather than storage:
//
// 1. **A batch, not every message.** Summarising two messages at a time would
//    spend a model call every turn. Waiting until four have fallen out amortises
//    it over two turns, and the note is a few words either way.
// 2. **Never the recent window.** The note covers only what the turn no longer
//    sends; the last few messages travel verbatim, because that is where "yes"
//    and "that one" are answered from.
// 3. **A cap on the batch.** One long study session's backlog cannot turn into a
//    single enormous prompt; leftover messages simply wait for the next batch.
//
// Nothing here is stored anywhere but the panel's memory, and it is sent back to
// the server with each turn - the backend keeps no session of its own.

import { HISTORY_MESSAGES } from './conversation.js'

/** How many messages must fall out of the recent window before a note is written. */
export const SUMMARY_BATCH = 4

/** How many messages one summarise call may carry. */
export const SUMMARY_MAX_MESSAGES = 12

/** How much conversation the panel holds before it starts forgetting the oldest. */
export const MAX_CONVERSATION_MESSAGES = 200

/**
 * What to send for summarising, or null when there is nothing worth a call.
 *
 * `summarizedThrough` is how many messages the current note already covers, so
 * this returns only messages that would otherwise be forgotten - and `through` is
 * where that count should move to once the note comes back. A failure therefore
 * changes nothing and is retried later, rather than losing the messages.
 */
export function summarizePlan(
  messages,
  summarizedThrough = 0,
  { recent = HISTORY_MESSAGES, batch = SUMMARY_BATCH, limit = SUMMARY_MAX_MESSAGES } = {},
) {
  if (!Array.isArray(messages)) return null
  const newest = Math.max(0, Number(recent) || 0)
  const need = Math.max(1, Number(batch) || 0)
  const most = Math.max(1, Number(limit) || 0)

  // Everything older than the window that the note does not already cover.
  const cutoff = Math.max(0, messages.length - newest)
  const start = Math.min(Math.max(0, Number(summarizedThrough) || 0), cutoff)
  const pending = messages.slice(start, cutoff).slice(0, most)
  if (pending.length < need) return null
  return { messages: pending, through: start + pending.length }
}

/**
 * Drop the oldest messages once the conversation gets very long.
 *
 * Returns the note's position adjusted for what was dropped, because the count is
 * an index into this array - trimming without shifting it would make the next
 * plan re-summarise messages that are gone, or skip ones that are not.
 */
export function trimConversation(
  messages,
  summarizedThrough = 0,
  limit = MAX_CONVERSATION_MESSAGES,
) {
  const most = Math.max(1, Number(limit) || 0)
  if (!Array.isArray(messages) || messages.length <= most) {
    return { messages: messages || [], summarizedThrough: Math.max(0, summarizedThrough || 0) }
  }
  const dropped = messages.length - most
  return {
    messages: messages.slice(dropped),
    summarizedThrough: Math.max(0, (summarizedThrough || 0) - dropped),
  }
}
