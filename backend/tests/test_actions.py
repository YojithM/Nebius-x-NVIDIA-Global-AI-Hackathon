#!/usr/bin/env python3
"""Tests for action dispatch: the `remember` path, end to end but offline.

    python3 backend/tests/test_actions.py

Importing `server` does not start anything - `main()` is guarded - so this can
test the real dispatch table against a temp memory directory. Nothing here
touches the student's real memory, and nothing here makes a model call.
"""

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
from memory import MAX_FACT_CHARS, MemoryStore  # noqa: E402
from skills import SkillRegistry  # noqa: E402

TIERS = list(server.CONFIG["tiers"])


class ActionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Point the module-global store at a temp directory for the duration.
        self._original = server.MEMORY
        server.MEMORY = MemoryStore(self.root).load()
        self.registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()

    def tearDown(self):
        server.MEMORY = self._original
        self.tmp.cleanup()


class TestHandlerCoverage(ActionTestCase):
    """The bug class this catches: a skill ships whose action nothing handles."""

    def test_every_action_skill_has_a_registered_handler(self):
        for skill in self.registry.skills.values():
            if skill.action == "prompt":
                continue
            with self.subTest(skill=skill.name):
                self.assertIn(
                    skill.action,
                    server.ACTION_HANDLERS,
                    f"{skill.name} declares action {skill.action!r} but nothing handles it",
                )

    def test_remember_uses_the_remember_handler(self):
        skill = self.registry.get("remember")
        self.assertEqual(skill.action, "remember")
        self.assertIs(server.ACTION_HANDLERS["remember"], server.remembered_reply)


class TestTheExplicitRequestGate(ActionTestCase):
    """The explicit-only policy is enforced in code, not asked of the model.

    These cases were chosen from what actually went wrong on the live backend:
    the prompt-only version saved a plain statement the student never asked us to
    keep.
    """

    def test_explicit_requests_pass(self):
        for text in [
            "remember that my organic chemistry midterm is on November 12",
            "remember my advisor is Dr. Chen",
            "note that I hate group projects",
            "make a note that my lab is on Tuesdays",
            "keep in mind I'm bad at stoichiometry",
            "don't forget the midterm is on the 12th",
            "do not forget my student ID is 12345",
            "save this: my exam is at 9am",
            "jot down that the library closes at 8",
            "write that down for me",
        ]:
            with self.subTest(text=text):
                self.assertTrue(server.explicit_memory_request(text))

    def test_statements_and_questions_are_refused(self):
        """The exact utterance that got saved before the gate existed."""
        for text in [
            "my midterm is on November 12",
            "what is a ketone?",
            "break my lab report into study blocks",
            "my organic chemistry midterm is on November 12",
            "I have three deadlines this week",
        ]:
            with self.subTest(text=text):
                self.assertFalse(server.explicit_memory_request(text))

    def test_trigger_words_that_are_not_requests_are_refused(self):
        """The phrase 'I can't remember' contains 'remember' and asks for nothing."""
        for text in [
            "I can't remember this formula",
            "I cannot remember what a ketone is",
            "I don't remember the mechanism",
            "do you remember my midterm date?",
            "remember when we did this?",
        ]:
            with self.subTest(text=text):
                self.assertFalse(server.explicit_memory_request(text))

    def test_the_gate_is_what_blocks_the_action(self):
        skill = self.registry.get("remember")
        self.assertTrue(server._action_blocked(skill, "my midterm is on the 12th"))
        self.assertEqual(server._action_blocked(skill, "remember my midterm is on the 12th"), "")

    def test_a_prompt_skill_is_never_gated(self):
        """Only actions have preconditions; answering is always allowed."""
        skill = self.registry.get("study_breakdown")
        self.assertEqual(server._action_blocked(skill, "anything at all"), "")


