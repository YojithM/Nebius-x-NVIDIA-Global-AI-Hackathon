#!/usr/bin/env python3
"""Tests for what we are willing to say out loud.

    python3 backend/tests/test_replies.py

This file exists because of a live turn. Driving the panel in `/demo`, the `plan`
skill (Ultra, a thinking tier) answered a spoken question with **2,900
characters of its own deliberation** — "The student needs a schedule for lab
report 3... I need to create a schedule using 25-min morning blocks" — cut off
mid-word when the output budget ran out. The panel was about to read it aloud.

The tell is `finish_reason: length`. A reply that ran out of budget is not an
answer, whatever it contains, and this is the one signal we act on:

1. **Truncated means try again, briefly.** The retry asks for at most three
   sentences of spoken words, with thinking off and a small budget — small
   because the failure *was* deliberation, and room to deliberate again would
   reproduce it.
2. **The retry is what gets said.** Whichever attempt is usable is the one the
   student hears, and the cost of both is reported, because the second call was
   real money.
3. **Complete but long is not a defect**, so it is logged rather than rewritten.
   Shortening an explanation the student asked for would be us being clever at
   their expense.
4. **Nothing is ever empty.** If both attempts are unusable the first one is kept
   and the log says so, because silence is the one reply a voice assistant cannot
   give.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
from skills import SkillRegistry  # noqa: E402

SKILL = """---
name: plan_study
description: "break an assignment into study blocks"
version: 0.1.0
tier: ultra
requires_confirmation: false
when_to_use: "when the student wants a plan for a piece of work"
tools: []
metadata:
  hermes:
    tags: [plan]
---

# plan_study

## When to Use

Planning.

## Input

```json
{
  "goal": {"type": "string", "description": "what to do"},
  "deadline": {"type": "string", "default": "", "description": "when it is due"}
}
```

## Prompt

```
Plan this: {goal} by {deadline}.
```
"""

# A capture of the shape this defect arrives in: prose, no ending, and the
# finish_reason that says it was cut off rather than finished.
RUNAWAY = (
    "The student needs a schedule for lab report 3, deadline October 20. "
    "I need to create a schedule using 25-min morning blocks. Let me think "
    "about which days are free. They have classes MWF 10am, so mornings might "
    "be busy. Let me work out the order of the pieces, then produce the"
)
GOOD_ANSWER = "Spend Monday on the calculations, then draft the report in your morning blocks. Ask me to check the plan once you start."


def reply(content, finish_reason="stop", prompt_tokens=100, completion_tokens=40):
    return (
        {
            "choices": [
                {"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}
            ],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
        0.01,
    )


class ScriptedClient:
    """Plays a queue of replies and records every call, including its options."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append(
            {
                "tier": tier,
                "messages": [dict(m) for m in messages],
                "max_tokens": max_tokens,
                "extra": extra,
            }
        )
        if not self.responses:
            return reply(GOOD_ANSWER)
        return self.responses.pop(0)

    @property
    def prompts(self):
        return [call["messages"][-1]["content"] for call in self.calls]

    @property
    def last_prompt(self):
        return self.prompts[-1] if self.prompts else ""


class ReplyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._client = server.get_client
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        folder = self.root / "skills" / "plan_study"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
        self.registry = SkillRegistry(
            self.root / "skills", tiers=list(server.CONFIG["tiers"])
        ).load()
        self.assertEqual(self.registry.errors, [])
        self.skill = self.registry.get("plan_study")
        self.logged = []

    def tearDown(self):
        server.get_client = self._client
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def run_skill(self, *responses):
        self.client = ScriptedClient(*responses)
        server.get_client = lambda: self.client
        result = server.execute_skill(
            self.skill,
            {"goal": "lab report 3", "deadline": "October 20"},
            {"log": lambda event, **fields: self.logged.append((event, fields))},
        )
        return result

    @staticmethod
    def events(logged):
        return [event for event, _ in logged]


