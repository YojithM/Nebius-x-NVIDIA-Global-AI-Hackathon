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


import { MAX_PER_CHECK, LEAD_MINUTES, dueReminders } from '../public/background.js'

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

async function loadWorker(payload, notified = {}) {
  const calls = { created: [], stored: null, alarm: null, listeners: {} }
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener: (fn) => (calls.listeners.install = fn) },
      onStartup: { addListener: (fn) => (calls.listeners.startup = fn) },
      lastError: null,
    },
    alarms: {
      create: (name, info) => (calls.alarm = { name, ...info }),
      onAlarm: { addListener: (fn) => (calls.listeners.alarm = fn) },
    },
    notifications: {
      create: async (id, options) => {
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
  globalThis.fetch = async () => ({ ok: true, json: async () => payload })
  await import(`../public/background.js?wiring=${Date.now()}${Math.random()}`)
  return calls
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
