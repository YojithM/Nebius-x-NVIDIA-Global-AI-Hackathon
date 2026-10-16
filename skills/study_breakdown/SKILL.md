---
name: study_breakdown
description: "Break a topic or assignment into the first few moves worth making."
version: 0.1.0
author: Cadence
license: MIT
tier: super
requires_confirmation: false
when_to_use: "the student names a subject, chapter, assignment, or project and asks how to begin, how to start, or how to split it into pieces"
not_for: "a question that has a factual answer ('what is a ketone', 'define entropy', 'when did X happen'), a request to explain, summarise, rephrase, or describe something rather than plan it, a request to do the work rather than plan it, anything about deadlines, grades, or scores, or a schedule spread over days or fitted to the hours they have ('plan the next four evenings') - that is plan. Also not for a request to create or draft a NEW skill for our own library, even when that skill is about breaking work into blocks: that is the general path, the only one that can write a skill file, and breaking their work down now is not what they asked for"
tools: []
metadata:
  hermes:
    tags: [study, planning, education]
---

# study_breakdown

Turns something large and vague — "the organic chemistry midterm" — into the
first two or three concrete moves, so the student stops staring at it.

This is the reference skill, and it is deliberately small. It exists to prove
the whole path works end to end: the router picks it, its arguments are
validated against the schema below, and its prompt runs on the tier it declares.
Later skills (`plan_assignment`, `make_quiz`, `explain_page`) follow the same
shape, and the agent in Step 9 can add more without touching Python.

## When to Use

Use when the student is deciding *what to do first* about something they have
already named: a chapter to revise, a lab report to write, a midterm to prepare
for.

Do not use when the student wants the work done — this skill points at moves, it
does not produce the answer. Do not use for a question that has a factual answer
("what is a ketone") — that is the general path.

And do not use it for a *schedule*. "Where do I start" is this skill: a few moves,
no dates. "I have four evenings before Friday, what do I do when" is `plan`, which
allocates time against a deadline and runs on a bigger model. These two are close
enough that the router can pick either, so both files say plainly which is which.

## Input

```json
{
  "topic": {
    "type": "string",
    "required": true,
    "description": "What the student is facing, in their own words, e.g. 'organic chemistry midterm' or 'lab report 3'"
  },
  "subtasks": {
    "type": "integer",
    "default": 3,
    "description": "How many pieces to split it into. Kept at 3 by default because the reply is spoken aloud."
  }
}
```

## Prompt

```
The student is facing this: {topic}

Give them the first moves to make, most useful first, as at most {subtasks} pieces of work.

Say which single thing to do first, and why it comes first. Write plain spoken prose in at most 3 sentences - no bullets, no numbering, no markdown, because this is read aloud. End with one concrete next step.

Do not do the work for them. Point at what to do, don't produce the answer.
```

## Steps

1. The router picks this skill and extracts `topic` (required) and optionally `subtasks`.
2. The registry validates those arguments against the schema above.
3. The prompt is rendered with them and sent to the declared tier (`super`).
4. The reply is returned to the panel, shown, and spoken aloud.

## Verification

The reply should name a first move and a reason, contain **no bullet characters
or numbering**, and stay within three sentences. If it comes back as a list, the
prompt is not being honoured and the tier or prompt needs attention.
