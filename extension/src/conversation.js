// What the panel remembers of the conversation, and hands to the backend.
//
// Two layers now. The `history` sent with a turn is the last few messages; older
// ones are compacted into a running note by the backend (`sessionMemory.js`
// decides when, and `/summarize` writes it), so a study session keeps its thread
// past the message window without growing the prompt forever.
//
// Without this, every utterance arrived alone: a reply could offer "want me to
// show you how?", and the "yes" that followed was a brand-new request with no
// referent, so the model either guessed or asked what they meant. The backend
// keeps no session of its own - it is a stateless turn endpoint - so the panel
// is the only place a conversation exists, and it already has one on screen.
//
// Two rules keep this from becoming a second, invisible transcript:
//
// 1. It is derived from `turns`, the exact array that is rendered. Whatever was
//    cleared from the screen is absent from the request, so "Clear" really does
//    clear the conversation rather than merely hiding it.
// 2. It is bounded, twice: the last few messages, each shortened. A long reply
//    read aloud is not worth its full text in the next prompt, and the server
//    enforces its own caps regardless of what this sends.

/** How many recent messages ride along. Three exchanges is enough for "that" and "yes". */
export const HISTORY_MESSAGES = 6

/** How much of one message is kept. Longer replies are trimmed rather than dropped. */
export const HISTORY_MESSAGE_CHARS = 500

/**
 * One panel turn as one backend message, or null if it is not one.
 *
 * The single place roles are translated (`you` -> `user`, `cadence` ->
 * `assistant`) and the single place text is flattened: one line per message,
 * because a reply's own line breaks would otherwise arrive as separate messages
 * in the prompt.
 */
export function toMessage(role, text, chars = HISTORY_MESSAGE_CHARS) {
  const mapped =
    role === 'you' || role === 'user'
      ? 'user'
      : role === 'cadence' || role === 'assistant'
        ? 'assistant'
        : ''
  if (!mapped || typeof text !== 'string') return null
  const width = Math.max(1, Number(chars) || 0)
  const clean = text.replace(/\s+/g, ' ').trim().slice(0, width).trim()
  return clean ? { role: mapped, text: clean } : null
}

/**
 * The messages just before the current request: the last few, oldest first.
 *
 * Interim speech is never a turn, and the current utterance is not included - it
 * travels as `transcript`, and sending it twice would make the model answer
 * itself.
 */
export function recentMessages(messages, recent = HISTORY_MESSAGES) {
  if (!Array.isArray(messages)) return []
  const limit = Math.max(0, Number(recent) || 0)
  return limit > 0 ? messages.slice(-limit) : []
}