class TestTopicCoercion(unittest.TestCase):
    def test_only_canonical_topics_survive(self):
        from memory import CANONICAL_TOPICS, canonical_topic

        for topic in CANONICAL_TOPICS:
            self.assertEqual(canonical_topic(topic), topic)

    def test_an_invented_topic_lands_in_general(self):
        """One file per utterance is how memory becomes a junk drawer."""
        from memory import canonical_topic

        for topic in ["stoichiometry", "midterm", "chem", "Organic Chemistry!", "../../etc", None]:
            with self.subTest(topic=topic):
                self.assertEqual(canonical_topic(topic), "general")


class TestFactText(unittest.TestCase):
    """A stored memory must stand alone months later.

    These are the two exact extractions the live backend produced: "Dr. Chen"
    from "don't forget my advisor is Dr. Chen", and "stoichiometry" from
    "remember that I really struggle with stoichiometry".
    """

    def test_a_fragment_of_the_utterance_is_replaced_by_the_utterance(self):
        cases = [
            ("don't forget my advisor is Dr. Chen", "Dr. Chen", "my advisor is Dr. Chen"),
            (
                "remember that I really struggle with stoichiometry",
                "stoichiometry",
                "I really struggle with stoichiometry",
            ),
            (
                "remember my organic chemistry midterm is on November 12",
                "my organic chemistry midterm",
                "my organic chemistry midterm is on November 12",
            ),
        ]
        for transcript, model_text, expected in cases:
            with self.subTest(transcript=transcript):
                self.assertEqual(server._fact_text(transcript, model_text), expected)

    def test_the_request_framing_is_stripped_even_without_a_fragment(self):
        self.assertEqual(
            server._fact_text("please remember that my lab is on Tuesdays", "my lab is on Tuesdays"),
            "my lab is on Tuesdays",
        )

    def test_a_richer_extraction_is_kept(self):
        """A resolved date or tidied sentence is not a substring, so it survives."""
        self.assertEqual(
            server._fact_text(
                "remember my midterm is next Thursday",
                "my midterm is on 2026-10-22",
            ),
            "my midterm is on 2026-10-22",
        )

    def test_a_rewritten_fact_is_kept(self):
        self.assertEqual(
            server._fact_text("note that I hate group projects", "prefers to work alone"),
            "prefers to work alone",
        )

    def test_a_complete_utterance_with_no_framing_still_works(self):
        self.assertEqual(server._fact_text("remember stoichiometry", "stoichiometry is hard"), "stoichiometry is hard")


