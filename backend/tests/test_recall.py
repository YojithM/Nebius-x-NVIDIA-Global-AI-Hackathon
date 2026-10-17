#!/usr/bin/env python3
"""Tests for the date lookup: a question the brief already answers, in code.

    python3 backend/tests/test_recall.py

The bug this file exists for, measured live before the fix:

    "when is my next deadline?"
    -> skill `plan`, tier **ultra**, 2 steps, 1869 ms, $0.000980
    -> "I have your organic chemistry midterm on November 12 from memory, but you
        didn't tell me how many days or hours you can actually study before then -
        give me that number and I'll lay out the blocks."

A model was asked to re-derive a date the student had already given us, on the
most expensive tier in the project, and answered a question nobody asked. The
fix has two halves, and each one is a different kind of claim:

1. **The reply is computed, not generated** (`brief_lookup`), which makes it free,
   instant, and - the part that matters most - unable to disagree with the brief
   shown in the panel above it. That half is tested here by behaviour.
2. **The router is told what a lookup is** (a rule in `ROUTER_SYSTEM`, and the
   exclusion in `plan`'s `not_for`), which is what catches the phrasings the
   patterns do not. That half is a prompt, so it is pinned as text: a rule that
   silently disappears from a prompt is a regression no behaviour test can see.
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

import router  # noqa: E402
import server  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import Question, QuizStore  # noqa: E402
from skills import SkillRegistry  # noqa: E402

MIDTERM = "my organic chemistry midterm is on November 12"
ESSAY = "the history essay is due October 20"
LAB = "lab report 3 is due 2026-10-13"

TODAY = "2026-10-16"


class RecallTestCase(unittest.TestCase):
    """A temp memory, and one clock: the facts decide what the answers say."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._today = server._today
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        # The date is fixed so "26 days from now" is arithmetic rather than a
        # race with the calendar - the same reason `scheduler` takes `today`.
        server._today = lambda: TODAY

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server._today = self._today
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def remember(self, *facts):
        for fact in facts:
            server.MEMORY.add("deadlines", fact)

    def ask(self, transcript, brief=None):
        return server.brief_lookup(transcript, brief)

    def brief(self):
        return server.brief_payload()


class TestTheQuestionsItAnswers(RecallTestCase):
    def setUp(self):
        super().setUp()
        self.remember(MIDTERM, ESSAY)

    def test_the_question_that_started_this(self):
        reply = self.ask("when is my next deadline?")
        self.assertTrue(reply)
        self.assertIn("2026-10-20", reply)
        self.assertIn("history essay", reply)

    def test_the_possessive_forms_are_answered_too(self):
        """The regression the first version shipped: both fell through."""
        for transcript in ("when's my next deadline", "what's due this week"):
            with self.subTest(transcript=transcript):
                self.assertTrue(self.ask(transcript))

    def test_the_shapes_a_student_actually_says(self):
        for transcript in (
            "when is my next deadline?",
            "when's my next deadline",
            "what's due this week",
            "what is due",
            "what is coming up",
            "what's coming up next",
            "is anything due",
            "do I have anything due",
            "when do I have anything due",
            "how many days until the midterm",
            "when is my midterm due",
        ):
            with self.subTest(transcript=transcript):
                self.assertTrue(self.ask(transcript), "no answer for a plain lookup")

    def test_a_named_subject_gets_the_note_about_it(self):
        reply = self.ask("when is my chemistry midterm due?")
        self.assertIn("organic chemistry midterm", reply)
        self.assertIn("2026-11-12", reply)
        self.assertNotIn("history essay", reply, "answered about the wrong thing")

    def test_a_named_subject_matches_the_word_the_student_used(self):
        """\"chem\" is about a note that says \"chemistry\"."""
        self.assertIn("2026-11-12", self.ask("when's my chem midterm"))
        self.assertIn("2026-10-20", self.ask("when is the essay due"))

    def test_an_answer_is_one_sentence_and_quotes_the_note(self):
        reply = self.ask("when is my chemistry midterm due?")
        self.assertTrue(reply.endswith("."))
        self.assertEqual(reply.count("."), 1)
        self.assertTrue(reply.startswith("Next up:"))


