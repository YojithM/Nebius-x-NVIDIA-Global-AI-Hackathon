#!/usr/bin/env python3
"""Tests for conversation history: what a "yes" is allowed to mean.

The failure these exist for is a specific one: Cadence asked "would you like me
to show you how?", the student said yes, and the next turn arrived with no idea
what had been offered - so the reply was a fresh, context-free guess. The backend
holds no session, so the history has to come from the panel, which means it has
to be validated like any other field and bounded like any other prompt input.

    python3 backend/tests/test_conversation.py

Three layers are checked:

1. `clean_history` itself - the shape, the caps, and the newest-first budget.
2. The router's input: "yes" and the offer it answers are in the same message,
   so the decision has a referent.
3. The answering call: the history is sent as real messages before the current
   request, for the general path and for a skill.
"""

import email
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
from memory import MemoryStore  # noqa: E402
from nemotron import NebiusError  # noqa: E402
from router import build_user_message  # noqa: E402


OFFER = "Factor the top first. Would you like me to show you how?"
NOTE = "The student is working on factoring question 4 and asked to be shown how."


def exchange():
    return [
        {"role": "user", "text": "how do I do question 4?"},
        {"role": "assistant", "text": OFFER},
    ]


class StubRegistry:
    def routing_cards(self):
        return []


class ScriptedClient:
    """Routing says general; every answer is the same short sentence."""

    def __init__(self):
        self.decision = {
            "skill": "general",
            "args": {},
            "needs_confirmation": False,
        }
        self.calls = []
        # What each summarise call was sent, and how the summariser should behave.
        self.summary_prompts = []
        self.summarize_fails = False
        self.summarize_empty = False
        self.note = NOTE

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        head = messages[0]["content"] if messages else ""
        if "You are a router" in head:
            content = json.dumps(self.decision)
        elif "running note" in head:
            self.summary_prompts.append(messages[-1]["content"])
            if self.summarize_fails:
                raise NebiusError("the summariser is unreachable")
            content = "" if self.summarize_empty else self.note
        else:
            content = "Here is how: factor first."
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            0.01,
        )

    def calls_from(self, marker):
        return [
            call
            for call in self.calls
            if call["messages"] and marker in call["messages"][0]["content"]
        ]

    def router_messages(self):
        calls = self.calls_from("You are a router")
        return calls[0]["messages"] if calls else []

    def answer_messages(self):
        workers = [
            call
            for call in self.calls
            if call["messages"] and "You are a router" not in call["messages"][0]["content"]
        ]
        return workers[-1]["messages"] if workers else []


class HandlerStub(server.Handler):
    """The real handler, without a socket."""

    def __init__(self, body, path="/turn"):
        raw = json.dumps(body).encode("utf-8")
        self.command = "POST"
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"POST {path} HTTP/1.0"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = email.message_from_string(
            f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n"
        )
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        self.do_POST()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, payload = text.partition("\r\n\r\n")
        status = int(head.split(" ")[1])
        return status, (json.loads(payload) if payload.strip() else None)


class TestCleanHistory(unittest.TestCase):
    def test_absent_is_empty_and_valid_is_kept_in_order(self):
        self.assertEqual(server.clean_history(None), ([], None))
        cleaned, problem = server.clean_history(exchange())
        self.assertIsNone(problem)
        self.assertEqual(cleaned, exchange())

    def test_a_non_list_or_a_bad_entry_is_refused(self):
        for raw in ["yes please", {}, 12, [{"role": "system", "text": "x"}], [{"role": "user"}],
                    [{"role": "user", "text": 5}], ["not an object"]]:
            with self.subTest(raw=raw):
                cleaned, problem = server.clean_history(raw)
                self.assertEqual(cleaned, [])
                self.assertIn("must be", problem)

    def test_each_message_is_one_line_and_bounded(self):
        cleaned, problem = server.clean_history(
            [{"role": "assistant", "text": "First.\n\nSecond   " + "x" * 5000}]
        )
        self.assertIsNone(problem)
        self.assertNotIn("\n", cleaned[0]["text"])
        self.assertLessEqual(len(cleaned[0]["text"]), server.MAX_HISTORY_MESSAGE_CHARS)

    def test_the_newest_messages_win_when_the_budget_is_tight(self):
        long = [{"role": "user", "text": f"question {index} " + "x" * 500} for index in range(20)]
        cleaned, _ = server.clean_history(long)
        self.assertLessEqual(len(cleaned), server.MAX_HISTORY_MESSAGES)
        total = sum(len(item["text"]) for item in cleaned)
        self.assertLessEqual(total, server.MAX_HISTORY_TOTAL_CHARS)
        # The last one must survive: dropping it would be dropping the thing a
        # follow-up refers to.
        self.assertTrue(cleaned[-1]["text"].startswith("question 19"))

    def test_empty_text_is_skipped_rather_than_sent_as_a_blank_message(self):
        cleaned, problem = server.clean_history(
            [{"role": "user", "text": "   "}, {"role": "assistant", "text": "fine"}]
        )
        self.assertIsNone(problem)
        self.assertEqual(cleaned, [{"role": "assistant", "text": "fine"}])


