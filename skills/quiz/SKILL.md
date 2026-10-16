---
name: quiz
description: "Quiz the student: write questions on a topic or the page they are reading, then ask them one at a time."
version: 0.1.0
author: Cadence
license: MIT
tier: super
action: quiz
requires_confirmation: false
context: [memory, page]
when_to_use: "the student asks to be tested or quizzed — 'quiz me on chapter 4', 'quiz me, ten questions', 'test me on the page I'm reading', 'ask me some questions about stoichiometry', 'quiz me on what I'm weak at'"
not_for: "a request to explain, plan, rephrase, or remember something, a question that wants a factual answer ('what is a ketone', 'when is the midterm'), and anything they did not ask to be tested on — quizzing someone who asked for help is not helpful"
tools: []
metadata:
  hermes:
    tags: [practice, assessment, education]
---

# quiz

The first skill in this library that **runs over several turns**. It writes a set
of questions, asks the first one, and then listens: the student's next answer is
graded against the expected answer, the score goes into `progress/`, and the next
question follows. Up to the last one, which ends with the score and one next step.

Three things make this work, and each of them is a decision rather than a
mechanism.

**1. The questions are written once; the asking is deterministic.** One model call
produces the whole set (and the expected answer for each). After that, nothing
about *which* question comes next is a model's choice — it is the next
`unanswered` question in the file. A model asked to "ask question 3" can
accidentally ask question 2 again, or invent a question 6, and both sound like
bugs to a student who is counting.

**2. Code decides whether a turn is an answer, and the grader goes first.** While a
quiz is open the grader runs **before** the router, and it may **decline**: an
utterance that is not an attempt at the question at all comes back as `off_topic`,
and only then does the router run. So *"explain that again"* mid-quiz gets an
explanation and leaves the question open, while *"the stroma"* gets graded.

We shipped the other order first and it failed live on the first try:
*"plants use light to make sugar"* is a sentence **about a topic**, so the router
filed it as `explain` and explained the student's own answer back to them instead
of grading it. The router knows *which skill is this about*; it cannot know *is
this an answer* — the grader is the component that judges that, so it goes first.
It is also the cheaper order: one Super call, no router call. This rule lives in
`backend/server.py`, next to the page gate and the memory gate, for the same
reason: it is a policy about the student's data, so the prompt is not allowed to
be the only thing enforcing it.

**3. The escapes are free.** *"stop"*, *"repeat that"*, *"how am I doing"* and
*"I don't know"* are answered in code with no model call at all. They are the four
things a student says during a quiz that have nothing to do with the material,
and paying a rounding error to have a model interpret them would be both slower
and less predictable. *"I don't know"* is worth calling out: it is scored as
**incorrect** (it is the clearest possible signal of a weak topic, which is what
the Progress tab is for), the correct answer is read back, and life goes on —
rather than pretending they skipped a question they actually faced.

## When to Use

Use it when the student asks to be **tested**: *"quiz me on chapter 4"*,
*"test me, five questions"*, *"ask me something about the Krebs cycle"*.

Do not use it as a way to teach. If they ask what something means, that is
`explain`; if they ask for a plan, that is `plan`; if they ask a factual question,
the `general` path is faster and cheaper. The distinction matters because a quiz
is the one skill that asks the student for work rather than doing it.

## Input

```json
{
  "topic": {
    "type": "string",
    "description": "What to be quizzed on, in their words ('chapter 4', 'SN1 reactions', 'the page I'm reading', 'my weak topics'). Leave it out when they just say 'quiz me' - the material they are looking at and what we know they struggle with are then used instead, and if there is neither, the skill asks what to quiz on rather than inventing a subject."
  },
  "count": {
    "type": "integer",
    "default": 5,
    "description": "How many questions they asked for ('ten questions' -> 10). Leave it out unless they said a number; five is the default and the maximum is ten, because a longer sitting stops being a quiz and starts being an exam."
  }
}
```

## Prompt

The generation call (one per quiz, on `super`):

```
Write {count} quiz questions for a college student on: {topic}

Each question must be answerable in a sentence or two and checkable against the material in the labelled sections below when there is any. Vary the angle - a definition, a mechanism, one applied question - and stay strictly inside the topic.

Reply with ONLY JSON: {"questions": [{"question": "...", "answer": "..."}]}

"answer" is what a correct answer would say, in one sentence, and it is what the answer is graded against. Ask exactly {count} questions.
```

The grading call (one per answer, on `super`):

```
A student is answering one quiz question. Judge this answer and nothing else.

Question: {question}
A correct answer would say: {expected}

The student said: {answer}

Reply with ONLY JSON: {"verdict": "correct" | "incorrect" | "off_topic", "feedback": "one short sentence"}

"correct" when the answer shows the understanding the question asks for, in any words, however terse. "incorrect" for a real attempt that misses something. "off_topic" when it is not an attempt at this question at all - a different question, a greeting, a request to stop. Feedback is one spoken sentence: if correct, name what made it right; if incorrect, name the missing piece without reciting the whole answer.
```

## Steps

1. **The explicit request is required.** The router choosing this skill is not
   enough — `_action_blocked()` in `backend/server.py` also demands that the
   utterance asks to be quizzed (the same shape of rule that stops a passing
   remark from being written to memory). Otherwise the turn is answered as
   `general`.
2. **What to quiz on** is resolved in order: the `topic` argument; else the page
   they asked us to read (if one was read this turn, it travels in a labelled
   block); else the weak topics we already know from memory; else the skill asks,
   with no model call spent.
3. **Generation** is one `super` call that must return JSON. A reply that is not
   JSON gets one corrective retry, then the turn fails with a sentence the student
   can act on — no half-written quiz is saved.
4. **The set is written to `progress/<date>-<topic>-<id>.md`**, questions and
   expected answers included, and the first question is read aloud with its number
   and the total.
5. **Answers** are graded one per turn (see the note above about precedence), the
   verdict and their words are appended to the file, and the reply is the feedback
   plus the next question. A student can open that file at any point and see
   exactly what they were asked and how they did.
6. **The last answer ends the quiz**: the session is closed, the score spoken, and
   the reply offers one next step — a round on the ones they missed, or an
   explanation.

## Verification

`backend/tests/test_quiz.py` covers the store and the turn: a new quiz writes one
file and speaks question 1; a graded answer lands in that file with the student's
words; *"stop"*, *"repeat that"* and *"how am I doing"* each answer with **zero
model calls**; *"I don't know"* scores incorrect and reveals the answer; and an
utterance the router sends to another skill leaves the quiz open rather than
grading it. Live, the whole loop runs against Token Factory in about two calls per
answer, which is why the meta line under each reply is worth watching.
