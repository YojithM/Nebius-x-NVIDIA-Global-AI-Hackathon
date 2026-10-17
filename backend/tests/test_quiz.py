#!/usr/bin/env python3
"""Tests for the quiz store and the graded quiz turn.

    python3 backend/tests/test_quiz.py

Four properties, each of which is a way a quiz feature goes wrong:

1. **The record on disk is real.** Questions, the student's own words and the
   verdicts survive a restart, because the Progress tab is a view over those
   files and the student is invited to open them.
2. **Scoring tells the truth.** A skipped question is not a wrong answer, an
   ungradeable one is not scored at all, and a wrong answer is a fact about the
   student we record rather than hide.
3. **Only an answer is graded.** A quiz turn starts only when the student asked
   to be quizzed, and while a quiz is open a turn is graded only when the router
   found no skill for it - so "explain that again" does not eat the question.
4. **The escapes are free.** "stop", "repeat that", "how am I doing" and
   "I don't know" make no model call at all, which is asserted rather than
   assumed: the scripted client records every call it receives.
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
from quiz import QuizStore, Question, clamp_count, slugify  # noqa: E402

QUESTIONS = [
    {"question": "What is the rate-determining step of an SN1 reaction?",
     "answer": "loss of the leaving group to form a carbocation"},
    {"question": "Where does the Calvin cycle happen?", "answer": "in the stroma"},
    {"question": "What does a buffer do?", "answer": "resists a change in pH"},
]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "progress"
        self.store = QuizStore(self.root).load()

    def tearDown(self):
        self.tmp.cleanup()

    def started(self, topic="organic chemistry", source="topic"):
        return self.store.start(
            topic, source, [Question(text=q["question"], expected=q["answer"]) for q in QUESTIONS],
            started="2026-10-14",
        )

    def reload(self):
        return QuizStore(self.root).load()


class TestWriting(StoreTestCase):
    def test_one_file_per_quiz_named_for_when_it_happened(self):
        session = self.started()
        self.assertTrue(session.path.exists())
        self.assertEqual(session.path.name, f"2026-10-14-organic-chemistry-{session.id}.md")
        self.assertEqual([p.name for p in self.root.glob("*.md")], [session.path.name])

    def test_the_file_reads_like_a_worked_quiz(self):
        """The student's promise: open it in any editor and it makes sense."""
        session = self.started()
        self.store.record(session, "losing the chloride", "correct")
        text = session.path.read_text(encoding="utf-8")
        self.assertIn("topic: organic chemistry", text)
        self.assertIn("# Quiz — organic chemistry", text)
        self.assertIn("1. What is the rate-determining step of an SN1 reaction?", text)
        self.assertIn("- expected: loss of the leaving group to form a carbocation", text)
        self.assertIn("- answer: losing the chloride", text)
        self.assertIn("- verdict: correct", text)
        # The questions nobody reached are still visible, and not invented.
        self.assertIn("- verdict: unanswered", text)

    def test_the_score_survives_a_reload(self):
        session = self.started()
        self.store.record(session, "losing the chloride", "correct")
        self.store.record(session, "in the mitochondria", "incorrect")
        reloaded = self.reload()
        found = reloaded.get(session.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.score_text(), "1 of 2")
        self.assertEqual(found.questions[0].answer, "losing the chloride")
        self.assertEqual(found.questions[2].verdict, "unanswered")

    def test_a_verdict_we_do_not_know_is_not_scored(self):
        """A hand-edited file must not be able to inflate or dock a score."""
        session = self.started()
        session.path.write_text(
            session.path.read_text().replace("verdict: unanswered", "verdict: maybe"),
            encoding="utf-8",
        )
        reloaded = self.reload().get(session.id)
        self.assertEqual(reloaded.questions[0].verdict, "unclear")
        self.assertEqual(reloaded.graded, 0)

    def test_no_temp_file_is_left_behind(self):
        session = self.started()
        self.store.record(session, "x", "correct")
        self.assertEqual([p.suffix for p in self.root.iterdir()], [".md"])

    def test_only_one_quiz_is_open_and_a_new_one_closes_the_old(self):
        first = self.started()
        self.store.record(first, "losing the chloride", "correct")
        second = self.started(topic="the Calvin cycle")
        self.assertEqual(first.status, "abandoned")
        self.assertEqual(second.status, "open")
        self.assertIs(self.store.active().id, second.id)
        # The old answers are kept, not deleted and not re-scored.
        self.assertEqual(self.reload().get(first.id).score_text(), "1 of 1")

    def test_an_empty_question_set_is_refused(self):
        from quiz import QuizError

        with self.assertRaises(QuizError):
            self.store.start("something", "topic", [])

    def test_an_unreadable_file_is_reported_and_the_rest_still_load(self):
        good = self.started()
        (self.root / "broken.md").write_text("---\nsession: \n---\nno questions\n", encoding="utf-8")
        reloaded = self.reload()
        self.assertEqual([s.id for s in reloaded.ordered()], [good.id])
        self.assertEqual(len(reloaded.errors), 1)

    def test_an_id_that_looks_like_a_number_survives_a_reload(self):
        """A real failure, caught only because a random id happened to hit it.

        Session ids are uuid4().hex[:8], which is hex and not always numeric
        text: "582e6104" is a perfectly good id, but float() reads it as
        582e6104 - scientific notation - and overflows to inf. The id then came
        back as "inf", so the quiz could not be found, answered or deleted.
        """
        session = self.started()
        session.path.write_text(
            session.path.read_text(encoding="utf-8").replace(
                session.id, "582e6104", 1
            ),
            encoding="utf-8",
        )
        session.path.rename(self.root / "2026-10-14-organic-chemistry-582e6104.md")

        reloaded = self.reload()
        self.assertEqual([s.id for s in reloaded.ordered()], ["582e6104"])
        self.assertIsNotNone(reloaded.get("582e6104"))
        self.assertEqual(reloaded.errors, [])

    def test_a_readme_in_the_progress_directory_is_not_a_quiz(self):
        self.started()
        (self.root / "README.md").write_text("# Your study record\n", encoding="utf-8")
        self.assertEqual(len(self.reload().ordered()), 1)