class TestAUsableReply(ReplyTestCase):
    def test_a_normal_answer_costs_exactly_one_call(self):
        result = self.run_skill(reply(GOOD_ANSWER))
        self.assertEqual(result["reply"], GOOD_ANSWER)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.events(self.logged), [])

    def test_a_long_but_complete_answer_is_left_alone(self):
        """Long is not the defect. Cut off is."""
        long_answer = " ".join([GOOD_ANSWER] * 40)
        self.assertGreater(len(long_answer), server.MAX_SPOKEN_CHARS)
        result = self.run_skill(reply(long_answer))
        self.assertEqual(result["reply"], long_answer)
        self.assertEqual(len(self.client.calls), 1, "a long answer was second-guessed")
        self.assertIn("reply_long", self.events(self.logged))


class TestATruncatedReply(ReplyTestCase):
    def test_a_cut_off_reply_is_not_what_the_student_hears(self):
        result = self.run_skill(reply(RUNAWAY, finish_reason="length"), reply(GOOD_ANSWER))
        self.assertEqual(result["reply"], GOOD_ANSWER)
        self.assertEqual(len(self.client.calls), 2)

    def test_the_retry_asks_for_three_sentences_and_turns_thinking_off(self):
        self.run_skill(reply(RUNAWAY, finish_reason="length"), reply(GOOD_ANSWER))
        retry = self.client.calls[-1]
        self.assertIn("three short sentences", retry["messages"][-1]["content"])
        self.assertEqual(
            retry["extra"]["chat_template_kwargs"], {"enable_thinking": False}
        )
        # A small budget: the failure was deliberation, and giving it room to
        # deliberate again would reproduce the failure.
        self.assertEqual(retry["max_tokens"], server.SPOKEN_RETRY_MAX_TOKENS)
        self.assertLess(retry["max_tokens"], server.CONFIG["budgets"]["max_output_tokens_per_request"])

    def test_the_original_prompt_is_kept_in_the_retry(self):
        self.run_skill(reply(RUNAWAY, finish_reason="length"), reply(GOOD_ANSWER))
        self.assertTrue(
            any("Plan this: lab report 3" in call["messages"][-1]["content"] for call in self.client.calls),
            "the retry lost the question it was answering",
        )

    def test_the_cost_of_both_calls_is_reported(self):
        with_one = self.run_skill(reply(GOOD_ANSWER))["cost_usd"]
        with_two = self.run_skill(reply(RUNAWAY, finish_reason="length"), reply(GOOD_ANSWER))["cost_usd"]
        self.assertAlmostEqual(with_two, with_one * 2, places=8)

    def test_the_truncation_is_logged(self):
        self.run_skill(reply(RUNAWAY, finish_reason="length"), reply(GOOD_ANSWER))
        self.assertIn("reply_truncated", self.events(self.logged))

    def test_two_bad_attempts_say_so_rather_than_reading_a_scratchpad(self):
        """Silence is not an option; a scratchpad is not an answer."""
        result = self.run_skill(
            reply(RUNAWAY, finish_reason="length"),
            reply("More half-finished notes to myself about the schedule", finish_reason="length"),
        )
        self.assertTrue(result["reply"].strip(), "the student got no reply at all")
        self.assertNotIn("The student needs a schedule", result["reply"], "a scratchpad was read aloud")
        self.assertNotIn("half-finished notes to myself", result["reply"])
        self.assertIn("ask me again", result["reply"])
        self.assertIn("spoken_reply_unusable", self.events(self.logged))

    def test_an_empty_first_attempt_still_gets_the_empty_reply_retry_first(self):
        """ask_model's own retry fires on emptiness; this guard is for truncation."""
        result = self.run_skill(reply("", finish_reason="length"), reply(GOOD_ANSWER))
        self.assertEqual(result["reply"], GOOD_ANSWER)
        # Two calls: the empty-reply retry, which happened to answer properly.
        self.assertEqual(len(self.client.calls), 2)
        self.assertNotIn("spoken_reply_unusable", self.events(self.logged))


if __name__ == "__main__":
    unittest.main(verbosity=2)
