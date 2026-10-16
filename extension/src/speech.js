// Browser speech helpers.
//
// IMPORTANT: both of these are resolved at CALL time, not at module load. That
// keeps the panel testable outside a real microphone (a test can install a
// stand-in before clicking) and means the UI shows a useful message on browsers
// that lack these APIs instead of crashing on import.

export function getRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!Recognition) return null

  const recognition = new Recognition()
  recognition.lang = 'en-US'
  recognition.continuous = false // one utterance per press, so state stays simple
  recognition.interimResults = true // this is what makes the live transcript live
  recognition.maxAlternatives = 1
  return recognition
}

export function speechToTextSupported() {
  return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)
}

export function textToSpeechSupported() {
  return 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance === 'function'
}

// A citation marker: `[1]`, and the space in front of it, so removing one leaves
// "the exam moved." rather than "the exam moved .".
const CITATION = /\s?\[\d{1,2}\]/g

/**
 * The spoken form of a reply: the same words, without the citation markers.
 *
 * Citations are how the reply points at a page *on screen*, and out loud there is
 * nothing to point at. "[1]" is either read as "bracket one" or dropped by the
 * voice, and neither is what the student should hear in the middle of a sentence.
 * Falling back to the raw text keeps this from ever handing speech an empty
 * string - a reply that was nothing but a marker is still a reply.
 */
export function withoutCitations(text) {
  const raw = String(text || '')
  const stripped = raw.replace(CITATION, '').replace(/\s{2,}/g, ' ').trim()
  return stripped || raw.trim()
}

/**
 * Speak a reply aloud. Returns false when the browser can't do it, so the caller
 * can fall back to showing text rather than hanging in a "speaking" state.
 */
export function speak(text, { onStart, onEnd } = {}) {
  if (!textToSpeechSupported()) return false

  // Cancel anything mid-sentence: a new reply should interrupt the old one.
  window.speechSynthesis.cancel()

  const utterance = new window.SpeechSynthesisUtterance(text)
  utterance.rate = 1.02
  utterance.pitch = 1
  utterance.volume = 1
  if (onStart) utterance.onstart = onStart
  if (onEnd) utterance.onend = onEnd
  utterance.onerror = onEnd

  window.speechSynthesis.speak(utterance)
  return true
}

export function stopSpeaking() {
  if (textToSpeechSupported()) window.speechSynthesis.cancel()
}
