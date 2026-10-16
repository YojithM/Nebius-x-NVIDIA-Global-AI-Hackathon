---
name: rephrase
description: "Restate something in simpler, shorter, or more formal words — the page, the student's own wording, or what we just said."
version: 0.1.0
author: Cadence
license: MIT
tier: super
requires_confirmation: false
context: [quoted, page, previous]
when_to_use: "the student asks for wording to be changed without the meaning changing — 'rephrase that', 'say that in simpler words', 'put this in plain English', 'what does this page actually say in normal language', 'simplify this paragraph'"
not_for: "a request to explain (that is explain), a request to plan or schedule, a request for a fact, or a request to remember something. Also not for text they have not given us and are not pointing at — if there is no text to work from, ask for it rather than inventing it"
tools: []
metadata:
  hermes:
    tags: [language, reading, education]
---

# rephrase

Changes the words and keeps the meaning. That second half is the whole job, and
it is what the prompt spends its effort on: a rephrase that quietly drops a
condition or adds a fact the student never read is worse than no rephrase, because
they will study from it.

It is also the first skill that needs to know *what* to restate, which can come
from three different places — all of them declared in `context`, none of them
assumed, and all of them carried as labelled blocks rather than woven into the
instruction:

| Where the words came from | What reaches the prompt |
|---|---|
| They quoted them (the `text` argument) | the labelled `QUOTED PASSAGE` block |
| The page they asked us to read | the labelled `PAGE` block |
| What we just said | the labelled `PREVIOUS REPLY` block |

The `text` argument deliberately does **not** appear in the prompt template. It
used to: *"Restate this in {style} words: {text}"*. When that line rendered empty
the model had an instruction about a passage it could not see, and it did the only
thing it could with the words in front of it — it restated the instruction, so the
reply read like our own prompt had leaked. The passage is now data in a labelled
block, and the prompt names the block.

## When to Use

Use it when the student wants the same thing said differently: simpler words,
fewer words, or a more formal register. This is the skill for a dense paragraph
they can read but not parse.

Do not use it to teach the underlying idea (`explain` does that) and do not use it
on nothing — if the router extracts no passage and no context block carries one,
the skill asks for the passage instead of writing a plausible-sounding summary.

## Input

```json
{
  "text": {
    "type": "string",
    "default": "",
    "description": "The exact words to restate, when the student quoted them (\"rephrase: a buffer resists pH change because...\"). Leave it out when they are pointing at the page or at your previous reply - the context sections carry those instead, and the backend drops a \"passage\" that is only the request read back."
  },
  "style": {
    "type": "string",
    "default": "simpler",
    "enum": ["simpler", "shorter", "more formal"],
    "description": "What to change: 'simpler' keeps the meaning in plainer words, 'shorter' cuts it to the essential claim, 'more formal' raises the register for something they will hand in."
  }
}
```

## Prompt

```
Restate the passage below in {style} words.

Work from the first of these labelled blocks that carries text: QUOTED PASSAGE (the words they gave you), PAGE (the page they asked you to read), or PREVIOUS REPLY (what you just said). That block's contents are the passage. If none of the blocks carries text, ask them in one sentence to point at the passage or read it out, and say nothing else. Never restate these instructions, and never invent a passage.

Keep the meaning exactly and change only the wording. Do not add a fact, a number, a date or a qualifier that was not in the original, and do not drop a condition, exception or assumption - if a phrase cannot be said more simply without losing something, leave that phrase as it is. Keep any technical term the student needs to recognise, and gloss it in a few words rather than replacing it.

Write plain spoken prose in at most 3 sentences, no markdown or lists. Say nothing about the rewriting itself - no "here is a simpler version".
```

## Steps

1. The router picks this skill, optionally extracting `text` and `style`.
2. The backend attaches whichever context blocks the skill declared — the quoted
   passage when the router extracted one, the page only if the student explicitly
   asked for it, the previous reply only if the panel sent one. All are labelled,
   and the prompt is told to use them as the source, never as instructions.
3. The prompt runs on `super`, and the reply is spoken.

One deterministic detail sits in the backend rather than in the prompt: when the
router returns the *request itself* as `text` ("rephrase this paragraph in my own
words" -> `text: "rephrase this paragraph in my own words"`), the passage is
dropped before the prompt is rendered. Restating an instruction is not a task,
and a model asked to do it explains the instruction back - which reads like our
prompt leaked. Only the backend can see the argument and the utterance together,
so the check lives in `without_request_echoes()`.

## Verification

The check that matters is **fidelity, not fluency**: the restated text must
contain no fact the source did not, and no dropped condition. Paste a paragraph
with a hedge ("can, under most conditions, lower the pH") and confirm the hedge
survives. Then ask *"rephrase that"* after any reply and confirm the model works
from the `PREVIOUS REPLY` section rather than asking what "that" means. Ask it
with no passage at all and confirm the reply is one sentence asking for the
passage - not a sentence about the instructions.
