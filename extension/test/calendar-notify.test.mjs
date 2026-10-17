// Calendar reminders, tested without Chrome, a calendar, or a clock.
//
// `dueReminders` is pure and exported from the service worker for exactly this
// reason: the part worth testing is "which events deserve a pop-up right now",
// and that question does not need a browser to answer. The wiring around it
// (alarm, notification, click) is checked by the manifest assertions at the
// bottom — a permission removed from manifest.json would leave this logic
// running against an undefined `chrome.notifications`.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'


import {
  ARM_LIMIT,
  MAX_PER_CHECK,
  LEAD_MINUTES,
  REMINDER_ALARM,
  TEST_REMINDER_MESSAGE,
  armPlan,
  dueReminders,
  reminderFor,
} from '../public/background.js'
// The panel's half of the same message. Plain ESM, so node imports it directly,
// which is the only way to test the error path that a real browser produced.
import { sendTestReminder, testReminderSupported } from '../src/reminders.js'

// Local time on purpose: `day`/`time` from the backend are local, and so is
// `new Date('2026-10-17T10:00:00')`. Both sides of the comparison move together
// in any timezone, so the suite does not depend on where it runs.
const NOW = new Date('2026-10-17T10:00:00').getTime()

function event(overrides = {}) {
  return {
    id: 'e1',
    title: 'Thermodynamics lecture',
    day: '2026-10-17',
    time: '10:10',
    end_time: '11:00',
    all_day: false,
    calendar: 'Work',
    account: 'me@school.edu',
    ...overrides,
  }
}

const payload = (events) => ({ connected: true, events })

test('an event starting inside the lead time is due', () => {
  const due = dueReminders(payload([event()]), NOW)
  assert.equal(due.length, 1)
  assert.equal(due[0].key, 'e1|2026-10-17|10:10')
  assert.equal(due[0].title, 'Thermodynamics lecture')
  assert.equal(due[0].message, '10:10–11:00 · Work · me@school.edu')
})

test('the lead time is inclusive and one minute past it is not', () => {
  const atLead = dueReminders(payload([event({ time: '10:10' })]), NOW)
  assert.equal(atLead.length, 1, 'exactly 10 minutes out still counts')

  const pastLead = dueReminders(payload([event({ time: '10:11' })]), NOW)
  assert.equal(pastLead.length, 0)
  assert.equal(LEAD_MINUTES, 10)
})

test('an event that already started is not reminded', () => {
  assert.equal(dueReminders(payload([event({ time: '09:59' })]), NOW).length, 0)
  // The exact minute it starts: too late to be a reminder.
  assert.equal(dueReminders(payload([event({ time: '10:00' })]), NOW).length, 0)
})

test('all-day events are skipped: there is no moment to remind you at', () => {
  const allDay = event({ all_day: true, time: null })
  assert.equal(dueReminders(payload([allDay]), NOW).length, 0)
})

test('an event already shown is not shown again', () => {
  const already = { 'e1|2026-10-17|10:10': NOW - 60_000 }
  assert.equal(dueReminders(payload([event()]), NOW, already).length, 0)
  // Both halves have to agree, or the poll notifies and the alarm does it again a
  // few minutes later: one occurrence, one reminder, whichever path gets there.
  assert.deepEqual(armPlan(payload([event()]), NOW, already), [])
  assert.equal(reminderFor(payload([event()]), 'e1|2026-10-17|10:10', NOW, already), null)
})

// ------------------------------------------------------------ the two clocks --
//
// "Ten minutes before" and "every five minutes" are different promises, and this
// is the difference: a five-minute poll can only ever land *somewhere* inside the
// ten minutes. So every event gets its own one-shot alarm for exactly its mark,
// and the poll's job is discovery and catching up.

/** When the worker should arm the alarm for an event at `day`/`time`. */
function tenBefore({ day, time }) {
  return new Date(`${day}T${time}:00`).getTime() - LEAD_MINUTES * 60 * 1000
}

const armedReminders = (calls) =>
  calls.alarms.filter((alarm) => String(alarm.name).startsWith(REMINDER_ALARM))