class TestScoringAndWeakness(StoreTestCase):
    def test_skipped_is_not_wrong_and_does_not_count(self):
        session = self.started()
        self.store.skip(session)
        self.assertEqual(session.questions[0].verdict, "skipped")
        self.assertEqual(session.graded, 0)
        self.assertEqual(session.score_text(), "no answers yet")
        # It is still the *next* question: a skip moves past it.
        self.assertEqual(session.index, 1)

    def test_unclear_is_recorded_but_never_scored(self):
        session = self.started()
        self.store.record(session, "something", "unclear")
        self.assertEqual(session.questions[0].verdict, "unclear")
        self.assertEqual(session.score_text(), "no answers yet")

    def test_a_verdict_that_is_not_a_verdict_becomes_unclear(self):
        session = self.started()
        self.store.record(session, "something", "probably-fine")
        self.assertEqual(session.questions[0].verdict, "unclear")

    def test_weakness_is_worst_first_and_counts_only_answers(self):
        first = self.started(topic="organic chemistry")
        self.store.record(first, "a", "correct")
        self.store.record(first, "b", "incorrect")
        self.store.finish(first)
        second = self.started(topic="thermodynamics")
        self.store.record(second, "a", "incorrect")
        self.store.record(second, "b", "incorrect")
        rows = self.store.weakness()
        self.assertEqual([r["topic"] for r in rows], ["thermodynamics", "organic chemistry"])
        self.assertEqual(rows[0]["accuracy"], 0.0)
        self.assertEqual(rows[1]["accuracy"], 0.5)
        self.assertEqual(rows[0]["asked"], 2)

    def test_a_topic_nobody_answered_is_not_called_a_weakness(self):
        self.started(topic="untouched")
        self.assertEqual(self.store.weakness(), [])

    def test_describe_carries_the_totals_the_tab_renders(self):
        session = self.started()
        self.store.record(session, "a", "correct")
        described = self.store.describe(include_questions=True)
        self.assertEqual(described["totals"]["quizzes"], 1)
        self.assertEqual(described["totals"]["correct"], 1)
        self.assertEqual(described["totals"]["questions"], 3)
        self.assertEqual(described["active"]["question"], 2)
        self.assertEqual(described["sessions"][0]["questions"][1]["verdict"], "unanswered")
        self.assertIn("directory", described)

    def test_deleting_one_quiz_leaves_the_others(self):
        first = self.started()
        second = self.started(topic="kept")
        self.assertTrue(self.store.delete(first.id))
        self.assertFalse((self.root / first.path.name).exists())
        self.assertEqual([s.topic for s in self.reload().ordered()], ["kept"])
        self.assertEqual(second.topic, "kept")

    def test_deleting_something_already_gone_is_false_not_an_error(self):
        session = self.started()
        self.store.delete(session.id)
        self.assertFalse(self.store.delete(session.id))

    def test_clear_removes_every_quiz_and_counts_them(self):
        self.started()
        self.started(topic="another")
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.ordered(), [])

    def test_slugs_and_counts_are_bounded(self):
        self.assertEqual(slugify("Organic Chemistry!"), "organic-chemistry")
        self.assertEqual(slugify("../../etc/passwd"), "etc-passwd")
        self.assertEqual(slugify("   "), "quiz")
        self.assertEqual(clamp_count(50), 10)
        self.assertEqual(clamp_count("3"), 3)
        self.assertEqual(clamp_count("lots"), 5)
        self.assertEqual(clamp_count(0), 5)


