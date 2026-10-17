// The service worker does three things: wire the toolbar button to the side
// panel, remind you about a calendar event before it starts, and answer the
// panel when it asks for a reminder by hand.
//
// Everything else — microphone, speech recognition, text-to-speech, the turn
// loop — deliberately lives in the side panel. MV3 service workers are
// terminated after ~30s idle, so any audio or conversation state kept here
// would be silently destroyed mid-sentence.
//
// The reminders are the exception that proves the rule: a reminder that only
// fires while the panel happens to be open is not a reminder. So they run on an
// alarm here, from a `fetch` to the same backend the panel uses, and they hold
// no state beyond the ids already shown (kept in `chrome.storage.local`, because
// this worker is restarted constantly and an in-memory set would re-notify the
// same event every few minutes).
//
// Note: no API key is present anywhere in this extension. Model calls happen
// on the backend; the browser only ever talks to that backend.

const BACKEND_URL = 'http://127.0.0.1:8787'
// Where the worker reports what it put on screen. A reminder fires on an alarm,
// with no panel open, so this report is the only way any other part of the
// project can learn that it happened at all - see `reportReminders`.
const REPORT_URL = `${BACKEND_URL}/reminders`

const CHECK_ALARM = 'calendar-check'
const CHECK_MINUTES = 5
// Ten minutes: long enough to put a coat on, short enough that the notification
// is about the thing you are walking into rather than the thing after it.
export const LEAD_MINUTES = 10
// A machine that was asleep for an hour comes back with several events inside the
// window at once. Three notifications is help; eight is the reason people turn
// notifications off.
export const MAX_PER_CHECK = 3

// What the panel sends to ask for a test reminder. Duplicated in
// `extension/src/reminders.js` on purpose: that file is bundled by Vite and this
// one is copied verbatim, so neither can import the other - and a test fails if
// the two strings drift apart.
export const TEST_REMINDER_MESSAGE = 'cadence:test-reminder'

const ICON = 'icons/icon128.png'
const NOTIFIED_KEY = 'calendarNotified'
// Long enough to cover a recurring event's next occurrence, short enough that the
// stored map cannot grow without bound.
const KEEP_MS = 2 * 24 * 60 * 60 * 1000

/**
 * Which events deserve a notification right now.
 *
 * Pure on purpose: `now` and `already` are arguments, so the rule can be tested
 * without Chrome, a calendar, or a clock. `payload` is the backend's `/calendar`
 * body, whose events carry a local `day` (YYYY-MM-DD) and `time` (HH:MM), plus
 * `title`, `calendar` and `account`.
 *
 * Skipped: all-day events (there is no moment to remind you at), anything that
 * already started, anything further out than the lead time, and anything already
 * shown. The key is the event's *occurrence*, not its id, so tomorrow's copy of a
 * weekly event is a different reminder.
 */
export function dueReminders(payload, now = Date.now(), already = {}) {
  const events = Array.isArray(payload && payload.events) ? payload.events : []
  const lead = LEAD_MINUTES * 60 * 1000
  const due = []

  for (const event of events) {
    if (!event || event.all_day || !event.day || !event.time) continue
    const start = new Date(`${event.day}T${event.time}:00`).getTime()
    if (!Number.isFinite(start)) continue
    if (start <= now || start - now > lead) continue

    const key = `${event.id || event.title}|${event.day}|${event.time}`
    if (already[key]) continue

    const when = event.end_time ? `${event.time}–${event.end_time}` : event.time
    // A primary calendar is named after its own address, so the two labels are
    // usually the same string and saying it twice reads like a bug.
    const where = [...new Set([event.calendar, event.account].filter(Boolean))].join(' · ')
    due.push({
      key,
      start,
      // The event's own start time, kept so the backend can be told *when* the
      // reminder was about without being told *what* it was about. The title
      // stays here: the action log has never held the student's words, and a
      // calendar title is one of them.
      at: event.time,
      title: String(event.title || 'Calendar event').slice(0, 120),
      message: where ? `${when} · ${where}` : when,
    })
  }

  // Soonest first, so a cap drops the event the student can still afford to miss.
  due.sort((a, b) => a.start - b.start)
  return due.slice(0, MAX_PER_CHECK)
}

/**
 * Ask Chrome for one notification. -> {ok, reason}
 *
 * `ok` means Chrome *accepted* the call, which is not the same fact as a banner
 * on the screen: macOS silences an app without telling it, so a resolved call and
 * an empty screen is a combination that has been observed here. A rejection still
 * tells us something worth having - the permission or the icon is wrong, and that
 * half is ours.
 *
 * Extracted so the manual test in the panel goes through the same call a real
 * reminder does. A test that drew its own banner would stay green while the
 * alarm path was broken, which is the only path that cannot be watched.
 */
async function raise(id, title, message) {
  try {
    await chrome.notifications.create(id, {
      type: 'basic',
      iconUrl: ICON,
      title,
      message,
      priority: 1,
    })
    return { ok: true, reason: '' }
  } catch (error) {
    console.warn('[cadence] calendar notification:', error)
    return { ok: false, reason: String((error && error.message) || error) }
  }
}