test('the fire rule re-checks the event but not the lead, so a hair early still fires', () => {
  // The fixture starts at 10:10 and NOW is 10:00, so its ten-minute mark is
  // exactly NOW. A clock an instant fast is the case this rule exists for: the
  // poll would say "not yet" and the reminder would be lost for good.
  const early = NOW - 1
  assert.equal(dueReminders(payload([event()]), early).length, 0)
  assert.equal(
    reminderFor(payload([event()]), 'e1|2026-10-17|10:10', early).title,
    'Thermodynamics lecture',
  )
})

test('the plan is the soonest ten-minute marks, and no more than it can hold', () => {
  const many = []
  for (let index = 0; index < 80; index += 1) {
    const start = new Date(Date.now() + (index + 1) * 6 * 60 * 60 * 1000)
    many.push(event({ id: `e${index}`, ...localParts(start), end_time: null }))
  }
  const plan = armPlan(payload(many), Date.now())
  const expected = many
    .map((item) => tenBefore(item))
    .sort((a, b) => a - b)
    .slice(0, ARM_LIMIT)
  assert.equal(plan.length, ARM_LIMIT)
  assert.deepEqual(
    plan.map((entry) => entry.when),
    expected,
    'a cap that keeps the wrong events is worse than no cap',
  )
})

test('a weekly event\'s next occurrence is a new reminder', () => {
  const already = { 'e1|2026-10-10|10:10': NOW - 7 * 24 * 60 * 60 * 1000 }
  assert.equal(dueReminders(payload([event()]), NOW, already).length, 1)
})

test('the soonest events win when several are due', () => {
  const many = ['10:01', '10:02', '10:03', '10:04', '10:05'].map((time, index) =>
    event({ id: `e${index}`, time, end_time: null }),
  )
  const due = dueReminders(payload(many), NOW)
  assert.equal(due.length, MAX_PER_CHECK)
  assert.deepEqual(
    due.map((reminder) => reminder.start),
    [...due.map((reminder) => reminder.start)].sort((a, b) => a - b),
  )
  assert.equal(due[0].key, 'e0|2026-10-17|10:01')
})

test('an account-labelled primary calendar is named once', () => {
  const due = dueReminders(
    payload([event({ calendar: 'me@school.edu', account: 'me@school.edu' })]),
    NOW,
  )
  assert.equal(due[0].message, '10:10–11:00 · me@school.edu')
})

test('a missing end time reads as a single time, not an empty range', () => {
  const due = dueReminders(payload([event({ end_time: null, calendar: '', account: '' })]), NOW)
  assert.equal(due[0].message, '10:10')
})

test('unreadable times and half-empty payloads are skipped, not thrown', () => {
  assert.deepEqual(dueReminders(payload([event({ time: 'noon' })]), NOW), [])
  assert.deepEqual(dueReminders(payload([event({ day: null })]), NOW), [])
  assert.deepEqual(dueReminders(payload([null, undefined]), NOW), [])
  assert.deepEqual(dueReminders({}, NOW), [])
  assert.deepEqual(dueReminders(null, NOW), [])
  // Not connected: the backend sends this often, and it must stay quiet.
  assert.deepEqual(dueReminders({ connected: false }, NOW), [])
})

test('a run of due events carries a default title', () => {
  const due = dueReminders(payload([event({ title: '' })]), NOW)
  assert.equal(due[0].title, 'Calendar event')
})

// -------------------------------------------------------------------- wiring --
//
// `dueReminders` decides *what*; these check that the service worker actually
// runs that decision on an alarm, raises the notification and remembers it. The
// module is imported again with a cache-busting query after a fake `chrome` is
// installed, because the listener registration is guarded by `typeof chrome`.

function localParts(date) {
  const pad = (number) => String(number).padStart(2, '0')
  return {
    day: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  }
}

