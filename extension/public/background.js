// The service worker does three things: wire the toolbar button to the side
// panel, remind you about a calendar event ten minutes before it starts, and
// answer the panel when it asks for a reminder by hand.
//
// Everything else — microphone, speech recognition, text-to-speech, the turn
// loop — deliberately lives in the side panel. MV3 service workers are
// terminated after ~30s idle, so any audio or conversation state kept here
// would be silently destroyed mid-sentence.
//
// The reminders are the exception that proves the rule: a reminder that only
// fires while the panel happens to be open is not a reminder. So they run on
// alarms here, from a `fetch` to the same backend the panel uses.
//
// There are two clocks, because "ten minutes before" and "every five minutes"
// are not the same promise. A single five-minute poll can only ever say *some
// point* inside the ten minutes, which is what the first version of this did and
// what the READMEs admitted in a footnote:
//
//   * `calendar-check` polls every five minutes, and its job is discovery: learn
//     about new, moved and deleted events, catch up on anything already inside
//     its window, and keep the alarms below in step with the calendar.
//   * one one-shot alarm per upcoming event, set for exactly its start minus ten
//     minutes. That is what turns the timing into a promise rather than a range.
//
// The only state kept is the set of occurrences already shown (in
// `chrome.storage.local`, because this worker is restarted constantly and an
// in-memory set would re-notify the same event every few minutes).
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
// One alarm per event, named after the occurrence it belongs to. A name rather
// than an index, so arming the same event twice replaces its alarm instead of
// adding a second one that fires the same notification.
export const REMINDER_ALARM = 'cadence-reminder:'
// Ten minutes: long enough to put a coat on, short enough that the notification
// is about the thing you are walking into rather than the thing after it.
export const LEAD_MINUTES = 10
// A machine that was asleep for an hour comes back with several events inside the
// window at once. Three notifications is help; eight is the reason people turn
// notifications off.
export const MAX_PER_CHECK = 3
// How many one-shot alarms to keep armed at once. A week of a busy calendar is
// tens of events and Chrome's own ceiling is 500 alarms per extension, so this is
// a guard against a bug - a duplicate key, a runaway payload - rather than a
// limit anyone should meet. What it would drop, the five-minute poll still
// catches.
export const ARM_LIMIT = 64

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
 * The occurrence an event belongs to: its id, its day, its time.
 *
 * The unit of "already reminded", and so the unit of an alarm's name too. Not the
 * event id alone: tomorrow's copy of a weekly event is a different occurrence and
 * deserves its own reminder.
 */
export function occurrenceKey(event) {
  const item = event || {}
  return `${item.id || item.title}|${item.day}|${item.time}`
}

/**
 * The reminder one event turns into, or null when it is not one.
 *
 * Skipped: all-day events (there is no moment to remind you at), events with no
 * readable start, and events that have already started - a reminder for an event
 * that is beginning is not a reminder.
 *
 * `at` carries the event's own start time so the backend can be told *when* a
 * reminder was about without being told *what* it was about. The title stays
 * here: the action log has never held the student's words, and a calendar title
 * is one of them.
 */
function reminderOf(event, now) {
  if (!event || event.all_day || !event.day || !event.time) return null
  const start = new Date(`${event.day}T${event.time}:00`).getTime()
  if (!Number.isFinite(start)) return null
  if (start <= now) return null

  const when = event.end_time ? `${event.time}–${event.end_time}` : event.time
  // A primary calendar is named after its own address, so the two labels are
  // usually the same string and saying it twice reads like a bug.
  const where = [...new Set([event.calendar, event.account].filter(Boolean))].join(' · ')
  return {
    key: occurrenceKey(event),
    start,
    at: event.time,
    title: String(event.title || 'Calendar event').slice(0, 120),
    message: where ? `${when} · ${where}` : when,
  }
}

/**
 * Which events deserve a notification right now - the catch-up rule.
 *
 * Pure, like everything below it: `now` and `already` are arguments, so the rule
 * can be tested without Chrome, a calendar, or a clock. `payload` is the
 * backend's `/calendar` body, whose events carry a local `day` (YYYY-MM-DD) and
 * `time` (HH:MM), plus `title`, `calendar` and `account`.
 *
 * This is what the five-minute poll asks, and the answer is "anything already
 * inside its ten-minute window": an event created after its own ten-minute mark
 * passed has no earlier moment to be reminded at. Everything further out is armed
 * instead - see `armPlan`.
 */