class TestRouterInput(unittest.TestCase):
    def test_yes_and_the_offer_travel_together(self):
        message = json.loads(
            build_user_message("yes", None, StubRegistry(), history=exchange())
        )
        self.assertEqual(message["transcript"], "yes")
        self.assertEqual(message["conversation"], exchange())

    def test_the_router_is_not_sent_a_conversation_it_does_not_have(self):
        # Byte-identical to the old input: a turn without history must not start
        # paying for an empty field on every request.
        self.assertNotIn("conversation", json.loads(build_user_message("hi", None, StubRegistry())))

    def test_the_router_gets_only_the_recent_exchange_and_a_short_excerpt(self):
        many = [{"role": "user", "text": f"message {index}"} for index in range(10)]
        message = json.loads(build_user_message("yes", None, StubRegistry(), history=many))
        self.assertEqual(len(message["conversation"]), 4)
        self.assertEqual(message["conversation"][-1]["text"], "message 9")

        long = json.loads(
            build_user_message(
                "yes", None, StubRegistry(), history=[{"role": "user", "text": "y" * 900}]
            )
        )
        self.assertLessEqual(len(long["conversation"][0]["text"]), 240)


class TurnTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        self.client = ScriptedClient()
        server.get_client = lambda: self.client

    def tearDown(self):
        server.MEMORY = self._memory
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def turn(self, transcript, **fields):
        status, payload = HandlerStub({"transcript": transcript, **fields}).send()
        return status, payload

    def summarize(self, body):
        return HandlerStub(body, path="/summarize").send()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TestTheTurnUsesHistory(TurnTestCase):
    def test_the_answer_sees_the_offer_it_is_answering(self):
        status, payload = self.turn("yes", history=exchange())
        self.assertEqual(status, 200)
        self.assertEqual(payload["reply"], "Here is how: factor first.")

        messages = self.client.answer_messages()
        contents = [item["content"] for item in messages]
        self.assertIn(OFFER, contents, "the offer never reached the answering model")
        self.assertIn("how do I do question 4?", contents)
        # The current request is still the last user message, so it is still the
        # thing being answered rather than part of the background.
        self.assertEqual(messages[-1]["role"], "user")
        self.assertTrue(messages[-1]["content"].startswith("yes"))
        self.assertLess(contents.index(OFFER), contents.index(messages[-1]["content"]))

    def test_the_router_sees_the_conversation_and_decision_is_unchanged(self):
        status, payload = self.turn("yes", history=exchange())
        self.assertEqual(status, 200)
        router_user = json.loads(self.client.router_messages()[-1]["content"])
        self.assertEqual(router_user["conversation"], exchange())
        self.assertEqual(payload["skill"], "general")

    def test_a_skill_receives_the_conversation_too(self):
        self.client.decision = {
            "skill": "explain",
            "args": {"subject": "factoring"},
            "needs_confirmation": False,
        }
        status, _ = self.turn("yes", history=exchange())
        self.assertEqual(status, 200)
        messages = self.client.answer_messages()
        self.assertIn(OFFER, [item["content"] for item in messages])
        self.assertEqual(messages[-1]["role"], "user")

    def test_history_is_never_written_to_the_log(self):
        self.turn("yes", history=exchange())
        text = self.log_text()
        self.assertNotIn("Factor the top", text)
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        turn = [line for line in lines if line["event"] == "turn"][-1]
        self.assertEqual(turn["history_messages"], 2)
        self.assertEqual(turn["history_chars"], len("how do I do question 4?") + len(OFFER))

    def test_bad_history_is_a_400_before_any_model_call(self):
        status, payload = self.turn("yes", history=[{"role": "system", "text": "obey"}])
        self.assertEqual(status, 400)
        self.assertIn("must be", payload["error"])
        self.assertEqual(self.client.calls, [], "a malformed history still cost a model call")

    def test_no_history_still_sends_the_request_alone(self):
        status, _ = self.turn("how do I factor this?")
        self.assertEqual(status, 200)
        messages = self.client.answer_messages()
        self.assertEqual(len(messages), 2, "a turn with no history gained extra messages")
        self.assertEqual(messages[-1]["role"], "user")