async function loadWorker(
  payload,
  notified = {},
  { notificationFails = false, armed = [] } = {},
) {
  const calls = {
    created: [],
    stored: null,
    alarm: null,
    alarms: [],
    cleared: [],
    listeners: {},
    requests: [],
  }
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener: (fn) => (calls.listeners.install = fn) },
      onStartup: { addListener: (fn) => (calls.listeners.startup = fn) },
      onMessage: { addListener: (fn) => (calls.listeners.message = fn) },
      lastError: null,
    },
    alarms: {
      create: (name, info) => {
        calls.alarm = { name, ...info }
        calls.alarms.push({ name, ...info })
        return Promise.resolve()
      },
      // What Chrome already has armed. The worker reads this to leave an alarm
      // alone when nothing about its event changed, and to clear the ones whose
      // events are gone. `armed` is what a test wants it to find waiting.
      getAll: async () => armed,
      clear: async (name) => {
        calls.cleared.push(name)
        return true
      },
      onAlarm: { addListener: (fn) => (calls.listeners.alarm = fn) },
    },
    notifications: {
      create: async (id, options) => {
        // Chrome rejects the call when the student has revoked the permission,
        // which is the failure the counter in the report has to survive.
        if (notificationFails) throw new Error('notifications are blocked')
        calls.created.push({ id, ...options })
        return id
      },
      onClicked: { addListener: (fn) => (calls.listeners.click = fn) },
      clear: () => {},
    },
    storage: {
      local: {
        get: async () => ({ calendarNotified: notified }),
        set: async (value) => (calls.stored = value),
      },
    },
    sidePanel: { setPanelBehavior: async () => {}, open: async () => {} },
    windows: { getLastFocused: () => {} },
  }
  // Recorded rather than ignored: the report to `/reminders` is the whole point
  // of the log line, so the tests read the request the worker actually made.
  globalThis.fetch = async (url, options = {}) => {
    calls.requests.push({ url: String(url), options })
    return { ok: true, json: async () => payload }
  }
  await import(`../public/background.js?wiring=${Date.now()}${Math.random()}`)
  return calls
}

/** The body of the report the worker sent to the backend, parsed. */
function reportOf(calls) {
  const request = calls.requests.find((entry) => entry.url.endsWith('/reminders'))
  assert.ok(request, 'nothing was reported to the backend about the reminder')
  assert.equal(request.options.method, 'POST')
  return JSON.parse(request.options.body)
}

function unloadWorker() {
  delete globalThis.chrome
  delete globalThis.fetch
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 25))

test('an alarm poll raises a notification and records the event', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]))
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    assert.equal(calls.created.length, 1)
    assert.equal(calls.created[0].type, 'basic')
    assert.equal(calls.created[0].title, 'Thermodynamics lecture')
    assert.match(calls.created[0].message, /\d\d:\d\d · Work · me@school\.edu/)
    assert.ok(
      calls.stored?.calendarNotified?.[`e1|${soon.day}|${soon.time}`],
      'the event was not recorded, so the next poll would notify again',
    )
  } finally {
    unloadWorker()
  }
})

test('a poll whose event was already shown stays silent', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon })]), {
    [`e1|${soon.day}|${soon.time}`]: Date.now(),
  })
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    assert.equal(calls.created.length, 0)
    assert.equal(calls.stored, null)
  } finally {
    unloadWorker()
  }
})

test('every other alarm is ignored', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon })]))
  try {
    calls.listeners.alarm({ name: 'something-else' })
    await settle()
    assert.equal(calls.created.length, 0)
  } finally {
    unloadWorker()
  }
})

test('an event hours away is armed for exactly ten minutes before it starts', async () => {
  const soon = localParts(new Date(Date.now() + 3 * 60 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]))
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    assert.equal(calls.created.length, 0, 'nothing is due three hours out')
    const armed = armedReminders(calls)
    assert.equal(armed.length, 1)
    assert.equal(armed[0].name, `${REMINDER_ALARM}e1|${soon.day}|${soon.time}`)
    assert.equal(
      armed[0].when,
      tenBefore(soon),
      'the alarm belongs at the ten-minute mark, not at the next poll',
    )
    assert.equal(
      armed[0].periodInMinutes,
      undefined,
      'one shot for one event: a repeating alarm here would be the old design again',
    )
  } finally {
    unloadWorker()
  }
})

test('an event already inside its window is notified instead of armed', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]))
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    assert.equal(calls.created.length, 1, 'its ten-minute mark has gone by, so it is due now')
    assert.equal(armedReminders(calls).length, 0, 'there is no earlier moment to arm for')
  } finally {
    unloadWorker()
  }
})

