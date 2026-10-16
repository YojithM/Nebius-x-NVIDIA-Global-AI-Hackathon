#!/usr/bin/env python3
"""Tests for Step 10's permissioning: the question, and the answer to it.

    python3 backend/tests/test_approvals.py

The gate has existed since Step 3 - a skill that declares `requires_confirmation`
is never run on the model's say-so. What Step 10 adds is the other half of a
question: until now the panel said *"awaiting your go-ahead"* and there was no
way to give one, so a Tier-2 action dead-ended in a sentence.

Five properties, each a way this goes wrong:

1. **The ask is stored, not re-derived.** A "yes" runs the decision that was
   shown. The test proves it by reading the prompt the executor received: if the
   server re-routed on approval, the store is consulted for nothing and the
   argument the student agreed to would be whatever the model said next.
2. **An approval is single-use.** A double click must not run the action twice,
   and a used id must not work later.
3. **A stale approval is not a permission.** Five minutes is the whole life of
   "shall I?", and after that the honest answer is to ask again.
4. **A refusal is free and final.** No model call, no write, and no second dialog
   appearing behind the first.
5. **Answering is not routing.** `confirm` and `cancel` are handled before the
   transcript is even validated, because the request is an answer to a question
   rather than a new utterance.
"""

import email.message
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
from approvals import Approvals  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402
from skills import SkillRegistry  # noqa: E402

# A skill that must be confirmed before it runs. The router is told to pick it
# with `needs_confirmation: false`, which is the mistake a model actually makes -
# and the escalation it is not allowed to undo.
CONFIRMING_SKILL = """---
name: rearrange_tabs
description: "group the open tabs by course"
version: 0.1.0
tier: nano
requires_confirmation: true
when_to_use: "when the student asks to tidy or group their browser tabs"
tools: []
metadata:
  hermes:
    tags: [tabs]
---

# rearrange_tabs

## When to Use

Tidying tabs.

## Input

```json
{
  "group_by": {"type": "string", "default": "course", "description": "how to group"}
}
```

## Prompt

```
Group the tabs by {group_by}.
```
"""