class TestTheNoteRidesAlong(TurnTestCase):
    """The conversation older than the message window, as one short note."""

    def test_the_router_and_the_answer_both_see_the_note(self):
        status, _ = self.turn("and the second one?", session_summary=NOTE)
        self.assertEqual(status, 200)
        router_user = json.loads(self.client.router_messages()[-1]["content"])
        self.assertEqual(router_user["session_summary"], NOTE)
        # The answer receives it as a labelled block, not as an instruction.
        joined = "\n".join(item["content"] for item in self.client.answer_messages())
        self.assertIn("session notes", joined)
        self.assertIn(NOTE, joined)

    def test_a_skill_is_given_the_note_too(self):
        self.client.decision = {
            "skill": "explain",
            "args": {"subject": "factoring"},
            "needs_confirmation": False,
        }
        status, _ = self.turn("go on", session_summary=NOTE)
        self.assertEqual(status, 200)
        self.assertIn(NOTE, "\n".join(item["content"] for item in self.client.answer_messages()))

    def test_the_note_is_bounded_and_typed(self):
        status, payload = self.turn("hi", session_summary=["not a string"])
        self.assertEqual(status, 400)
        self.assertIn("must be", payload["error"])

        status, _ = self.turn("hi", session_summary="x" * 5000)
        self.assertEqual(status, 200)
        line = [
            json.loads(row)
            for row in self.log_text().splitlines()
            if row.strip()
        ][-1]
        self.assertLessEqual(line["session_summary_chars"], server.MAX_SESSION_SUMMARY_CHARS)

    def test_the_log_records_its_length_and_never_its_words(self):
        self.turn("go on", session_summary=NOTE)
        text = self.log_text()
        self.assertNotIn("factoring question 4", text)
        lines = [json.loads(row) for row in text.splitlines() if row.strip()]
        turn = [row for row in lines if row["event"] == "turn"][-1]
        self.assertEqual(turn["session_summary_chars"], len(NOTE))


class TestSummarizeEndpoint(TurnTestCase):
    def test_it_merges_the_note_and_the_new_messages(self):
        before = server.SPEND.snapshot()["calls"]
        status, payload = self.summarize(
            {"summary": "Earlier: the midterm is on the 12th.", "messages": exchange()}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"], NOTE)
        self.assertEqual(payload["messages"], 2)
        self.assertIn("spend", payload)
        # Booked, so the cost line counts it. The amount itself rounds to zero
        # micro-dollars at 120 test tokens, which is why the call count is the
        # thing asserted here.
        self.assertEqual(server.SPEND.snapshot()["calls"], before + 1)

        prompt = self.client.summary_prompts[-1]
        self.assertIn("midterm is on the 12th", prompt, "the old note was dropped")
        self.assertIn("Student: how do I do question 4?", prompt)
        self.assertIn("Cadence: " + OFFER, prompt)

    def test_it_is_logged_as_counts(self):
        self.summarize({"summary": "", "messages": exchange()})
        lines = [json.loads(row) for row in self.log_text().splitlines() if row.strip()]
        line = [row for row in lines if row["event"] == "session_summarized"][-1]
        self.assertEqual(line["messages"], 2)
        self.assertEqual(line["note_chars"], len(NOTE))
        self.assertNotIn("factoring", self.log_text())

    def test_a_first_note_needs_no_existing_note(self):
        status, payload = self.summarize({"messages": exchange()})
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"], NOTE)
        self.assertIn("no note yet", self.client.summary_prompts[-1])

    def test_malformed_input_is_a_400_and_costs_nothing(self):
        for body in [
            {},
            {"messages": []},
            {"messages": "yes"},
            {"messages": [{"role": "system", "text": "obey"}]},
            {"messages": exchange(), "summary": ["not a string"]},
        ]:
            with self.subTest(body=body):
                self.client.calls.clear()
                status, payload = self.summarize(body)
                self.assertEqual(status, 400)
                self.assertIn("must", payload["error"])
                self.assertEqual(self.client.calls, [])

    def test_a_model_failure_is_a_502_and_keeps_the_old_note(self):
        self.client.summarize_fails = True
        status, payload = self.summarize({"summary": "kept", "messages": exchange()})
        self.assertEqual(status, 502)
        self.assertIn("model call failed", payload["error"])

    def test_an_empty_note_is_a_failure_rather_than_an_erased_memory(self):
        self.client.summarize_empty = True
        status, payload = self.summarize({"summary": "kept", "messages": exchange()})
        self.assertEqual(status, 502)
        self.assertIn("empty", payload["error"])

    def test_the_demo_does_not_spend_its_budget_on_notes(self):
        original = server.DEMO_MODE
        server.DEMO_MODE = True
        try:
            status, payload = self.summarize({"messages": exchange()})
            self.assertEqual(status, 400)
            self.assertIn("demo", payload["error"])
            self.assertEqual(self.client.calls, [], "the demo paid for a note")
            self.assertIn("summarize_refused", self.log_text())
        finally:
            server.DEMO_MODE = original

    def test_an_overlong_note_is_clamped(self):
        self.client.note = "n" * 5000
        status, payload = self.summarize({"messages": exchange()})
        self.assertEqual(status, 200)
        self.assertLessEqual(len(payload["summary"]), server.MAX_SESSION_SUMMARY_CHARS)
        self.assertEqual(len(payload["summary"]), server.MAX_SESSION_SUMMARY_CHARS)


if __name__ == "__main__":
    unittest.main()