export function dueReminders(payload, now = Date.now(), already = {}) {
  const events = Array.isArray(payload && payload.events) ? payload.events : []
  const lead = LEAD_MINUTES * 60 * 1000
  const due = []

  for (const event of events) {
    const reminder = reminderOf(event, now)
    if (!reminder) continue
    if (reminder.start - now > lead) continue
    if (already[reminder.key]) continue
    due.push(reminder)
  }

  // Soonest first, so a cap drops the event the student can still afford to miss.
  due.sort((a, b) => a.start - b.start)
  return due.slice(0, MAX_PER_CHECK)
}

/**
 * The reminder a *named* occurrence deserves right now, or null.
 *
 * This is the fire-time rule, and it is deliberately not `dueReminders`: there is
 * no lead-time check here, because the alarm was already set for exactly ten
 * minutes before. Re-checking the lead would be a way to lose a reminder to a
 * clock that is a second fast, and the two rules are tested against that
 * difference.
 *
 * The calendar is re-read for the same reason the rule re-reads it: an event that
 * was moved or deleted since it was armed should not be announced as if it were
 * still there.
 */
export function reminderFor(payload, key, now = Date.now(), already = {}) {
  const events = Array.isArray(payload && payload.events) ? payload.events : []
  for (const event of events) {
    if (occurrenceKey(event) !== key) continue
    const reminder = reminderOf(event, now)
    if (!reminder || already[reminder.key]) return null
    return reminder
  }
  return null
}

/**
 * The one-shot alarms worth having armed right now. -> [{name, when, key}]
 *
 * One per occurrence whose ten-minute mark is still ahead of us, soonest first.
 * Anything already inside its window is missing from this list on purpose: it
 * belongs to the poll, which notifies it immediately.
 *
 * An alarm, not a timer: Chrome tears this worker down after half a minute idle,
 * so a `setTimeout` for three hours from now would die long before it fired. Also
 * the reason the plan is re-derived from the calendar every five minutes rather
 * than trusted - alarms do not survive an extension reload, and a calendar is
 * something people edit.
 */
