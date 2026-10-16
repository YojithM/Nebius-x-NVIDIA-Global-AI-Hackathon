---
name: plan
description: "Fit the work into the time the student actually has, across the days leading up to a deadline."
version: 0.1.0
author: Cadence
license: MIT
tier: ultra
requires_confirmation: false
context: [memory, page]
when_to_use: "the student asks how to fit work into the time they have — 'plan the next four evenings', 'how should I split this over the week', 'plan my revision before Friday', 'when should I do this by'"
not_for: "a request for only the first move or two ('where do I start', 'break this into pieces'), which is study_breakdown, or a request to explain, rephrase, quiz, or remember something. Also not for a schedule with no deadline or time span in it, which leaves nothing to allocate, and not for a request to create or draft a NEW skill for our own library - that is the general path, and a plan for the task is not the skill file they asked for"
tools: []
metadata:
  hermes:
    tags: [planning, scheduling, education]
---

# plan

The only shipped skill that runs on **Ultra**, and it is here on purpose.
Allocating a fixed amount of time across several days, against a deadline, is the
place where a wrong answer costs the student real hours — the same class of
problem as the readiness estimate Ultra is fenced for in `config/models.json`.
Everything else in the library explains, restates, or splits; this one reasons
about time.

Two things this skill needs that nothing else does: the deadlines the student
asked us to remember, and — when they ask about the page in front of them — the
assignment's actual requirements. Both are declared in `context`, so both are
attached only when they apply and only when the student invoked them.

## When to Use

Use it when the student has a **span of time** and something to fit into it:
"the next four evenings", "before Friday", "I have about six hours this week".

Do not use it for the smaller question "where do I start" — that is
`study_breakdown`, and its three-move answer is deliberately not a schedule. Do
not use it when there is no time span at all: with no days and no hours, there is
nothing to allocate, and inventing a schedule is worse than asking.

## Input

```json
{
  "goal": {
    "type": "string",
    "required": true,
    "description": "What the time has to be spent on, in the student's own words: 'the organic chem midterm', 'the lab report and the problem set', 'this assignment'"
  },
  "deadline": {
    "type": "string",
    "description": "When it is due, exactly as they said it ('Tuesday', 'the 12th'). Leave it out when they did not say - the memory section may already hold the date."
  },
  "time_available": {
    "type": "string",
    "description": "How much time they actually have, in their words: 'four evenings', 'about six hours this week'. If they did not say, leave it out and ask - a plan for unknown time is a guess wearing a schedule's clothes."
  }
}
```

## Prompt

```
The student needs to fit this into the time they have: {goal}
Deadline as they said it: {deadline}
Time they have: {time_available}

Give them a schedule: which day (or block) gets which piece, in the order that finishes the work by the deadline, and say which block is the one that must not slip.

Use the dates in the STUDENT MEMORY section when they are relevant, and say which date you used so they can correct it. If the deadline or the time available came back "(not specified)" and memory does not supply it, ask for that one thing instead of building a plan on a guess.

Put the hardest or earliest-deadline work first, and leave the last block as slack - something always takes longer. Say in one clause what to drop if a block is lost. At most 3 sentences of plain spoken prose, no markdown, no lists. Do not do the work for them: this is about when, not what the answers are.
```

## Steps

1. The router picks this skill and extracts `goal` (required), optionally
   `deadline` and `time_available`.
2. The backend attaches the two context sections the skill declares: the student's
   saved memory (deadlines, weak topics) and — only if they explicitly asked us to
   read it — the page they are on. Memory is theirs; the page is only ever shown
   when they invoked the read.
3. The prompt runs on **Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`), the only
   skill that does, which is why the meta line under this reply shows a visibly
   higher cost than the others.

## Verification

Ask *"plan my week, I have the organic chem midterm and about six hours"* with a
deadline saved in memory, and check three things: the plan names days or blocks,
it names the date it used from memory (so a wrong date is visible rather than
baked in), and it leaves slack. Then ask *"plan it"* with no time span at all and
confirm it asks for the missing piece instead of producing a schedule.