class ScriptedClient:
    """Answers the router with a fixed decision, then whatever the test says.

    Every call is recorded, which is how "this cost nothing" is checked rather
    than asserted from the source code.
    """

    def __init__(self, decision=None, replies=None):
        self.decision = decision or {
            "skill": "quiz",
            "args": {"topic": "organic chemistry", "count": 3},
            "needs_confirmation": False,
        }
        self.replies = list(replies or [])
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        if messages and "You are a router" in messages[0]["content"]:
            content = json.dumps(self.decision)
        elif self.replies:
            content = self.replies.pop(0)
        else:
            content = json.dumps({"questions": QUESTIONS})
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            0.01,
        )

    @property
    def non_router_calls(self):
        return [
            call
            for call in self.calls
            if "You are a router" not in call["messages"][0]["content"]
        ]

    @property
    def prompts(self):
        return [call["messages"][-1]["content"] for call in self.non_router_calls]


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_pages.py for the reasoning)."""

    def __init__(self, path="/turn", body=None, command="POST"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"{command} {path} HTTP/1.0"
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
        getattr(self, f"do_{self.command}")()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, payload = text.partition("\r\n\r\n")
        return int(head.split(" ")[1]), (json.loads(payload) if payload.strip() else None)


class TurnTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        self.client = ScriptedClient()
        server.get_client = lambda: self.client

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def turn(self, transcript, **fields):
        stub = HandlerStub(body={"transcript": transcript, **fields})
        return stub.send()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def seed_quiz(self):
        """An open quiz, as the student left it."""
        return server.PROGRESS.start(
            "organic chemistry",
            "topic",
            [Question(text=q["question"], expected=q["answer"]) for q in QUESTIONS],
            started="2026-10-14",
        )

    def answer(self, verdict="correct"):
        """Answer the open question directly, without a turn."""
        return server.PROGRESS.record(server.PROGRESS.active(), "an earlier answer", verdict)


class TestTheExplicitQuizGate(TurnTestCase):
    def test_an_explicit_request_starts_a_quiz(self):
        status, payload = self.turn("quiz me on chapter 4")
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "quiz")
        self.assertIn("Question 1 of 3", payload["reply"])
        self.assertEqual(payload["quiz"]["total"], 3)
        self.assertEqual(payload["quiz"]["question"], 1)
        self.assertEqual(len(server.PROGRESS.ordered()), 1)
        self.assertIn("quiz_started", self.log_text())

    def test_a_quiz_that_was_mentioned_is_not_a_request_to_be_quizzed(self):
        """The memory gate's lesson, applied to the other action skill."""
        for transcript in [
            "the quiz was hard",
            "don't quiz me on that",
            "I have a quiz on Friday",
        ]:
            with self.subTest(transcript=transcript):
                self.client.calls.clear()
                status, payload = self.turn(transcript)
                self.assertEqual(status, 200)
                self.assertNotEqual(payload["skill"], "quiz")
                self.assertEqual(server.PROGRESS.ordered(), [])
                self.assertIn("action_blocked", self.log_text())

    def test_with_nothing_to_quiz_on_it_asks_and_spends_nothing(self):
        self.client.decision = {"skill": "quiz", "args": {}, "needs_confirmation": False}
        status, payload = self.turn("quiz me")
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "quiz")
        self.assertEqual(self.client.non_router_calls, [])
        self.assertIn("what should I quiz you on", payload["reply"])
        self.assertEqual(server.PROGRESS.ordered(), [])

    def test_the_count_is_the_student_s_and_is_bounded(self):
        self.client.decision = {
            "skill": "quiz",
            "args": {"topic": "chapter 4", "count": 99},
            "needs_confirmation": False,
        }
        _, payload = self.turn("quiz me on chapter 4, ninety nine questions")
        self.assertEqual(payload["quiz"]["total"], 3)  # the scripted reply has three

    def test_a_quiz_on_the_page_gets_the_page_text(self):
        self.client.decision = {
            "skill": "quiz",
            "args": {"count": 3},
            "needs_confirmation": False,
        }
        status, payload = self.turn(
            "quiz me on this page", page_text="Photosynthesis happens in the chloroplast."
        )
        self.assertEqual(status, 200)
        self.assertIn("chloroplast", self.client.prompts[0])
        self.assertEqual(server.PROGRESS.active().source, "page")

    def test_a_quiz_with_no_page_uses_the_weak_topics_we_know(self):
        server.MEMORY.add("weak_topics", "I really struggle with stoichiometry")
        self.client.decision = {"skill": "quiz", "args": {}, "needs_confirmation": False}
        status, payload = self.turn("quiz me")
        self.assertEqual(status, 200)
        self.assertIn("stoichiometry", self.client.prompts[0])