export function armPlan(payload, now = Date.now(), already = {}) {
  const events = Array.isArray(payload && payload.events) ? payload.events : []
  const lead = LEAD_MINUTES * 60 * 1000
  const plan = []

  for (const event of events) {
    const reminder = reminderOf(event, now)
    if (!reminder) continue
    if (already[reminder.key]) continue
    const when = reminder.start - lead
    if (when <= now) continue
    plan.push({ name: `${REMINDER_ALARM}${reminder.key}`, when, key: reminder.key })
  }

  plan.sort((a, b) => a.when - b.when)
  return plan.slice(0, ARM_LIMIT)
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

/** The calendar as the backend sees it, or null when it cannot be reached. */
async function readCalendar() {
  try {
    const response = await fetch(`${BACKEND_URL}/calendar`, { cache: 'no-store' })
    if (!response.ok) return null
    return await response.json()
  } catch (error) {
    console.warn('[cadence] calendar read:', error)
    return null
  }
}

/**
 * Put reminders on screen and remember them. -> {shown, failed}
 *
 * One path for both clocks, so the dedupe store and its pruning rule exist in
 * exactly one place. The catch-up poll and a one-shot alarm can each decide that
 * the same occurrence is due, and whichever arrives second must find it already
 * recorded - which is the whole reason the store is on disk rather than in
 * memory.
 */
async function deliver(reminders) {
  if (!reminders.length) return { shown: 0, failed: 0 }

  const stamp = Date.now()
  const stored = await chrome.storage.local.get(NOTIFIED_KEY)
  const already = (stored && stored[NOTIFIED_KEY]) || {}
  const kept = {}
  for (const [key, at] of Object.entries(already)) {
    if (Number(at) && stamp - Number(at) < KEEP_MS) kept[key] = at
  }

  let shown = 0
  let failed = 0
  for (const reminder of reminders) {
    // Recorded whether or not the banner drew. A notification is never worth
    // failing over, and a permission the student revoked must not become a retry
    // loop every five minutes.
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
  // `reminders` is soonest-first, so the first entry is the soonest event this
  // batch was about. Only the time travels: see the `at` field in `reminderOf`.
  await reportReminders({ shown, failed, soonest: reminders[0].at || null, manual: false })
  return { shown, failed }
}

/**
 * Make the armed alarms match the plan. -> {set, cleared}
 *
 * Re-arming an unchanged event every five minutes would be harmless but noisy, so
 * an alarm whose time has not moved is left alone. One whose event moved has a
 * different name (the name carries the time), so it is a clear plus a set. And
 * one whose event is gone is cleared - without that, a calendar edited twice a
 * day would leave a trail of alarms behind it.
 */
async function syncAlarms(plan) {
  const wanted = new Map(plan.map((entry) => [entry.name, entry]))
  const existing = (await chrome.alarms?.getAll?.()) || []
  let cleared = 0

  for (const alarm of existing) {
    const name = String(alarm.name)
    if (!name.startsWith(REMINDER_ALARM)) continue
    if (wanted.has(name)) continue
    // An alarm whose time has already passed is left alone: either it has fired
    // and gone, or Chrome is about to deliver it, and clearing it in that gap
    // would eat the reminder it was set for.
    if (Number(alarm.scheduledTime) <= Date.now()) continue
    await chrome.alarms.clear(name)
    cleared += 1
  }

  const armed = new Map(existing.map((alarm) => [alarm.name, alarm]))
  let set = 0
  for (const [name, entry] of wanted) {
    const current = armed.get(name)
    // Inside a second counts as the same time: Chrome rounds, and rewriting the
    // alarm would restart it for no reason.
    if (current && Math.abs(Number(current.scheduledTime) - entry.when) < 1000) continue
    await chrome.alarms.create(name, { when: entry.when })
    set += 1
  }

  return { set, cleared }
}

/**
 * The five-minute poll: read the calendar, catch up on what is due, arm the rest.
 *
 * Discovery and catch-up only. The reminders themselves belong to the one-shot
 * alarms, which is how the ten minutes became a time rather than a window.
 */
async function checkCalendar() {
  const payload = await readCalendar()
  if (!payload) return

  const stored = await chrome.storage.local.get(NOTIFIED_KEY)
  const already = (stored && stored[NOTIFIED_KEY]) || {}

  // Catch up first, then arm: an event whose ten-minute mark has already passed
  // is notified by the first half and skipped by the second, which is exactly the
  // split that keeps a late-added event from being missed.
  await deliver(dueReminders(payload, Date.now(), already))
  await syncAlarms(armPlan(payload, Date.now(), already))
}

/**
 * One event's alarm, at its ten-minute mark: check it is still true, then notify.
 *
 * The alarm carries nothing but the occurrence's name, so this re-reads the
 * calendar to confirm the event still exists at that time. If the backend cannot
 * be reached in that exact minute the reminder is missed rather than guessed at,
 * and the poll still catches the event inside its window - which is the second
 * reason that poll exists.
 */
async function fireReminder(name) {
  const key = String(name).slice(REMINDER_ALARM.length)
  const payload = await readCalendar()
  if (!payload) return

  const stored = await chrome.storage.local.get(NOTIFIED_KEY)
  const already = (stored && stored[NOTIFIED_KEY]) || {}
  const reminder = reminderFor(payload, key, Date.now(), already)
  if (!reminder) return

  await deliver([reminder])
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

/**
 * Start the discovery poll. The per-event alarms follow from its first run.
 *
 * Deliberately the only alarm anyone creates up front: Chrome drops every alarm
 * when an extension is reloaded or updated, so a plan armed now would be a plan
 * missing after the next rebuild. One minute after this starts, the poll reads
 * the calendar and arms whatever the week actually holds.
 */
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
    const name = String(alarm.name)
    if (name === CHECK_ALARM) {
      checkCalendar().catch((error) => console.warn('[cadence] calendar check:', error))
      return
    }
    if (name.startsWith(REMINDER_ALARM)) {
      fireReminder(name).catch((error) =>
        console.warn('[cadence] calendar reminder:', error),
      )
    }
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