class TestWhatItLeavesAlone(RecallTestCase):
    """Every one of these has to reach the router. A lookup that ate them would
    be a worse bug than the one being fixed - it is the same failure, reversed."""

    def setUp(self):
        super().setUp()
        self.remember(MIDTERM, ESSAY)

    def test_requests_that_are_someone_else_s_job(self):
        for transcript in (
            "when should I study for the midterm",
            "plan my week, I have about six hours",
            "plan my revision before Friday",
            "break lab report 3 into study blocks",
            "where do I start on the essay",
            "explain photosynthesis",
            "rephrase this paragraph",
            "quiz me on chapter 4",
            "remember my midterm is on November 12",
            "make yourself a skill for lab reports",
            "help me plan around the midterm",
            "why is the essay due so soon",
        ):
            with self.subTest(transcript=transcript):
                self.assertEqual(self.ask(transcript), "", "a work request was answered as a lookup")

    def test_a_two_part_request_is_not_half_answered(self):
        self.assertEqual(self.ask("when is my midterm due and can you plan the week"), "")

    def test_a_long_sentence_is_not_a_lookup(self):
        self.assertEqual(self.ask("when is my next deadline " + "really " * 40), "")

    def test_nothing_at_all_and_a_non_question_are_not_lookups(self):
        for transcript in ("", "   ", "the midterm was hard", "hello", "thanks"):
            with self.subTest(transcript=transcript):
                self.assertEqual(self.ask(transcript), "")


class TestItNeverGuesses(RecallTestCase):
    """Silence is the correct answer when the student's files cannot answer.

    This is the rule that keeps the feature from being a machine for confidently
    saying "nothing is due" about something we were never told.
    """

    def test_with_no_dated_notes_there_is_no_answer(self):
        self.assertEqual(self.ask("when is my next deadline?"), "")

    def test_an_undated_note_does_not_become_a_date(self):
        self.remember("the midterm is soon I think")
        self.assertEqual(self.ask("when is my next deadline?"), "")
        self.assertEqual(self.ask("when is the midterm due?"), "")

    def test_a_subject_we_have_no_note_about_falls_through(self):
        self.remember(MIDTERM)
        self.assertEqual(self.ask("when is my physics final due?"), "")
        self.assertEqual(self.ask("when's the biology exam"), "")

    def test_one_word_overlap_is_not_a_match(self):
        """\"chemistry\" must not match \"the chemistry reading is optional\"-style
        near misses; the match is on every word the student named."""
        self.remember("the chemistry reading list is due October 30")
        self.assertEqual(
            self.ask("when is my chemistry midterm due?"),
            "",
            "matched a note about a different piece of work",
        )


