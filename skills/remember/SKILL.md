---
name: remember
description: "Save something the student explicitly asked you to remember."
version: 0.1.0
author: Cadence
license: MIT
tier: nano
action: remember
requires_confirmation: false
when_to_use: "the student explicitly asks you to remember, note, save, or keep something for later - 'remember that my midterm is on the 12th', 'note that I hate group projects', 'keep in mind I'm bad at stoichiometry', 'don't forget my advisor is Dr. Chen'"
not_for: "a question, a request to explain or plan, or an ordinary statement the student did not ask you to save - silently storing things they never asked you to keep is the fastest way to lose their trust in what we hold on to. Also not for a request to create or draft a NEW skill for our own library: that is the general path, the only one that can write a skill file, and a sentence like 'save that as a skill' is a request for a new skill rather than a fact to remember"
tools: []
metadata:
  hermes:
    tags: [memory, capture]
---

# remember

Turns one explicit instruction — *"remember that my organic chemistry midterm is
November 12th"* — into one line in a Markdown file the student owns.

This skill writes; it does not speak. Its reply is a short deterministic
confirmation, because spending a model call to say *"got it"* would be a waste
of the student's money and about two seconds of their time.

## When to Use

Use it when the student **explicitly** asks for something to be kept: "remember
that…", "note that…", "keep in mind…", "don't forget…".

The word *explicit* is the entire design, and it is load-bearing. An
always-on extraction pass would quietly accumulate facts the student never
chose to hand over, and the failure mode is invisible — they would only find out
by opening a file they didn't know existed. So memory is written when asked, and
at no other time. Nothing else in the library writes here.

## Input

```json
{
  "topic": {
    "type": "string",
    "default": "general",
    "enum": ["deadlines", "classes", "weak_topics", "preferences", "people", "general"],
    "description": "Which file to save it in: deadlines (any date or due date), classes (what they are taking), weak_topics (a subject they struggle with), preferences (how they like to be taught), people (advisors, professors, group members), general (anything else). The enum is enforced by the router, so an invented name is rejected rather than silently filed elsewhere - one file per utterance turns memory into a junk drawer."
  },
  "text": {
    "type": "string",
    "required": true,
    "description": "The fact as ONE SELF-CONTAINED sentence about the student, in the third person, e.g. 'my organic chemistry midterm is on 2026-11-12'. It must still make sense on its own months later with no other context around it: write 'my advisor is Dr. Chen', never just 'Dr. Chen'. Strip the request framing ('remember that', 'don't forget') - store the fact, not the instruction. Resolve a relative date ('next Thursday') to a real date when they gave you enough to do it, and leave it as they said it when they did not."
  }
}
```

## Steps

Implemented in `backend/memory.py` and dispatched from `backend/server.py`, not
in a prompt. The handler:

1. **Requires an explicit request in the utterance itself.** This is the gate in
   `_action_blocked()`, and it is not a formality: we tested the prompt-only
   version live and *"my midterm is on November 12"* — a statement, not a
   request — was routed here and saved. The model cannot be the only thing
   standing between a student's passing remark and a file we keep about them. If
   the router chooses this skill and the utterance contains no explicit ask,
   the write does not happen and the turn is answered as `general` instead.
   The asymmetry is the point: a false negative costs one clarifying sentence,
   a false positive stores something they never handed over.
2. **`topic` is a declared `enum`**, so the router is told the six legal values
   in its own prompt and the router's validator rejects anything else, which
   triggers the corrective retry. This replaced a prose instruction in the field
   description that the router never saw — it was inventing a new topic on every
   utterance (`stoichiometry`, `organic chemistry midterm`), and every fact landed
   in a different file. `canonical_topic()` remains as a backstop for a skill that
   declares no enum.
3. Appends the fact to `memory/<topic>.md` as a plain bullet, with a stable id
   in a trailing HTML comment so a single fact can be deleted later without
   renumbering its neighbours.
4. Writes atomically (temp file, then `os.replace`), so an interrupted write
   cannot truncate the student's memory.
5. Returns a one-line confirmation naming the topic, so the student is told
   *where* it went rather than just that something happened.

No model call is made after routing. The declared `tier: nano` exists only
because every skill must declare a valid tier; the router's own Nano call is the
only spend this turn incurs.

## Verification

Covered by `backend/tests/test_memory.py`. The properties that matter:

- a saved fact survives a reload, and reads back verbatim;
- a rejected topic name leaves nothing on disk;
- deleting one fact leaves its neighbours untouched;
- `memory/*.md` stays readable as ordinary Markdown — which is the promise the
  README makes to the student, and the reason this is not a database.
