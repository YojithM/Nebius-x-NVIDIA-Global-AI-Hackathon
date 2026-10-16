#!/usr/bin/env python3
"""Tests for the router.

Hermetic: a scripted client stands in for Token Factory, so these run with no
API key and cost nothing. What is under test is our own logic - the JSON
contract, the validation, the single retry, the fallback, and the fact that the
model cannot talk its way out of a confirmation.

    python3 backend/tests/test_router.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from nemotron import load_config  # noqa: E402
from router import (  # noqa: E402
    ROUTER_TIER,
    build_user_message,
    extract_json_object,
    route,
    validate_decision,
)
from skills import SkillRegistry  # noqa: E402

CONFIG = load_config()
TIERS = list(CONFIG["tiers"])

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


def truncated_reply(prompt_tokens=100, completion_tokens=400):
    """What a reasoning model returns when its budget runs out mid-thought.

    Real capture: finish_reason='length', content=None, with the reasoning trace
    holding everything it never finished saying.
    """
    return (
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "We need to output JSON with skill name exactly one of...",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
        0.05,
    )


def model_reply(content, prompt_tokens=100, completion_tokens=20):
    return (
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
        0.05,
    )


class ScriptedClient:
    """Hands back queued replies and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append(
            {
                "tier": tier,
                "messages": [dict(m) for m in messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self.replies:
            return model_reply('{"skill": "general", "args": {}}')
        return self.replies.pop(0)


def registry_with(text=None, folder="rearrange_tabs"):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    if text is not None:
        path = root / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return SkillRegistry(root, TIERS).load(), tmp


# One shared empty directory for the tests that just need "no skills loaded",
# so they don't each spawn a TemporaryDirectory and leak a ResourceWarning into
# the test output.
_EMPTY_DIR = tempfile.TemporaryDirectory()
_EMPTY_REGISTRY = SkillRegistry(Path(_EMPTY_DIR.name), TIERS).load()


def empty_registry():
    return _EMPTY_REGISTRY


class TestJsonExtraction(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_code_fenced_object(self):
        self.assertEqual(
            extract_json_object('```json\n{"skill": "general"}\n```'),
            {"skill": "general"},
        )

    def test_object_with_trailing_prose(self):
        self.assertEqual(
            extract_json_object('Here you go: {"a": 1}. Hope that helps!'),
            {"a": 1},
        )

    def test_braces_inside_strings_do_not_confuse_it(self):
        text = '{"note": "use {curly} braces", "n": 2} trailing'
        self.assertEqual(extract_json_object(text), {"note": "use {curly} braces", "n": 2})

    def test_escaped_quote_inside_a_string(self):
        text = '{"note": "he said \\"hi\\"", "n": 3}'
        self.assertEqual(extract_json_object(text), {"note": 'he said "hi"', "n": 3})

    def test_nested_objects(self):
        self.assertEqual(extract_json_object('{"a": {"b": {"c": 1}}}'), {"a": {"b": {"c": 1}}})

    def test_pure_prose_returns_none(self):
        self.assertIsNone(extract_json_object("I think you should study chapter four."))

    def test_empty_returns_none(self):
        self.assertIsNone(extract_json_object(""))


class TestValidation(unittest.TestCase):
    def test_unknown_skill_is_rejected(self):
        clean, error = validate_decision({"skill": "make_coffee"}, empty_registry())
        self.assertIsNone(clean)
        self.assertIn("unknown skill", error)

    def test_general_is_always_allowed(self):
        clean, error = validate_decision(
            {"skill": "general", "args": {}, "needs_confirmation": False},
            empty_registry(),
        )
        self.assertIsNone(error)
        self.assertEqual(clean["skill"], "general")

    def test_needs_confirmation_must_be_a_real_bool(self):
        clean, error = validate_decision(
            {"skill": "general", "args": {}, "needs_confirmation": "true"},
            empty_registry(),
        )
        self.assertIsNone(clean)
        self.assertIn("true or false", error)

    def test_missing_skill_key_is_rejected(self):
        clean, error = validate_decision({"args": {}}, empty_registry())
        self.assertIsNone(clean)
        self.assertIn("missing required key", error)

    def test_args_must_be_an_object(self):
        clean, error = validate_decision(
            {"skill": "general", "args": "nope"}, empty_registry()
        )
        self.assertIsNone(clean)
        self.assertIn("'args' must be an object", error)

    def test_nested_general_args_are_rejected(self):
        clean, error = validate_decision(
            {"skill": "general", "args": {"deep": {"nested": 1}}}, empty_registry()
        )
        self.assertIsNone(clean)
        self.assertIn("simple values", error)

    def test_invented_argument_is_rejected(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            clean, error = validate_decision(
                {"skill": "rearrange_tabs", "args": {"group_by": "course", "colour": "blue"}},
                registry,
            )
            self.assertIsNone(clean)
            self.assertIn("no argument(s)", error)
            self.assertIn("colour", error)

    def test_wrong_argument_type_is_rejected(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            clean, error = validate_decision(
                {"skill": "rearrange_tabs", "args": {"group_by": 5}}, registry
            )
            self.assertIsNone(clean)
            self.assertIn("should be a string", error)

    def test_missing_required_argument_is_rejected(self):
        registry = SkillRegistry(Path(__file__).resolve().parent.parent.parent / "skills", TIERS).load()
        clean, error = validate_decision({"skill": "study_breakdown", "args": {}}, registry)
        self.assertIsNone(clean)
        self.assertIn("requires argument", error)

    def test_boolean_is_not_accepted_as_an_integer(self):
        clean, error = validate_decision(
            {"skill": "general", "args": {}, "needs_confirmation": True}, empty_registry()
        )
        self.assertIsNone(error)


class TestRouting(unittest.TestCase):
    def test_valid_decision_is_used_on_the_first_attempt(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            client = ScriptedClient(
                model_reply('{"skill": "rearrange_tabs", "args": {"group_by": "course"}, "needs_confirmation": false}')
            )
            decision = route(client, CONFIG, registry, "tidy up my tabs")
            self.assertEqual(decision.skill, "rearrange_tabs")
            self.assertEqual(decision.args, {"group_by": "course"})
            self.assertEqual(decision.source, "model")
            self.assertEqual(decision.attempts, 1)
            self.assertEqual(len(client.calls), 1)

    def test_routing_uses_the_nano_tier(self):
        client = ScriptedClient(model_reply('{"skill": "general", "args": {}}'))
        decision = route(client, CONFIG, empty_registry(), "hello")
        self.assertEqual(client.calls[0]["tier"], ROUTER_TIER)
        self.assertEqual(decision.model, CONFIG["tiers"]["nano"]["model"])

    def test_routing_is_deterministic_temperature_zero(self):
        client = ScriptedClient(model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, empty_registry(), "hello")
        self.assertEqual(client.calls[0]["temperature"], 0)

    def test_bad_json_then_good_json_retries_once(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            client = ScriptedClient(
                model_reply("I think you want to tidy your tabs!"),
                model_reply('{"skill": "rearrange_tabs", "args": {}, "needs_confirmation": true}'),
            )
            decision = route(client, CONFIG, registry, "tidy up my tabs")
            self.assertEqual(decision.skill, "rearrange_tabs")
            self.assertEqual(decision.attempts, 2)
            self.assertEqual(decision.source, "model")
            # the corrective message must say what was wrong
            corrective = client.calls[1]["messages"][-1]["content"]
            self.assertIn("was invalid", corrective)
            self.assertIn("not a JSON object", corrective)

    def test_retry_tells_the_model_the_unknown_skill_name(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            client = ScriptedClient(
                model_reply('{"skill": "make_coffee", "args": {}}'),
                model_reply('{"skill": "general", "args": {}}'),
            )
            route(client, CONFIG, registry, "make me coffee")
            corrective = client.calls[1]["messages"][-1]["content"]
            self.assertIn("make_coffee", corrective)

    def test_truncated_reply_is_reported_as_truncation_not_nonsense(self):
        client = ScriptedClient(truncated_reply(), model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, empty_registry(), "hi")
        corrective = client.calls[1]["messages"][-1]["content"]
        self.assertIn("ran out of tokens", corrective)

    def test_retry_gets_a_bigger_token_budget_than_the_first_attempt(self):
        """Retrying a truncated reply at the same size just repeats the failure."""
        client = ScriptedClient(truncated_reply(), model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, empty_registry(), "hi")
        self.assertGreater(client.calls[1]["max_tokens"], client.calls[0]["max_tokens"])

    def test_two_truncations_fall_back_with_a_true_explanation(self):
        client = ScriptedClient(truncated_reply(), truncated_reply())
        decision = route(client, CONFIG, empty_registry(), "hi")
        self.assertEqual(decision.skill, "general")
        self.assertEqual(decision.source, "fallback")
        self.assertIn("ran out of tokens", decision.validation_error)

    def test_two_failures_fall_back_to_general_instead_of_erroring(self):
        client = ScriptedClient(
            model_reply("not json at all"),
            model_reply("still not json"),
        )
        decision = route(client, CONFIG, empty_registry(), "what is due this week")
        self.assertEqual(decision.skill, "general")
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.attempts, 2)
        self.assertEqual(decision.args, {"request": "what is due this week"})
        self.assertIn("not a JSON object", decision.validation_error)

    def test_fallback_still_carries_the_original_request(self):
        client = ScriptedClient(model_reply("??"), model_reply("??"))
        decision = route(client, CONFIG, empty_registry(), "explain entropy")
        self.assertEqual(decision.args["request"], "explain entropy")

    def test_cost_accumulates_across_both_attempts(self):
        client = ScriptedClient(model_reply("bad"), model_reply("also bad"))
        decision = route(client, CONFIG, empty_registry(), "hi")
        per_attempt = (100 * 0.06 + 20 * 0.24) / 1_000_000
        self.assertAlmostEqual(decision.cost_usd, per_attempt * 2, places=12)
        self.assertGreater(decision.cost_usd, 0)

    def test_model_cannot_de_escalate_a_confirmation_skill(self):
        """The safety property: the flag is ORed with the skill's own declaration."""
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            client = ScriptedClient(
                model_reply('{"skill": "rearrange_tabs", "args": {}, "needs_confirmation": false}')
            )
            decision = route(client, CONFIG, registry, "tidy my tabs")
            self.assertEqual(decision.skill, "rearrange_tabs")
            self.assertTrue(
                decision.needs_confirmation,
                "a requires_confirmation skill must be confirmed even if the model says false",
            )

    def test_general_requests_still_honour_the_model_flag(self):
        """No skill declaration exists for general, so the flag is all we have."""
        client = ScriptedClient(
            model_reply('{"skill": "general", "args": {}, "needs_confirmation": true}')
        )
        self.assertTrue(route(client, CONFIG, empty_registry(), "email my professor").needs_confirmation)

    def test_over_eager_model_flag_does_not_block_a_safe_skill(self):
        """A phantom confirmation prompt trains the student to approve blindly."""
        registry, tmp = registry_with(CONFIRMING_SKILL.replace("requires_confirmation: true", "requires_confirmation: false"))
        with tmp:
            client = ScriptedClient(
                model_reply('{"skill": "rearrange_tabs", "args": {}, "needs_confirmation": true}')
            )
            decision = route(client, CONFIG, registry, "group my tabs")
            self.assertEqual(decision.skill, "rearrange_tabs")
            self.assertFalse(
                decision.needs_confirmation,
                "the skill file is authoritative; the model's flag must be ignored for named skills",
            )

    def test_skill_declaration_still_forces_confirmation(self):
        registry, tmp = registry_with(CONFIRMING_SKILL)
        with tmp:
            client = ScriptedClient(model_reply('{"skill": "rearrange_tabs", "args": {}}'))
            self.assertTrue(route(client, CONFIG, registry, "group my tabs").needs_confirmation)

    def test_model_may_escalate_a_confirmation(self):
        client = ScriptedClient(
            model_reply('{"skill": "general", "args": {}, "needs_confirmation": true}')
        )
        decision = route(client, CONFIG, empty_registry(), "send an email to my professor")
        self.assertTrue(decision.needs_confirmation)


class TestEnumArguments(unittest.TestCase):
    """A closed vocabulary must be enforced, not merely suggested."""

    def test_a_value_outside_the_enum_is_rejected_with_the_options_named(self):
        skill = _enum_skill()
        clean, error = validate_decision(
            {"skill": "demo_enum", "args": {"topic": "stoichiometry"}, "needs_confirmation": False},
            _enum_registry(skill),
        )
        self.assertIsNone(clean)
        self.assertIn("must be one of", error)
        # Naming the options is what makes the corrective retry land.
        self.assertIn("deadlines", error)

    def test_a_value_inside_the_enum_is_accepted(self):
        clean, error = validate_decision(
            {"skill": "demo_enum", "args": {"topic": "deadlines"}, "needs_confirmation": False},
            _enum_registry(_enum_skill()),
        )
        self.assertIsNone(error)
        self.assertEqual(clean["args"]["topic"], "deadlines")

    def test_the_enum_is_visible_in_the_routing_card(self):
        card = _enum_registry(_enum_skill()).routing_cards()[0]
        self.assertEqual(
            card["args"]["topic"], "one of: deadlines | classes | general (required)"
        )


def _enum_skill():
    return """---
name: demo_enum
description: "stores things"
tier: nano
requires_confirmation: false
when_to_use: "when the student asks to store something"
not_for: "questions"
---

## Input

```json
{
  "topic": {"type": "string", "required": true, "enum": ["deadlines", "classes", "general"]}
}
```

## Prompt

```
Store {topic}.
```
"""


def _enum_registry(text):
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    path = root / "demo_enum" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return SkillRegistry(root, TIERS).load()


class TestRouterInput(unittest.TestCase):
    def test_input_includes_transcript_page_title_and_skills(self):
        registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()
        payload = json.loads(build_user_message("what's due", "Chem 101 — Syllabus", registry))
        self.assertEqual(payload["transcript"], "what's due")
        self.assertEqual(payload["page_title"], "Chem 101 — Syllabus")
        # Look the skill up by name rather than by index: index 0 silently
        # changed meaning the moment a second skill was added to the library.
        names = [card["name"] for card in payload["skills"]]
        self.assertIn("study_breakdown", names)
        self.assertEqual(names, sorted(names), "routing cards must be in a stable order")

    def test_shipped_remember_skill_declares_its_topic_enum(self):
        """Otherwise the router invents a new topic file on every utterance."""
        registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()
        payload = json.loads(build_user_message("remember x", None, registry))
        card = next(c for c in payload["skills"] if c["name"] == "remember")
        self.assertIn("deadlines", card["args"]["topic"])
        self.assertIn("general", card["args"]["topic"])

    def test_missing_page_title_becomes_null_not_a_missing_key(self):
        payload = json.loads(build_user_message("hi", None, empty_registry()))
        self.assertIsNone(payload["page_title"])

    def test_router_prompt_treats_general_as_a_real_answer(self):
        """The prompt must actively steer away from over-applying the one skill."""
        client = ScriptedClient(model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, empty_registry(), "what is a ketone")
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("not_for", system)
        self.assertIn("not a failure", system)
        self.assertIn("worse than picking none", system)

    def test_not_for_reaches_the_router(self):
        registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()
        client = ScriptedClient(model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, registry, "hi")
        sent = client.calls[0]["messages"][1]["content"]
        self.assertIn("not_for", sent)
        self.assertIn("factual answer", sent)

    def test_a_page_read_is_a_flag_in_the_input_not_the_page_itself(self):
        """The router picks who handles it; the skill is the only one shown text."""
        registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()
        without = json.loads(build_user_message("explain entropy", None, registry))
        self.assertNotIn("page_read", without)
        with_page = json.loads(
            build_user_message("explain this", None, registry, "", page_read=True)
        )
        self.assertIs(with_page["page_read"], True)
        # The card tells the router which skills can work from the page, so it can
        # prefer one, without any page text travelling through the router.
        explain = next(c for c in with_page["skills"] if c["name"] == "explain")
        self.assertEqual(explain["context"], ["page"])

    def test_router_prompt_lists_only_real_skill_names(self):
        registry = SkillRegistry(
            Path(__file__).resolve().parent.parent.parent / "skills", TIERS
        ).load()
        client = ScriptedClient(model_reply('{"skill": "general", "args": {}}'))
        route(client, CONFIG, registry, "hi")
        sent = client.calls[0]["messages"][1]["content"]
        self.assertIn("study_breakdown", sent)
        self.assertIn("Never invent a skill name", client.calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