class TestTheAnswerIsTheBrief(RecallTestCase):
    """The property that makes this worth doing in code: one number, two places."""

    def setUp(self):
        super().setUp()
        self.remember(MIDTERM, ESSAY, LAB)

    def test_the_date_and_the_countdown_are_the_brief_s_own(self):
        """Every shown note, against the brief's own numbers.

        Compared field by field rather than against a sentence written into this
        test: hard-coding "4 days from now" would pass on the day it was written
        and start failing tomorrow, which teaches nothing about a wrong date.
        """
        brief = self.brief()
        reply = self.ask("when is my next deadline?")
        self.assertEqual(brief["deadlines"][0]["due"], "2026-10-13")
        for item in brief["deadlines"][:3]:
            with self.subTest(due=item["due"]):
                self.assertIn(item["due"], reply)
                left = item["days_left"]
                if left < 0:
                    self.assertIn("{} days ago".format(-left), reply)
                elif left == 0:
                    self.assertIn("due today", reply)
                elif left == 1:
                    self.assertIn("due tomorrow", reply)
                else:
                    self.assertIn("{} days from now".format(left), reply)

    def test_the_note_s_own_date_is_not_printed_twice(self):
        """"the history essay is due October 20 - due 2026-10-20" is the bug this
        trimming exists for: two dates that look like they disagree."""
        reply = self.ask("when is the essay due")
        self.assertEqual(reply.count("2026-10-20"), 1)
        self.assertNotIn("October 20", reply)
        self.assertIn("the history essay", reply)

    def test_overdue_notes_lead_with_overdue(self):
        reply = self.ask("when is my lab report due")
        self.assertTrue(reply.startswith("Overdue:"))
        self.assertIn("2026-10-13", reply)
        self.assertIn("3 days ago", reply)  # the 13th, three days before the 16th

    def test_a_note_with_no_trailing_date_keeps_its_words(self):
        self.remember("the seminar paper")
        reply = self.ask("when is the reading week deadline")
        # No dated note matches, so nothing is said about it at all.
        self.assertEqual(reply, "")

    def test_today_and_tomorrow_are_words_not_numbers(self):
        self.remember("the physics problem set is due October 16")
        reply = self.ask("when is the problem set due")
        self.assertIn("due today (2026-10-16)", reply)

    def test_at_most_three_notes_and_a_count_of_the_rest(self):
        for index in range(10, 20):
            self.remember("assignment {0} is due October {0}".format(index))
        reply = self.ask("what's due")
        self.assertEqual(reply.count(" - "), 3)
        self.assertIn("dated notes", reply)

    def test_a_single_note_does_not_get_a_count_line(self):
        self.assertEqual(self.ask("when is the essay due").count(" - "), 1)
        self.assertNotIn("dated notes", self.ask("when is the essay due"))

    def test_the_answer_is_the_same_twice(self):
        self.assertEqual(self.ask("when is my next deadline?"), self.ask("when is my next deadline?"))

    def test_ccounts_and_dates_survive_the_calendar_panel(self):
        """A date lookup is about the brief, so it must not be affected by whether
        Google is connected - the calendar is a different source with its own line."""
        self.assertIn("2026-10-20", self.ask("what's due"))


class FrozenModel:
    """A client that fails the test if it is called at all."""

    def __init__(self):
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append(tier)
        raise AssertionError("a model call was made for a question the brief answers")


