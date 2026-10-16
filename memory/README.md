# Your memory

This folder is the assistant's memory, and it is **yours**. It is plain Markdown,
one file per topic. Nothing here is hidden from you, and nothing here is
encrypted — open any file in any text editor, change it, or delete a line.

(This file is ignored by the loader, because it's documentation rather than
memory. So is `index.md`.)

## What's in here

One file per topic:

| File | What lands in it |
|---|---|
| `deadlines.md` | dates and due dates |
| `classes.md` | what you're taking |
| `weak_topics.md` | subjects you struggle with |
| `preferences.md` | how you like to be taught |
| `people.md` | advisors, professors, group members |
| `general.md` | anything that doesn't fit the above |

## The format

```markdown
---
topic: deadlines
title: Deadlines
updated: 2026-10-14
---

# Deadlines

- my organic chemistry midterm is on November 12 <!-- id: c042a5a1 added: 2026-10-14 -->
```

A fact is just a bullet. The `<!-- ... -->` comment after it holds the internal id
and the date — it's invisible when the file is rendered, so the line still reads
like a note rather than a database row. Don't feel you have to write one: a bullet
with no comment works fine, and the assistant will work out a stable id for it.

## How to change things

- **Delete a line** — the fact is gone. That's the whole mechanism.
- **Edit the words** — the assistant re-reads the file and picks up your edit.
  (If you edit a fact's text, its id changes with it, so it may briefly look like
  a different note.)
- **Add your own bullets** — they're read back like any other fact. Bullets inside
  a ``` code fence are treated as examples and skipped.
- **Delete a whole file** — the topic simply stops existing. It comes back if you
  ask the assistant to remember something of that kind again.
- **Use the Memory tab** — the panel lists everything here with a × on each note,
  if you'd rather not touch the files.

## What the assistant will NOT do

It only writes here when you explicitly ask — *"remember that…"*, *"note that…"*,
*"don't forget…"*. Passing remarks are not stored. That rule is enforced in the
backend (see `_action_blocked` in `backend/server.py`), not requested of the
model, because "we only keep what you asked us to keep" is a promise that has to
be true in code.