class TestGradingATurn(TurnTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.seed_quiz()

    def grade(self, transcript, verdict="correct", feedback="That's the idea."):
        self.client.decision = {"skill": "general", "args": {}, "needs_confirmation": False}
        self.client.replies = [
            json.dumps({"verdict": verdict, "feedback": feedback})
        ]
        return self.turn(transcript)

    def test_an_answer_is_graded_and_the_next_question_follows(self):
        status, payload = self.grade("losing the chloride")
        self.assertEqual(status, 200)
        # The turn was the quiz's, not the router's: the grader went first and
        # decided, so there was no routing call at all. That is also why a graded
        # answer is cheaper than a normal turn.
        self.assertEqual(payload["skill"], "quiz")
        self.assertEqual(len(self.client.non_router_calls), 1)
        self.assertIn("That's the idea.", payload["reply"])
        self.assertIn("Question 2 of 3", payload["reply"])
        self.assertEqual(payload["quiz"]["question"], 2)
        self.assertEqual(payload["quiz"]["score"], "1 of 1")
        recorded = server.PROGRESS.get(self.session.id)
        self.assertEqual(recorded.questions[0].answer, "losing the chloride")
        self.assertEqual(recorded.questions[0].verdict, "correct")
        self.assertIn("quiz_answer", self.log_text())

    def test_the_student_s_words_are_what_gets_saved(self):
        self.grade("the leaving group falls off")
        recorded = server.PROGRESS.get(self.session.id)
        self.assertEqual(recorded.questions[0].answer, "the leaving group falls off")

    def test_the_last_answer_ends_the_quiz_with_the_score(self):
        self.answer("correct")
        self.answer("incorrect")
        _, payload = self.grade("it resists pH change")
        self.assertIn("That's the set", payload["reply"])
        self.assertIn("2 of 3", payload["reply"])
        self.assertIsNone(payload["quiz"])
        self.assertEqual(server.PROGRESS.get(self.session.id).status, "finished")
        self.assertIn("quiz_finished", self.log_text())

    def test_an_answer_that_is_not_an_attempt_is_answered_instead(self):
        """The grader's veto: a real question mid-quiz gets a real answer."""
        status, payload = self.grade("what is a ketone?", verdict="off_topic", feedback="")
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "general")
        self.assertIn("still open", payload["reply"])
        self.assertEqual(payload["quiz"]["question"], 1)
        # Nothing was scored, and the question is still waiting.
        self.assertEqual(server.PROGRESS.get(self.session.id).graded, 0)
        self.assertIn("quiz_off_topic", self.log_text())

    def test_an_ungradeable_answer_is_not_scored_either_way(self):
        _, payload = self.grade("the stroma", verdict="unclear", feedback="")
        self.assertIn("will not count", payload["reply"])
        self.assertEqual(server.PROGRESS.get(self.session.id).graded, 0)
        self.assertEqual(payload["quiz"]["score"], "no answers yet")

    def test_a_declined_request_still_reaches_the_skill_that_handles_it(self):
        """\"explain that\" is not an answer, so it must not consume the question."""
        self.client.decision = {
            "skill": "explain",
            "args": {"subject": "the SN1 reaction"},
            "needs_confirmation": False,
        }
        self.client.replies = [json.dumps({"verdict": "off_topic", "feedback": ""})]
        status, payload = self.turn("explain that again")
        self.assertEqual(status, 200)
        self.assertEqual(payload["skill"], "explain")
        self.assertEqual(payload["quiz"]["question"], 1)
        self.assertIn("still open", payload["reply"])
        self.assertEqual(server.PROGRESS.get(self.session.id).graded, 0)

    def test_a_new_quiz_closes_the_open_one_and_says_so(self):
        self.answer("correct")
        self.client.decision = {
            "skill": "quiz",
            "args": {"topic": "thermodynamics", "count": 3},
            "needs_confirmation": False,
        }
        # Asking for a different quiz mid-quiz costs one grading call that
        # declines before the router ever sees it. Predictable, and worth the
        # measurement: this is the one wasted call in the whole flow.
        self.client.replies = [
            json.dumps({"verdict": "off_topic", "feedback": ""}),
            json.dumps({"questions": QUESTIONS}),
        ]
        _, payload = self.turn("quiz me on thermodynamics")
        self.assertIn("You were 1 of 1 on organic chemistry", payload["reply"])
        self.assertEqual(server.PROGRESS.get(self.session.id).status, "abandoned")
        # The report has to describe the quiz that is actually open now, and the
        # reply must not claim the old question is still waiting.
        self.assertEqual(payload["quiz"]["topic"], "thermodynamics")
        self.assertEqual(payload["quiz"]["question"], 1)
        self.assertNotIn("still open", payload["reply"])