test('the armed alarm fires the reminder itself, once, with the event title', async () => {
  const soon = localParts(new Date(Date.now() + 10 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]))
  try {
    calls.listeners.alarm({ name: `${REMINDER_ALARM}e1|${soon.day}|${soon.time}` })
    await settle()
    assert.equal(calls.created.length, 1)
    assert.equal(calls.created[0].title, 'Thermodynamics lecture')
    assert.match(calls.created[0].message, /\d\d:\d\d · Work · me@school\.edu/)
    assert.ok(
      calls.stored?.calendarNotified?.[`e1|${soon.day}|${soon.time}`],
      'unrecorded, the next poll would notify the same event again',
    )
    assert.equal(reportOf(calls).shown, 1)
  } finally {
    unloadWorker()
  }
})

test('an alarm whose event moved or already started says nothing', async () => {
  const started = localParts(new Date(Date.now() - 60 * 1000))
  const calls = await loadWorker(payload([event({ ...started })]))
  try {
    calls.listeners.alarm({ name: `${REMINDER_ALARM}e1|1999-01-01|09:00` })
    await settle()
    assert.equal(calls.created.length, 0, 'that occurrence is not on the calendar any more')
    assert.equal(calls.stored, null, 'and nothing was recorded for it')

    calls.listeners.alarm({ name: `${REMINDER_ALARM}e1|${started.day}|${started.time}` })
    await settle()
    assert.equal(calls.created.length, 0, 'too late to be a reminder')
  } finally {
    unloadWorker()
  }
})

test('a stale alarm is cleared and an unchanged one is left alone', async () => {
  const soon = localParts(new Date(Date.now() + 2 * 60 * 60 * 1000))
  const key = `e1|${soon.day}|${soon.time}`
  const armed = [
    { name: `${REMINDER_ALARM}${key}`, scheduledTime: tenBefore(soon) },
    { name: `${REMINDER_ALARM}gone|2099-01-01|09:00`, scheduledTime: Date.now() + 60 * 60 * 1000 },
    { name: 'calendar-check', scheduledTime: Date.now() + 60 * 1000 },
  ]
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]), {}, { armed })
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    assert.deepEqual(
      calls.cleared,
      [`${REMINDER_ALARM}gone|2099-01-01|09:00`],
      'a deleted event should not leave its alarm behind, and the poll is not ours to clear',
    )
    assert.equal(armedReminders(calls).length, 0, 'the alarm that was already right was rewritten')
  } finally {
    unloadWorker()
  }
})

test('a fired reminder is reported so the backend can log it', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(payload([event({ ...soon, end_time: null })]))
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    const report = reportOf(calls)
    assert.equal(report.shown, 1)
    assert.equal(report.failed, 0)
    assert.equal(report.soonest, soon.time, 'the log needs the event time, not its title')
    assert.equal(report.manual, false)
    // The one rule the action log has: this was the student's own calendar, and
    // its title stays in the browser.
    assert.equal('title' in report, false)
  } finally {
    unloadWorker()
  }
})

test('a refused notification is reported as failed and is not retried', async () => {
  const soon = localParts(new Date(Date.now() + 5 * 60 * 1000))
  const calls = await loadWorker(
    payload([event({ ...soon })]),
    {},
    { notificationFails: true },
  )
  try {
    calls.listeners.alarm({ name: 'calendar-check' })
    await settle()
    const report = reportOf(calls)
    assert.equal(report.shown, 0)
    assert.equal(report.failed, 1)
    assert.ok(
      calls.stored?.calendarNotified?.[`e1|${soon.day}|${soon.time}`],
      'a refused notification would otherwise be retried every five minutes',
    )
  } finally {
    unloadWorker()
  }
})

