---
name: explain
description: "Explain a concept — or the page the student asked us to read — in plain words."
version: 0.1.0
author: Cadence
license: MIT
tier: super
requires_confirmation: false
context: [page]
when_to_use: "the student asks for something to be explained, unpacked, walked through, or made clearer — 'explain entropy', 'I don't get how the Krebs cycle works', 'walk me through this page', 'why does this happen'"
not_for: "a bare factual question that wants one short answer ('what is a ketone', 'when is the midterm'), a request to plan or schedule work, a request to quiz them or to remember something, a request to restate text they already understand (that is rephrase), or a request for what a page says rather than what one idea in it means ('analyze this page', 'summarise this', 'give me the gist') — that is page_summary, which covers the whole page instead of going deep on one part"
tools: []
metadata:
  hermes:
    tags: [teaching, explanation, education]
---

# explain

Explains one thing, at the level the student is actually at, in words they can
hear once and keep.

The interesting part of this skill is `context: [page]`. When — and only when —
the student asks us to read the page they are looking at, the backend attaches
that page's text to this prompt, fenced and labelled as untrusted data. So
*"explain this page"* works, and *"explain entropy"* works from nothing at all:
the same skill, one optional input, and no code that changes between the two.

## When to Use

Use it when the student wants **understanding**: a concept, a mechanism, a step
in a derivation, or the page in front of them.

Do not use it for a question that wants one short fact ("what is a ketone") — that
is the `general` path and it is faster. Do not use it to plan, quiz, or rephrase:
each of those is a separate skill with a different output shape.

## Input

```json
{
  "subject": {
    "type": "string",
    "required": true,
    "description": "What to explain, in the student's own words: a concept ('entropy'), a step ('why you flip the inequality here'), or the page they asked us to read ('the page')"
  },
  "level": {
    "type": "string",
    "default": "some background",
    "enum": ["new to it", "some background", "confident"],
    "description": "How much they already know. 'new to it' starts from the smallest idea that makes the rest make sense; 'confident' skips the basics and goes to the mechanism or the catch."
  }
}
```

## Prompt

```
Explain this to the student: {subject}

Assume this level: {level}. If that is "new to it", start from the smallest idea that makes everything else make sense. If it is "confident", skip the basics and go straight to the mechanism, the catch, or the part that trips people up.

If a PAGE section is below, the student asked you to read the page they are on: explain what it actually says, and quote at most a few words of it. Never explain a page from its title alone.

At most 3 sentences of plain spoken prose. No markdown, no bullets, no headings - this is read aloud. Use one concrete example rather than an abstraction. Do not do their graded work: explain the idea and stop short of the answer they are being graded on. End with the one thing they should be able to say back in their own words.
```

## Steps

1. The router picks this skill and extracts `subject` (required) and optionally
   `level`. Both are validated against the schema above before anything runs.
2. If the student asked us to read the page (`explain this page`), the backend
   attaches its text here — see `context_blocks()` in `backend/server.py`. If they
   asked and we could not read it, the prompt says so instead, and the model is
   told to say that rather than invent the page.
3. The prompt is rendered and sent to the declared tier (`super`).

## Verification

The reply must be **3 sentences or fewer, with no markdown or bullet
characters**, and it must explain rather than answer a graded question. Ask
*"explain how a buffer resists pH change"* for the no-page path and *"explain
this page"* on a reading for the page path — the meta line under the reply shows
how much page text was actually sent, and the log records the character count and
nothing else.