class TestTheEscapesCostNothing(TurnTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.seed_quiz()

    def escape(self, transcript):
        self.client.calls.clear()
        status, payload = self.turn(transcript)
        self.assertEqual(status, 200)
        # No router call and no grading call: the whole turn is code.
        self.assertEqual(self.client.calls, [], f"{transcript!r} made a model call")
        return payload

    def test_stop_ends_the_quiz_and_saves_it(self):
        payload = self.escape("stop")
        self.assertIn("Ending the quiz", payload["reply"])
        self.assertIsNone(payload["quiz"])
        self.assertEqual(server.PROGRESS.get(self.session.id).status, "abandoned")
        self.assertIn("quiz_escape", self.log_text())

    def test_stop_after_answers_is_a_finished_quiz_not_an_abandoned_one(self):
        self.answer("correct")
        self.escape("stop")
        self.assertEqual(server.PROGRESS.get(self.session.id).status, "finished")

    def test_repeat_reads_the_same_question_and_keeps_the_score(self):
        payload = self.escape("repeat that")
        self.assertIn("Question 1 of 3", payload["reply"])
        self.assertEqual(payload["quiz"]["question"], 1)
        self.assertEqual(self.session.graded, 0)

    def test_how_am_i_doing_reports_the_score(self):
        self.answer("correct")
        payload = self.escape("how am I doing")
        self.assertIn("1 of 1", payload["reply"])
        self.assertIn("2 questions to go", payload["reply"])

    def test_i_dont_know_is_wrong_reveals_the_answer_and_moves_on(self):
        payload = self.escape("I don't know")
        self.assertIn("loss of the leaving group", payload["reply"])
        # The revealed answer is spoken, so a doubled full stop is a real defect.
        self.assertNotIn("..", payload["reply"])
        self.assertIn("Question 2 of 3", payload["reply"])
        recorded = server.PROGRESS.get(self.session.id)
        self.assertEqual(recorded.questions[0].verdict, "incorrect")
        self.assertEqual(recorded.score_text(), "0 of 1")

    def test_skip_is_recorded_without_a_verdict(self):
        payload = self.escape("skip")
        self.assertIn("Question 2 of 3", payload["reply"])
        self.assertEqual(server.PROGRESS.get(self.session.id).graded, 0)

    def test_a_sentence_containing_stop_is_not_a_command(self):
        """The length bound is what keeps "stop" inside a sentence harmless."""
        self.client.decision = {"skill": "general", "args": {}, "needs_confirmation": False}
        self.client.replies = [json.dumps({"verdict": "off_topic", "feedback": ""})]
        status, payload = self.turn(
            "I want to stop by the library before my lab, and the answer is the stroma"
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.client.calls, "a long utterance should have been routed")
        self.assertEqual(payload["quiz"]["session"], self.session.id)

    def test_an_escape_with_no_quiz_open_is_just_a_normal_turn(self):
        server.PROGRESS.finish(self.session)
        self.client.decision = {"skill": "general", "args": {}, "needs_confirmation": False}
        self.client.replies = ["Fine."]
        _, payload = self.turn("stop")
        self.assertIsNone(payload["quiz"])
        self.assertTrue(self.client.calls)


class TestProgressEndpoint(TurnTestCase):
    """The HTTP contract the Progress tab renders.

    The question list is asserted here because the first build of the tab sent
    questions only when asked for them - `describe()` defaults to summaries - so
    the tab rendered blank with a TypeError while every store test passed. The
    panel is the interface; this is the test that covers it.
    """

    def get_progress(self):
        return HandlerStub(path="/progress", command="GET").send()

    def test_it_carries_the_questions_the_tab_renders(self):
        session = self.seed_quiz()
        self.answer("correct")
        status, payload = self.get_progress()
        self.assertEqual(status, 200)
        rendered = payload["sessions"][0]
        self.assertEqual(rendered["id"], session.id)
        self.assertEqual(rendered["questions"][0]["text"], QUESTIONS[0]["question"])
        self.assertEqual(rendered["questions"][0]["answer"], "an earlier answer")
        self.assertEqual(rendered["questions"][0]["verdict"], "correct")
        self.assertEqual(rendered["score"], "1 of 1")

    def test_declared_weakness_and_measured_weakness_are_separate(self):
        server.MEMORY.add("weak_topics", "I really struggle with stoichiometry")
        self.seed_quiz()
        self.answer("incorrect")
        _, payload = self.get_progress()
        self.assertEqual(payload["declared_weak"], ["I really struggle with stoichiometry"])
        self.assertEqual(payload["weakest"][0]["topic"], "organic chemistry")
        self.assertEqual(payload["weakest"][0]["accuracy"], 0.0)

    def test_an_open_quiz_is_reported_as_active(self):
        session = self.seed_quiz()
        _, payload = self.get_progress()
        self.assertEqual(payload["active"]["id"], session.id)
        self.assertEqual(payload["active"]["question"], 1)

    def test_deleting_one_quiz_answers_with_the_fresh_state(self):
        first = self.seed_quiz()
        self.turn("stop")
        second = self.seed_quiz()
        status, payload = HandlerStub(
            path=f"/progress?session={first.id}", command="DELETE"
        ).send()
        self.assertEqual(status, 200)
        self.assertTrue(payload["removed"])
        self.assertEqual([s["id"] for s in payload["sessions"]], [second.id])
        self.assertEqual(server.PROGRESS.ordered()[0].id, second.id)

    def test_deleting_a_quiz_that_is_already_gone_is_not_an_error(self):
        status, payload = HandlerStub(path="/progress?session=nope", command="DELETE").send()
        self.assertEqual(status, 200)
        self.assertFalse(payload["removed"])

    def test_clearing_everything_asks_for_the_whole_record(self):
        self.seed_quiz()
        status, payload = HandlerStub(path="/progress?all=1", command="DELETE").send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], 1)
        self.assertEqual(payload["sessions"], [])

    def test_a_delete_with_no_target_says_what_is_missing(self):
        status, payload = HandlerStub(path="/progress", command="DELETE").send()
        self.assertEqual(status, 400)
        self.assertIn("session", payload["error"])

    def test_health_reports_progress(self):
        self.seed_quiz()
        _, payload = HandlerStub(path="/health", command="GET").send()
        self.assertEqual(payload["step"], 13)
        self.assertEqual(payload["progress"]["quizzes"], 1)
        self.assertTrue(payload["progress"]["open_quiz"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
