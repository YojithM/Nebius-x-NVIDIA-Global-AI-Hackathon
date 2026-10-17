// The two voices, and the fallback between them.
//
// Listening is Chrome's own recogniser and always has been. Speaking has two
// paths since Step 13: the backend's hosted voice (ElevenLabs, keyed and billed
// server-side), and Chrome's local one, which is free, private, and sounds like
// it. The hosted voice is preferred when the backend says it has one; every
// failure - no key, out of credits, a rate limit, an unplayable blob, a browser
// that refuses autoplay - ends in the local voice rather than in silence. Silence
// would make a cost problem look like a broken app.
//
// IMPORTANT: all of these are resolved at CALL time, not at module load. That
// keeps the panel testable outside a real microphone (a test can install a
// stand-in before clicking) and means the UI shows a useful message on browsers
// that lack these APIs instead of crashing on import.

import { speakReply } from './api.js'

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

// Anything playing through the hosted voice, and a counter that makes a reply
// arriving late into a reply nobody is waiting for any more. Without the counter,
// clicking the mic twice starts two fetches and the first one wins the speaker.
let currentAudio = null
let generation = 0

function stopHosted() {
  if (!currentAudio) return
  const audio = currentAudio
  currentAudio = null
  try {
    audio.pause()
  } catch {
    /* already ended or never started */
  }
  if (audio.src) URL.revokeObjectURL(audio.src)
}

/**
 * Speak a reply with the backend's voice, and fall back to Chrome's on any
 * failure. Fire-and-forget: the caller has already set its "speaking" state, and
 * `onEnd` is the one thing that clears it.
 *
 * `onNotice('unavailable', err)` says the better voice was tried and did not
 * work. That is worth telling the student once - not because they can fix a
 * rate limit, but because "why does it sound different now" deserves an answer.
 * `onNotice('truncated')` means a long reply was spoken in part, which the panel
 * says out loud rather than quietly showing text it never read.
 */
export function speakHosted(text, { onEnd, onNotice } = {}) {
  const mine = ++generation
  stopHosted()
  if (textToSpeechSupported()) window.speechSynthesis.cancel()

  const finish = () => {
    if (mine === generation && onEnd) onEnd()
  }

  const fallback = () => {
    if (mine !== generation) return
    // The local voice may itself be unavailable; then the reply is simply not
    // spoken, and the panel returns to idle rather than waiting forever.
    if (!speak(text, { onEnd: finish })) finish()
  }

  speakReply(String(text ?? ''))
    .then(async ({ blob, truncated }) => {
      if (mine !== generation) return
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      currentAudio = audio
      let released = false
      const release = () => {
        if (released) return
        released = true
        if (currentAudio === audio) currentAudio = null
        URL.revokeObjectURL(url)
      }
      audio.onended = () => {
        release()
        finish()
      }
      audio.onerror = () => {
        release()
        fallback()
      }
      try {
        await audio.play()
      } catch {
        release()
        fallback()
      }
      if (truncated && onNotice) onNotice('truncated')
    })
    .catch((err) => {
      if (mine !== generation) return
      if (onNotice) onNotice('unavailable', err)
      fallback()
    })
}

/**
 * Stop whatever is being said, from either voice.
 *
 * The generation bump is the half that is easy to forget: a synthesis already in
 * flight would otherwise arrive after the student has moved on and start talking
 * over a question they are asking.
 */
export function stopSpeaking() {
  generation += 1
  stopHosted()
  if (textToSpeechSupported()) window.speechSynthesis.cancel()
}
