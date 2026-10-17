// When the panel compacts the conversation, tested without a model.
//
// The rules here are about cost and correctness rather than storage: summarise in
// batches, never the recent window, and never mark messages as covered that a
// failed call did not actually cover. A bug in the last one would silently delete
// the student's context, which is the failure this whole feature exists to fix.
//
// Run with `npm test` (node --test test/).

import assert from 'node:assert/strict'
import test from 'node:test'

import { HISTORY_MESSAGES } from '../src/conversation.js'
import {
  MAX_CONVERSATION_MESSAGES,
  SUMMARY_BATCH,
  SUMMARY_MAX_MESSAGES,
  summarizePlan,
  trimConversation,
} from '../src/sessionMemory.js'

const conversation = (length) =>
  Array.from({ length }, (_, index) => ({ role: 'user', text: `message ${index}` }))

test('nothing is summarised while everything still fits in a request', () => {
  assert.equal(summarizePlan(conversation(HISTORY_MESSAGES)), null)
  assert.equal(summarizePlan(conversation(HISTORY_MESSAGES + 1)), null)
})

test('a batch is the messages that fell out of the window', () => {
  const plan = summarizePlan(conversation(HISTORY_MESSAGES + SUMMARY_BATCH), 0)
  assert.equal(plan.messages.length, SUMMARY_BATCH)
  assert.equal(plan.messages[0].text, 'message 0')
  assert.equal(plan.through, SUMMARY_BATCH)
})

test('messages the note already covers are not summarised twice', () => {
  const plan = summarizePlan(conversation(HISTORY_MESSAGES + SUMMARY_BATCH * 2), SUMMARY_BATCH)
  assert.equal(plan.messages.length, SUMMARY_BATCH)
  assert.equal(plan.messages[0].text, `message ${SUMMARY_BATCH}`)
  assert.equal(plan.through, SUMMARY_BATCH * 2)
})

test('a long backlog is sent in bounded batches, oldest first', () => {
  const plan = summarizePlan(conversation(100), 0)
  assert.equal(plan.messages.length, SUMMARY_MAX_MESSAGES)
  assert.equal(plan.messages[0].text, 'message 0')
  assert.equal(plan.through, SUMMARY_MAX_MESSAGES)

  const next = summarizePlan(conversation(100), plan.through)
  assert.equal(next.messages[0].text, `message ${SUMMARY_MAX_MESSAGES}`)
})

test('a note that is already ahead of the window asks for nothing', () => {
  const plan = summarizePlan(conversation(HISTORY_MESSAGES + SUMMARY_BATCH), 99)
  assert.equal(plan, null)
})

test('nonsense input asks for nothing rather than throwing', () => {
  assert.equal(summarizePlan(null), null)
  assert.equal(summarizePlan('messages'), null)
  // An unreadable position counts as "nothing summarised yet", and a negative
  // one cannot travel backwards either.
  assert.equal(summarizePlan(conversation(20), 'x').messages.length, SUMMARY_MAX_MESSAGES)
  assert.equal(summarizePlan(conversation(20), -5).through, SUMMARY_MAX_MESSAGES)
  assert.equal(summarizePlan(conversation(20), 0, { batch: 1, limit: 1 }).messages.length, 1)
  assert.equal(summarizePlan(conversation(20), 0, { batch: 1, limit: 0 }).messages.length, 1)
  // A limit below the batch can never produce a plan, which is the honest
  // outcome: there is nothing worth spending a call on.
  assert.equal(summarizePlan(conversation(20), 0, { limit: 0 }), null)
})

test('a very long conversation drops its oldest messages, and the note keeps up', () => {
  const messages = conversation(MAX_CONVERSATION_MESSAGES + 5)
  const trimmed = trimConversation(messages, 10)
  assert.equal(trimmed.messages.length, MAX_CONVERSATION_MESSAGES)
  // The note covered messages 0..9, five of which were just dropped, so it now
  // covers the first five of what is left.
  assert.equal(trimmed.summarizedThrough, 5)
  assert.equal(trimmed.messages[0].text, 'message 5')
})

test('trimming a conversation that fits changes nothing', () => {
  const messages = conversation(10)
  assert.deepEqual(trimConversation(messages, 4), { messages, summarizedThrough: 4 })
  assert.deepEqual(trimConversation(null, 4), { messages: [], summarizedThrough: 4 })
  assert.deepEqual(trimConversation(conversation(10), -3), {
    messages: conversation(10),
    summarizedThrough: 0,
  })
})