/**
 * Tell the backend what was raised. -> true when the line was written.
 *
 * Never throws. The notification belongs to Chrome by the time this runs, and a backend
 * that is asleep, moved, or token-protected must not turn a delivered reminder
 * into a failed one. The answer is still returned, because the manual test in
 * the panel does want to know whether the backend heard about it.
 */
async function reportReminders(report) {
  try {
    const response = await fetch(REPORT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(report),
    })
    return response.ok
  } catch (error) {
    console.warn('[cadence] reminder report:', error)
    return false
  }
}

/** One poll: read the calendar, notify what is due, remember what was shown. */
async function checkCalendar() {
  const response = await fetch(`${BACKEND_URL}/calendar`, { cache: 'no-store' })
  if (!response.ok) return
  const payload = await response.json()

  const stored = await chrome.storage.local.get(NOTIFIED_KEY)
  const already = (stored && stored[NOTIFIED_KEY]) || {}
  const due = dueReminders(payload, Date.now(), already)
  if (!due.length) return

  const stamp = Date.now()
  const kept = {}
  for (const [key, at] of Object.entries(already)) {
    if (Number(at) && stamp - Number(at) < KEEP_MS) kept[key] = at
  }

  let shown = 0
  let failed = 0
  for (const reminder of due) {
    // Kept whether or not the banner drew. A notification is never worth failing
    // the poll over, and a permission the student revoked must not become a
    // retry loop every five minutes.
    kept[reminder.key] = stamp
    const drawn = await raise(
      `cadence-calendar:${reminder.key}`,
      reminder.title,
      reminder.message,
    )
    if (drawn.ok) shown += 1
    else failed += 1
  }

  await chrome.storage.local.set({ [NOTIFIED_KEY]: kept })
  // `due` is soonest-first, so the first entry is the soonest event this batch
  // was about. Only the time travels: see the `at` field in `dueReminders`.
  await reportReminders({ shown, failed, soonest: due[0].at || null, manual: false })
}

/**
 * A test reminder, asked for by the panel. -> {ok, reason, logged}
 *
 * `ok` is Chrome's answer, not the screen's - see `raise`.
 *
 * The id carries the clock so a second press is a second banner: reusing an id
 * makes `chrome.notifications.create` silently *replace* an existing
 * notification, which reads as a button that stopped working.
 */
export async function sendTestReminder() {
  const drawn = await raise(
    `cadence-test:${Date.now()}`,
    'Cadence',
    'A test reminder - this is what a calendar reminder looks like.',
  )
  const logged = await reportReminders({
    shown: drawn.ok ? 1 : 0,
    failed: drawn.ok ? 0 : 1,
    manual: true,
  })
  return { ok: drawn.ok, reason: drawn.reason, logged }
}

export function startCalendarChecks() {
  chrome.alarms.create(CHECK_ALARM, {
    delayInMinutes: 1,
    periodInMinutes: CHECK_MINUTES,
  })
}

function openPanel() {
  chrome.windows?.getLastFocused({ windowTypes: ['normal'] }, (win) => {
    if (chrome.runtime.lastError || !win) return
    // Requires a user gesture, which a notification click is. If a future Chrome
    // disagrees, the click still clears the notification and nothing breaks.
    Promise.resolve(chrome.sidePanel?.open({ windowId: win.id })).catch(() => {})
  })
}

if (typeof chrome !== 'undefined' && chrome.runtime?.onInstalled) {
  chrome.runtime.onInstalled.addListener(() => {
    // Clicking the toolbar icon opens the side panel instead of a popup.
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch((error) => console.warn('[cadence] side panel behavior:', error))
    startCalendarChecks()
  })

  chrome.runtime.onStartup?.addListener(() => {
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch(() => {})
    // Alarms survive a browser restart in principle, but creating it again is
    // idempotent and covers the case where it was never created (a reload of an
    // unpacked extension re-runs onInstalled, a normal start does not).
    startCalendarChecks()
  })

  chrome.alarms?.onAlarm.addListener((alarm) => {
    if (alarm.name !== CHECK_ALARM) return
    checkCalendar().catch((error) => console.warn('[cadence] calendar check:', error))
  })

  chrome.notifications?.onClicked.addListener((id) => {
    if (!String(id).startsWith('cadence-calendar:')) return
    chrome.notifications.clear(id)
    openPanel()
  })

  chrome.runtime.onMessage?.addListener((message, sender, respond) => {
    if (!message || message.type !== TEST_REMINDER_MESSAGE) return false
    // `return true` holds the channel open for the asynchronous answer - MV3 has
    // no promise-returning listener, so it is the only way to say whether the
    // banner went up. The panel is the only sender that can reach this: nothing
    // outside the extension is allowed to message it.
    sendTestReminder()
      .then(respond)
      .catch((error) =>
        respond({ ok: false, reason: String((error && error.message) || error) }),
      )
    return true
  })
}