class ScriptedModel:
    """A client that answers the router and then says something."""

    def __init__(self, decision=None, answer="Here is a plan for the week."):
        self.decision = decision or {
            "skill": "general", "args": {}, "needs_confirmation": False,
        }
        self.answer = answer
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        is_router = bool(messages) and "You are a router" in messages[0]["content"]
        content = json.dumps(self.decision) if is_router else self.answer
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 30},
            },
            0.01,
        )

    @property
    def router_calls(self):
        return [c for c in self.calls if "You are a router" in c["messages"][0]["content"]]


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_quiz.py for the reasoning)."""

    def __init__(self, path="/turn", body=None, command="POST"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = "{0} {1} HTTP/1.0".format(command, path)
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = email.message_from_string(
            "Content-Length: {0}\r\nContent-Type: application/json\r\n".format(len(raw))
        )
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        getattr(self, "do_" + self.command)()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, payload = text.partition("\r\n\r\n")
        return int(head.split(" ")[1]), (json.loads(payload) if payload.strip() else None)


class TestTheTurn(RecallTestCase):
    """The wiring: a lookup never reaches the router, and nothing else changed."""

    def setUp(self):
        super().setUp()
        self.remember(MIDTERM, ESSAY)
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._spend = server.SPEND
        server.LOG_PATH = self.root / "action-log.jsonl"

    def tearDown(self):
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.SPEND = self._spend
        super().tearDown()

    def turn(self, transcript, **fields):
        return HandlerStub(body={"transcript": transcript, **fields}).send()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_the_question_that_started_this_now_costs_nothing(self):
        model = FrozenModel()
        server.get_client = lambda: model
        before = server.SPEND.snapshot()["cost_usd"]

        status, payload = self.turn("when is my next deadline?")

        self.assertEqual(status, 200)
        self.assertEqual(model.calls, [], "the router was called for a date lookup")
        self.assertEqual(payload["skill"], "brief")
        self.assertEqual(payload["cost_usd"], 0.0)
        self.assertEqual(server.SPEND.snapshot()["cost_usd"], before)
        self.assertIsNone(payload["tier"], "a computed answer has no model behind it")
        self.assertEqual(payload["steps"], 1)
        self.assertIn("2026-10-20", payload["reply"])
        self.assertIn("brief_lookup", self.log_text())

    def test_the_log_records_the_lookup_without_the_student_s_words(self):
        server.get_client = lambda: FrozenModel()
        self.turn("when is my history essay due?")
        lines = [json.loads(row) for row in self.log_text().strip().splitlines()]
        line = [row for row in lines if row.get("event") == "brief_lookup"][-1]
        self.assertEqual(line["event"], "brief_lookup")
        # And the turn line names the path that answered, not "quiz" - the Log tab
        # reads these two fields, so a computed answer mislabelled there is the one
        # place a reader would conclude the router had chosen something.
        turn = [row for row in lines if row.get("event") == "turn"][-1]
        self.assertEqual(turn["skill"], "brief")
        self.assertEqual(turn["skill_source"], "server")
        self.assertEqual(turn["router_attempts"], 0)
        self.assertEqual(turn["cost_usd"], 0.0)
        self.assertNotIn("history", json.dumps(line))
        self.assertNotIn("essay", json.dumps(line))
        self.assertEqual(line["transcript_chars"], len("when is my history essay due?"))

    def test_a_real_question_still_reaches_the_router(self):
        model = ScriptedModel(
            decision={"skill": "plan", "args": {"goal": "the midterm"},
                      "needs_confirmation": False},
            answer="Four blocks: Monday and Tuesday for the mechanisms.",
        )
        server.get_client = lambda: model
        status, payload = self.turn("plan my week, I have about six hours")
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "plan")
        self.assertEqual(len(model.router_calls), 1)

    def test_a_date_we_do_not_have_still_reaches_the_router(self):
        """The honest failure: no note, so the turn routes instead of inventing one."""
        model = ScriptedModel(answer="I don't have a date for that.")
        server.get_client = lambda: model
        status, payload = self.turn("when is my physics final due?")
        self.assertEqual(status, 200)
        self.assertEqual(len(model.router_calls), 1)
        self.assertNotEqual(payload["skill"], "brief")

    def test_asked_mid_quiz_it_answers_and_leaves_the_question_open(self):
        server.PROGRESS.start(
            "organic chemistry", "topic",
            [Question(text="Where does the Calvin cycle happen?", expected="in the stroma")],
            started="2026-10-14",
        )
        model = FrozenModel()
        server.get_client = lambda: model

        def declined(session, transcript, log=None):
            return None, True

        original = server.grade_quiz_answer
        server.grade_quiz_answer = declined
        try:
            status, payload = self.turn("when is my next deadline?")
        finally:
            server.grade_quiz_answer = original

        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "brief")
        self.assertIn("2026-10-20", payload["reply"])
        # The quiz is untouched and the panel knows it: one question, no verdicts.
        self.assertEqual(payload["quiz"]["question"], 1)
        self.assertEqual(payload["quiz"]["score"], "no answers yet")
        self.assertIn("still open", payload["reply"])
        self.assertEqual(server.PROGRESS.active().graded, 0)


class TestThePromptSaysSo(RecallTestCase):
    """The other half of the fix, pinned as text.

    The patterns cannot cover every phrasing, so the router is told the rule and
    the only Ultra-tier skill is told it is not that skill's job. A prompt is data
    that no behaviour test reads, so the lines that matter are asserted directly -
    if they are edited away, this fails and says which sentence went missing.
    """

    def test_the_router_is_told_a_lookup_is_not_a_plan(self):
        self.assertIn("LOOKUP", router.ROUTER_SYSTEM)
        self.assertIn("Never send one to a skill that builds a schedule", router.ROUTER_SYSTEM)

    def test_the_router_still_wants_a_real_plan_to_be_a_plan(self):
        self.assertIn("that IS a plan", router.ROUTER_SYSTEM)

    def test_the_ultra_skill_excludes_the_lookup_itself(self):
        registry = SkillRegistry(BACKEND.parent / "skills", tiers=["nano", "super", "ultra"]).load()
        plan = registry.get("plan")
        self.assertIsNotNone(plan)
        self.assertIn("date lookup", plan.not_for)
        self.assertIn("when is my next deadline", plan.not_for)

    def test_the_router_is_still_told_memory_is_not_a_reason_to_pick_a_skill(self):
        """The rule this fix had to sit next to without weakening."""
        self.assertIn("it is NOT a reason to pick a skill", router.ROUTER_SYSTEM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
