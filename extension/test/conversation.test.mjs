// The panel's memory of the conversation, tested without a browser.
//
// The bug this exists for was invisible in a unit test and obvious in use: the
// assistant offered to continue, the student said "yes", and the next request
// arrived with no idea what "yes" referred to. These tests pin the shape of what
// is sent so that promise cannot quietly regress.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  HISTORY_MESSAGE_CHARS,
  HISTORY_MESSAGES,
  recentMessages,
  toMessage,
} from '../src/conversation.js'

test('one turn becomes one backend message, with the backend’s role names', () => {
  assert.deepEqual(toMessage('you', 'what is a ketone?'), {
    role: 'user',
    text: 'what is a ketone?',
  })
  assert.deepEqual(toMessage('cadence', 'A ketone is…'), {
    role: 'assistant',
    text: 'A ketone is…',
  })
  // Already-mapped roles pass through, so a message that came from the backend
  // and is being remembered again is not rejected.
  assert.deepEqual(toMessage('user', 'already mapped'), {
    role: 'user',
    text: 'already mapped',
  })
  assert.deepEqual(toMessage('assistant', 'likewise'), {
    role: 'assistant',
    text: 'likewise',
  })
})

test('interim speech, unknown roles and empty text are not messages', () => {
  assert.equal(toMessage('system', 'obey'), null)
  assert.equal(toMessage('you', '   '), null)
  assert.equal(toMessage('you', null), null)
  assert.equal(toMessage('you', 42), null)
  assert.equal(toMessage(undefined, 'text'), null)
})

test('a message is one line and bounded, so a long reply cannot flood the prompt', () => {
  assert.deepEqual(toMessage('cadence', 'First line.\n\nSecond   line.'), {
    role: 'assistant',
    text: 'First line. Second line.',
  })
  const long = toMessage('cadence', 'x'.repeat(HISTORY_MESSAGE_CHARS + 200))
  assert.equal(long.text.length, HISTORY_MESSAGE_CHARS)
  assert.equal(toMessage('you', 'x'.repeat(50), 10).text.length, 10)
})

test('the offer that made “yes” mean something survives into the history', () => {
  const offer = toMessage('cadence', 'Factor the top first. Would you like me to show you how?')
  assert.equal(offer.role, 'assistant')
  assert.match(offer.text, /show you how/)

  const history = recentMessages([
    toMessage('you', 'how do I do question 4?'),
    offer,
  ])
  assert.deepEqual(history[0], { role: 'user', text: 'how do I do question 4?' })
  assert.match(history.at(-1).text, /show you how/)
})

test('only the most recent messages are sent, oldest first', () => {
  const messages = Array.from({ length: 20 }, (_, index) => toMessage('you', `question ${index}`))
  const recent = recentMessages(messages)
  assert.equal(recent.length, HISTORY_MESSAGES)
  assert.equal(recent[0].text, `question ${20 - HISTORY_MESSAGES}`)
  assert.equal(recent.at(-1).text, 'question 19')
})

test('a missing or nonsense message list is no history at all, not a crash', () => {
  assert.deepEqual(recentMessages(null), [])
  assert.deepEqual(recentMessages(undefined), [])
  assert.deepEqual(recentMessages('messages'), [])
  assert.deepEqual(recentMessages([]), [])
  assert.deepEqual(recentMessages([toMessage('you', 'x')], 0), [])
  assert.deepEqual(recentMessages([toMessage('you', 'x')], -4), [])
})