class TestRememberedReply(ActionTestCase):
    def remember(self, topic, text, transcript="remember this pls"):
        return server.remembered_reply(
            self.registry.get("remember"), {"topic": topic, "text": text}, transcript
        )

    def test_a_fact_is_written_and_the_reply_names_the_topic(self):
        result = self.remember("deadlines", "organic chemistry midterm is on 2026-11-12")
        self.assertIn("deadlines", result["reply"])
        facts = server.MEMORY.facts("deadlines")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].text, "organic chemistry midterm is on 2026-11-12")

    def test_no_model_call_is_made_and_nothing_is_charged(self):
        result = self.remember("general", "something")
        self.assertIsNone(result["tier"])
        self.assertIsNone(result["model"])
        self.assertEqual(result["cost_usd"], 0.0)
        self.assertEqual(result["latency_ms"], 0)

    def test_the_saved_fact_is_reported_back_for_the_ui(self):
        result = self.remember("weak_topics", "stoichiometry")
        self.assertEqual(result["memory_saved"]["topic"], "weak_topics")
        self.assertEqual(result["memory_saved"]["text"], "stoichiometry")

    def test_a_bad_topic_from_the_model_still_saves_the_fact(self):
        """Losing a fact because the model picked an odd label is the worst trade."""
        result = self.remember("Organic Chemistry! 101", "midterm is on 2026-11-12")
        self.assertEqual(result["memory_saved"]["topic"], "general")
        self.assertEqual(server.MEMORY.facts("general")[0].text, "midterm is on 2026-11-12")

    def test_an_invented_topic_does_not_create_a_new_file(self):
        result = self.remember("stoichiometry", "bad at stoichiometry")
        self.assertEqual(result["memory_saved"]["topic"], "general")
        self.assertEqual([p.name for p in self.root.glob("*")], ["general.md"])

    def test_a_canonical_topic_is_kept_as_given(self):
        result = self.remember("weak_topics", "bad at stoichiometry")
        self.assertEqual(result["memory_saved"]["topic"], "weak_topics")

    def test_a_traversal_attempt_lands_in_general_instead_of_the_filesystem(self):
        result = self.remember("../../etc/passwd", "escape")
        self.assertEqual(result["memory_saved"]["topic"], "general")
        self.assertEqual([p.name for p in self.root.glob("*")], ["general.md"])

    def test_a_missing_topic_defaults_rather_than_failing(self):
        result = server.remembered_reply(self.registry.get("remember"), {"text": "no topic given"}, "")
        self.assertEqual(result["memory_saved"]["topic"], "general")

    def test_the_stored_fact_uses_the_students_own_words_over_a_fragment(self):
        result = self.remember(
            "people", "Dr. Chen", transcript="don't forget my advisor is Dr. Chen"
        )
        self.assertEqual(result["memory_saved"]["text"], "my advisor is Dr. Chen")

    def test_the_stored_fact_reaches_disk_as_the_full_sentence(self):
        self.remember("people", "Dr. Chen", transcript="don't forget my advisor is Dr. Chen")
        self.assertEqual(server.MEMORY.facts("people")[0].text, "my advisor is Dr. Chen")

    def test_overlong_text_is_trimmed_not_refused(self):
        result = self.remember("general", "x " * 900)
        saved = result["memory_saved"]
        self.assertIsNotNone(saved)
        self.assertLessEqual(len(saved["text"]), MAX_FACT_CHARS)
        self.assertEqual(len(server.MEMORY.facts("general")), 1)

    def test_empty_text_is_reported_gracefully_with_nothing_written(self):
        for text in ["", "   ", None]:
            with self.subTest(text=text):
                result = server.remembered_reply(
                    self.registry.get("remember"), {"topic": "general", "text": text}, ""
                )
                self.assertIsNone(result["memory_saved"])
                self.assertTrue(result["reply"], "the student must be told it did not save")
        self.assertEqual(server.MEMORY.count(), 0)


class TestPromptBlockWiring(ActionTestCase):
    def test_saved_memory_appears_in_the_prompt_block(self):
        server.remembered_reply(
            self.registry.get("remember"),
            {"topic": "deadlines", "text": "midterm is on 2026-11-12"},
        )
        block = server.MEMORY.prompt_block()
        self.assertIn("midterm is on 2026-11-12", block)

    def test_the_router_input_carries_memory_only_when_there_is_some(self):
        import json

        from router import build_user_message

        without = json.loads(build_user_message("hi", None, self.registry, ""))
        self.assertNotIn("memory", without)

        server.remembered_reply(
            self.registry.get("remember"), {"topic": "general", "text": "hates group work"}
        )
        with_memory = json.loads(
            build_user_message("hi", None, self.registry, server.MEMORY.prompt_block())
        )
        self.assertIn("hates group work", with_memory["memory"])


class TestMemoryRoundTripThroughTheApi(unittest.TestCase):
    """The same store the HTTP handlers use, exercised directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name)).load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_describe_lists_what_the_memory_tab_renders(self):
        self.store.add("classes", "taking organic chemistry", added="2026-10-14")
        described = self.store.describe()
        self.assertEqual(described["total"], 1)
        self.assertEqual(described["topics"][0]["topic"], "classes")
        self.assertEqual(described["topics"][0]["title"], "Classes")
        self.assertEqual(described["facts"][0]["text"], "taking organic chemistry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