test('the panel can ask for a test reminder and is told what happened', async () => {
  const calls = await loadWorker(payload([]))
  try {
    const answer = await new Promise((resolve) => {
      const held = calls.listeners.message({ type: TEST_REMINDER_MESSAGE }, {}, resolve)
      assert.equal(held, true, 'the channel has to stay open for the async answer')
    })
    assert.deepEqual(answer, { ok: true, reason: '', logged: true })
    assert.equal(calls.created.length, 1)
    assert.match(calls.created[0].id, /^cadence-test:/, 'a second press needs a new id')
    assert.equal(calls.created[0].title, 'Cadence')
    const report = reportOf(calls)
    assert.equal(report.manual, true)
    assert.equal(report.shown, 1)
  } finally {
    unloadWorker()
  }
})

test('a test reminder Chrome refuses says so instead of claiming success', async () => {
  const calls = await loadWorker(payload([]), {}, { notificationFails: true })
  try {
    const answer = await new Promise((resolve) => {
      calls.listeners.message({ type: TEST_REMINDER_MESSAGE }, {}, resolve)
    })
    assert.equal(answer.ok, false)
    assert.match(answer.reason, /blocked/)
    assert.equal(reportOf(calls).failed, 1)
  } finally {
    unloadWorker()
  }
})

test('a message the worker does not know gets no answer at all', async () => {
  const calls = await loadWorker(payload([]))
  try {
    assert.equal(calls.listeners.message({ type: 'something-else' }, {}, () => {}), false)
    assert.equal(calls.listeners.message(undefined, {}, () => {}), false)
    await settle()
    assert.equal(calls.created.length, 0)
  } finally {
    unloadWorker()
  }
})

test('the panel and the worker agree on the message name', () => {
  // The name cannot be imported across that boundary - `public/` is copied
  // verbatim and `src/` is bundled - so it is asserted instead. A rename on one
  // side that misses the other would leave a button that silently does nothing.
  const panel = readFileSync(new URL('../src/reminders.js', import.meta.url), 'utf8')
  assert.ok(
    panel.includes(`'${TEST_REMINDER_MESSAGE}'`),
    'src/reminders.js and public/background.js disagree about the message name',
  )
})

// ------------------------------------------------- the panel's error path --
//
// Caught in the browser, not here: a service worker still running the previous
// build answers nothing, and Chrome's wording for that says nothing about why.

test('a worker that never answers is explained rather than repeated', async () => {
  globalThis.chrome = {
    runtime: {
      lastError: null,
      sendMessage: (message, respond) => {
        // What Chrome does when no listener exists: lastError is set and the
        // callback fires with nothing in it.
        globalThis.chrome.runtime.lastError = {
          message: 'Could not establish connection. Receiving end does not exist.',
        }
        respond(undefined)
      },
    },
  }
  try {
    await assert.rejects(
      sendTestReminder(),
      /reload the extension at chrome:\/\/extensions/,
    )
  } finally {
    delete globalThis.chrome
  }
})

test('a delivered reminder resolves with what the worker said', async () => {
  globalThis.chrome = {
    runtime: { lastError: null, sendMessage: (message, respond) => respond({ ok: true, logged: true }) },
  }
  try {
    assert.deepEqual(await sendTestReminder(), { ok: true, logged: true })
  } finally {
    delete globalThis.chrome
  }
})

test('the button is hidden where there is no worker to ask', () => {
  delete globalThis.chrome
  assert.equal(testReminderSupported(), false)
  globalThis.chrome = { runtime: { sendMessage: () => {} } }
  try {
    assert.equal(testReminderSupported(), true)
  } finally {
    delete globalThis.chrome
  }
})

test('installing registers the five-minute check and the panel behaviour', async () => {
  const calls = await loadWorker(payload([]))
  try {
    calls.listeners.install()
    assert.equal(calls.alarm.name, 'calendar-check')
    assert.equal(calls.alarm.periodInMinutes, 5)
    assert.ok(calls.alarm.delayInMinutes >= 1, 'the first check should not be immediate')
  } finally {
    unloadWorker()
  }
})

test('the built manifest grants what the reminders need', () => {
  const manifest = JSON.parse(
    readFileSync(new URL('../public/manifest.json', import.meta.url), 'utf8'),
  )
  const permissions = new Set(manifest.permissions || [])
  for (const permission of ['alarms', 'notifications', 'storage']) {
    assert.ok(permissions.has(permission), `manifest.json is missing "${permission}"`)
  }
  assert.equal(manifest.background?.service_worker, 'background.js')
})
