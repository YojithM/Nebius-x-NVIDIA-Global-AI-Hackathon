// Asking the service worker for a test reminder.
//
// The pop-up is the one piece of this project the panel cannot produce on its
// own. Notifications belong to the worker - which is also where the real
// reminders come from - so a test that drew its own banner would prove nothing
// about the path that matters: an alarm, a five-minute poll, and a worker waking
// up with no panel on screen. The panel asks; the worker answers with what
// actually happened.
//
// The message name is deliberately duplicated from `public/background.js`. That
// file is copied into the build verbatim while this one is bundled by Vite, so
// neither can import the other, and a test in `test/calendar-notify.test.mjs`
// fails if the two strings ever drift apart.

export const TEST_REMINDER_MESSAGE = 'cadence:test-reminder'

/**
 * Is there a worker to ask?
 *
 * False in the `/demo` page and in any plain browser: the panel is served there
 * as an ordinary page, `chrome` is undefined, and a button that cannot work
 * should not be on screen at all.
 */
export function testReminderSupported() {
  return typeof chrome !== 'undefined' && !!chrome.runtime?.sendMessage
}

/**
 * Chrome's wording for "nothing is listening", turned into what to do about it.
 *
 * `Could not establish connection. Receiving end does not exist.` is what a
 * service worker still running the *previous* build says. That mismatch is easy
 * to produce and hard to see: the panel is read from disk every time it opens, so
 * a newly added button appears immediately next to a worker that Chrome has not
 * restarted, and the extension card's version number is the only other hint. It
 * says nothing about any of that, so we do - this is a real error that was hit
 * before this sentence existed.
 */
function explain(message) {
  const text = String(message || '')
  if (/Receiving end does not exist|message port closed/i.test(text)) {
    return (
      'the reminder worker is not answering — reload the extension at ' +
      'chrome://extensions and try again (the panel is read fresh on every open, ' +
      'the worker is not)'
    )
  }
  return text || 'the reminder worker did not answer'
}

/**
 * Ask the worker to raise a test reminder. -> Promise<{ok, logged, reason}>
 *
 * Rejects only when the message could not be delivered at all. A reminder that
 * was drawn but not written to the log resolves as `logged: false` instead,
 * because "your pop-up did not appear" and "your backend did not hear about it"
 * are two different problems and the tab says which one happened.
 */
export function sendTestReminder() {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage({ type: TEST_REMINDER_MESSAGE }, (response) => {
        const failure = chrome.runtime.lastError
        if (failure) {
          reject(new Error(explain(failure.message)))
          return
        }
        resolve(response || { ok: false, reason: 'the reminder worker gave no answer' })
      })
    } catch (error) {
      reject(error)
    }
  })
}