class TestTheQueue(unittest.TestCase):
    def test_an_approval_can_be_used_exactly_once(self):
        queue = Approvals()
        approval_id = queue.put("10.0.0.1", {"skill": "rearrange_tabs"})
        self.assertEqual(queue.take("10.0.0.1", approval_id)["skill"], "rearrange_tabs")
        self.assertIsNone(queue.take("10.0.0.1", approval_id), "it was used twice")

    def test_an_expired_approval_is_not_a_permission(self):
        queue = Approvals(ttl_seconds=0.05)
        approval_id = queue.put("10.0.0.1", {"skill": "rearrange_tabs"})
        time.sleep(0.06)
        self.assertIsNone(queue.take("10.0.0.1", approval_id))

    def test_another_session_cannot_answer_your_question(self):
        queue = Approvals()
        approval_id = queue.put("10.0.0.1", {"skill": "rearrange_tabs"})
        self.assertIsNone(queue.take("10.0.0.2", approval_id))
        self.assertEqual(queue.take("10.0.0.1", approval_id)["skill"], "rearrange_tabs")

    def test_an_unknown_id_is_nothing_rather_than_an_error(self):
        queue = Approvals()
        self.assertIsNone(queue.take("10.0.0.1", "ap9999"))
        self.assertIsNone(queue.take("10.0.0.1", None))

    def test_the_queue_is_bounded_and_drops_the_oldest_question(self):
        queue = Approvals(max_per_session=2)
        first = queue.put("10.0.0.1", {"n": 1})
        second = queue.put("10.0.0.1", {"n": 2})
        third = queue.put("10.0.0.1", {"n": 3})
        self.assertEqual(queue.count("10.0.0.1"), 2)
        self.assertIsNone(queue.take("10.0.0.1", first))
        self.assertIsNotNone(queue.take("10.0.0.1", second))
        self.assertEqual(queue.take("10.0.0.1", third)["n"], 3)

    def test_refusing_forgets_everything_that_session_was_asked(self):
        queue = Approvals()
        queue.put("10.0.0.1", {"n": 1})
        queue.put("10.0.0.1", {"n": 2})
        queue.put("10.0.0.2", {"n": 3})
        self.assertEqual(queue.drop("10.0.0.1"), 2)
        self.assertEqual(queue.count("10.0.0.1"), 0)
        self.assertEqual(queue.count("10.0.0.2"), 1, "another session was dropped too")

    def test_ids_are_not_guessable_across_sessions(self):
        """Two sessions asking at the same moment must not share an id."""
        queue = Approvals()
        one = queue.put("10.0.0.1", {"n": 1})
        two = queue.put("10.0.0.2", {"n": 2})
        self.assertNotEqual(one, two)


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_deploy.py for the reasoning)."""

    def __init__(self, path="/turn", body=None, command="POST", authorization=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"{command} {path} HTTP/1.0"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        lines = [f"Content-Length: {len(raw)}", "Content-Type: application/json"]
        if authorization is not None:
            lines.append(f"Authorization: {authorization}")
        self.headers = email.message_from_string("\r\n".join(lines) + "\r\n\r\n")
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        getattr(self, f"do_{self.command}")()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, body = text.partition("\r\n\r\n")
        return int(head.split(" ")[1]), (json.loads(body) if body.strip() else None)


class ScriptedClient:
    """Routes to the confirming skill, then answers whatever it is asked."""

    def __init__(self, decision=None):
        self.decision = decision or {
            "skill": "rearrange_tabs",
            "args": {"group_by": "course"},
            # The mistake this gate exists for: the model says no confirmation
            # is needed. The skill file disagrees, and the skill file wins.
            "needs_confirmation": False,
        }
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        if messages and "You are a router" in messages[0]["content"]:
            content = json.dumps(self.decision)
        else:
            content = "Grouped your tabs by course."
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            0.01,
        )

    @property
    def router_calls(self):
        return [
            call
            for call in self.calls
            if "You are a router" in call["messages"][0]["content"]
        ]

    @property
    def executor_prompts(self):
        return [
            call["messages"][-1]["content"]
            for call in self.calls
            if "You are a router" not in call["messages"][0]["content"]
        ]


class ApprovalTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._registry = server.REGISTRY
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._approvals = server.APPROVALS
        self._sent = server.SPEND
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.APPROVALS = Approvals()
        server.SPEND = type(server.SPEND)(server.CONFIG)
        server.REGISTRY = self.registry_with(CONFIRMING_SKILL)
        self.client = ScriptedClient()
        server.get_client = lambda: self.client

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.REGISTRY = self._registry
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.APPROVALS = self._approvals
        server.SPEND = self._sent
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def registry_with(self, text):
        root = self.root / "skills"
        folder = root / "rearrange_tabs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
        return SkillRegistry(root, tiers=list(server.CONFIG["tiers"])).load()

    def turn(self, transcript, **fields):
        stub = HandlerStub(body={"transcript": transcript, **fields})
        return stub.send()

    def answer(self, **fields):
        return HandlerStub(body=fields).send()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def ask(self):
        """Ask for something that needs permission. -> (status, payload)."""
        status, payload = self.turn("tidy up my tabs please")
        self.assertEqual(status, 200)
        self.assertTrue(payload["needs_confirmation"])
        return payload["confirmation"]["id"], payload


class TestTheQuestion(ApprovalTestCase):
    def test_a_skill_that_requires_confirmation_asks_instead_of_acting(self):
        approval_id, payload = self.ask()
        self.assertEqual(payload["skill"], "rearrange_tabs")
        self.assertEqual(payload["confirmation"]["skill"], "rearrange_tabs")
        self.assertIn("Shall I", payload["reply"])
        self.assertEqual(payload["confirmation"]["expires_in_seconds"], 300)
        # The question itself is free: the only money on this turn is the router
        # deciding which skill it was, and the skill has not run.
        self.assertEqual(payload["cost_usd"], payload["router"]["cost_usd"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.executor_prompts, [])

    def test_the_question_is_logged_without_the_words_of_the_request(self):
        self.ask()
        text = self.log_text()
        self.assertIn("approval_asked", text)
        self.assertNotIn("tidy up my tabs", text)

    def test_the_model_cannot_de_escalate_the_gate(self):
        """The router said confirmation was not needed; the skill file wins."""
        _, payload = self.ask()
        self.assertTrue(payload["needs_confirmation"])
        self.assertEqual(len(self.client.executor_prompts), 0)

    def test_a_reply_with_a_question_has_the_same_shape_as_one_without(self):
        _, asked = self.ask()
        server.REGISTRY, held = self.registry_with(CONFIRMING_SKILL), server.REGISTRY
        try:
            server.REGISTRY = self.registry_with(
                CONFIRMING_SKILL.replace("requires_confirmation: true", "requires_confirmation: false")
            )
            self.client.decision = {
                "skill": "general",
                "args": {"request": "hello"},
                "needs_confirmation": False,
            }
            status, plain = self.turn("what is a ketone")
        finally:
            server.REGISTRY = held
        self.assertEqual(status, 200)
        self.assertEqual(
            set(asked),
            set(plain),
            "the two paths answer with different keys, so one of them will drift",
        )
        self.assertIsNone(plain["confirmation"], "an ordinary turn offered a button")


class TestTheAnswer(ApprovalTestCase):
    def test_confirming_runs_the_decision_that_was_shown(self):
        approval_id, _ = self.ask()
        self.client.calls.clear()
        status, payload = self.answer(confirm=approval_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "rearrange_tabs")
        self.assertFalse(payload["needs_confirmation"], "still asking after a yes")
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(payload["reply"], "Grouped your tabs by course.")
        self.assertGreater(payload["cost_usd"], 0)
        # The proof that the stored decision ran: the argument the student agreed
        # to is the one the executor was given.
        self.assertTrue(
            any("Group the tabs by course" in prompt for prompt in self.client.executor_prompts),
            f"the executor never saw the agreed action: {self.client.executor_prompts}",
        )

    def test_the_router_is_not_consulted_again_on_a_yes(self):
        approval_id, _ = self.ask()
        self.client.calls.clear()
        self.answer(confirm=approval_id)
        self.assertEqual(self.client.router_calls, [], "the yes was routed as an utterance")

    def test_the_answer_is_logged_as_a_confirmed_turn(self):
        approval_id, _ = self.ask()
        self.answer(confirm=approval_id)
        text = self.log_text()
        self.assertIn("confirmed", text)
        self.assertIn("approval_asked", text)

    def test_an_approval_cannot_be_spent_twice(self):
        approval_id, _ = self.ask()
        self.assertEqual(self.answer(confirm=approval_id)[0], 200)
        status, payload = self.answer(confirm=approval_id)
        self.assertEqual(status, 409)
        self.assertIn("timed out or was already done", payload["error"])

    def test_an_unknown_approval_is_refused_rather_than_guessed_at(self):
        status, payload = self.answer(confirm="ap0042")
        self.assertEqual(status, 409)
        self.assertIn("ap0042", self.log_text())
        self.assertEqual(self.client.executor_prompts, [])

    def test_an_expired_approval_is_refused(self):
        server.APPROVALS = Approvals(ttl_seconds=0.05)
        approval_id, _ = self.ask()
        time.sleep(0.06)
        status, _ = self.answer(confirm=approval_id)
        self.assertEqual(status, 409)
        self.assertEqual(self.client.executor_prompts, [], "a stale yes ran something")

    def test_a_refusal_costs_nothing_and_runs_nothing(self):
        approval_id, _ = self.ask()
        self.client.calls.clear()
        status, payload = self.answer(cancel=approval_id)
        self.assertEqual(status, 200)
        self.assertIn("have not done that", payload["reply"])
        self.assertEqual(payload["cost_usd"], 0.0)
        self.assertEqual(self.client.calls, [], "a refusal made a model call")
        self.assertIn("approval_declined", self.log_text())

    def test_a_refusal_of_something_we_are_not_asking_is_honest(self):
        status, payload = self.answer(cancel="ap0042")
        self.assertEqual(status, 200)
        self.assertIn("not waiting on an answer", payload["reply"])
        self.assertEqual(payload["cost_usd"], 0.0)

    def test_a_refusal_drops_the_other_questions_too(self):
        first, _ = self.ask()
        second, _ = self.ask()
        self.answer(cancel=first)
        self.assertEqual(server.APPROVALS.count("127.0.0.1"), 0)
        self.assertEqual(self.answer(confirm=second)[0], 409)

    def test_a_yes_after_a_refusal_is_too_late(self):
        approval_id, _ = self.ask()
        self.answer(cancel=approval_id)
        status, _ = self.answer(confirm=approval_id)
        self.assertEqual(status, 409, "a refused action ran anyway")


class TestTheRequestContract(ApprovalTestCase):
    def test_accepting_an_approval_is_not_a_transcript(self):
        """No transcript, no complaint: it is an answer, not an utterance."""
        approval_id, _ = self.ask()
        status, payload = self.answer(confirm=approval_id)
        self.assertEqual(status, 200)
        self.assertNotIn("transcript", payload.get("error", ""))

    def test_a_plain_turn_still_needs_a_transcript(self):
        status, payload = HandlerStub(body={}).send()
        self.assertEqual(status, 400)
        self.assertIn("transcript", payload["error"])

    def test_an_approval_id_that_is_not_a_string_is_a_bad_request(self):
        status, payload = HandlerStub(body={"confirm": 123}).send()
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])

    def test_an_empty_approval_id_falls_through_to_the_transcript_check(self):
        status, payload = HandlerStub(body={"confirm": "   "}).send()
        self.assertEqual(status, 400)
        self.assertIn("transcript", payload["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
