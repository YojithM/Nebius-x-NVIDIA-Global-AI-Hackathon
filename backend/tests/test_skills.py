#!/usr/bin/env python3
"""Tests for the frontmatter parser and the skills registry.

    python3 backend/tests/test_skills.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontmatter import FrontmatterError, parse  # noqa: E402
from skills import SkillError, SkillRegistry, load_skill  # noqa: E402

TIERS = ["nano", "super", "ultra"]

VALID_SKILL = """---
name: demo_skill
description: "Does a demo thing."
version: 0.1.0
tier: super
requires_confirmation: false
when_to_use: "when the student wants a demo"
tools: [search]
metadata:
  hermes:
    tags: [demo, test]
---

# demo_skill

## When to Use

Use it for demos.

## Input

```json
{
  "thing": {"type": "string", "required": true, "description": "the thing"},
  "times": {"type": "integer", "default": 2, "description": "how many"}
}
```

## Prompt

```
Do {thing} {times} times, in plain spoken prose.
```
"""


def write_skill(root: Path, text: str, folder: str = "demo_skill") -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestFrontmatter(unittest.TestCase):
    def test_parses_scalars_lists_and_nesting(self):
        meta, body = parse(
            "---\n"
            "name: x\n"
            "count: 12\n"
            "ratio: 1.5\n"
            "flag: true\n"
            "nothing: null\n"
            "quoted: \"a: b\"\n"
            "inline: [a, b, c]\n"
            "block:\n"
            "  - one\n"
            "  - two\n"
            "nested:\n"
            "  child:\n"
            "    tags: [t1, t2]\n"
            "---\nBody here\n"
        )
        self.assertEqual(meta["name"], "x")
        self.assertEqual(meta["count"], 12)
        self.assertAlmostEqual(meta["ratio"], 1.5)
        self.assertIs(meta["flag"], True)
        self.assertIsNone(meta["nothing"])
        self.assertEqual(meta["quoted"], "a: b")
        self.assertEqual(meta["inline"], ["a", "b", "c"])
        self.assertEqual(meta["block"], ["one", "two"])
        self.assertEqual(meta["nested"]["child"]["tags"], ["t1", "t2"])
        self.assertEqual(body.strip(), "Body here")

    def test_skips_comments_but_keeps_hash_inside_block_strings(self):
        meta, _ = parse(
            "---\n"
            "a: 1\n"
            "# a comment\n"
            "b: 2\n"
            "note: |\n"
            "  # this hash is content\n"
            "  second line\n"
            "---\n"
        )
        self.assertEqual(meta["a"], 1)
        self.assertEqual(meta["b"], 2)
        self.assertIn("# this hash is content", meta["note"])

    def test_a_value_that_only_looks_numeric_stays_text(self):
        """Generated ids are hex, and not every hex id is non-numeric text.

        "582e6104" is scientific notation to float() and overflows to inf, and
        "01234567" is the int 1234567 - either way an id comes back changed and
        can no longer find its own file.
        """
        meta, _ = parse(
            "---\n"
            "session: 582e6104\n"
            "fact: 01234567\n"
            "version: 0.1.0\n"
            "count: 12\n"
            "ratio: 1.5\n"
            "---\n"
        )
        self.assertEqual(meta["session"], "582e6104")
        self.assertEqual(meta["fact"], "01234567")
        self.assertEqual(meta["version"], "0.1.0")
        # Real numbers are still numbers.
        self.assertEqual(meta["count"], 12)
        self.assertEqual(meta["ratio"], 1.5)

    def test_no_frontmatter_is_not_an_error(self):
        meta, body = parse("# Just a document\n")
        self.assertEqual(meta, {})
        self.assertIn("Just a document", body)

    def test_body_is_not_parsed_as_frontmatter(self):
        # A '---' horizontal rule in the body must not be mistaken for a key.
        meta, body = parse("---\nname: x\n---\nIntro\n\n---\n\nAfter\n")
        self.assertEqual(meta, {"name": "x"})
        self.assertIn("After", body)

    def test_fails_loudly_on_unclosed_frontmatter(self):
        with self.assertRaises(FrontmatterError):
            parse("---\nname: x\nbody without closing\n")

    def test_fails_loudly_on_bad_indentation(self):
        with self.assertRaises(FrontmatterError):
            parse("---\nname: x\n    stray: y\n---\n")

    def test_fails_loudly_on_mixed_list_and_keys(self):
        with self.assertRaises(FrontmatterError):
            parse("---\na: 1\n  - b\n---\n")


class TestSkillLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_a_valid_skill(self):
        skill = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        self.assertEqual(skill.name, "demo_skill")
        self.assertEqual(skill.tier, "super")
        self.assertFalse(skill.requires_confirmation)
        self.assertEqual(skill.tools, ["search"])
        self.assertEqual(skill.tags, ["demo", "test"])
        self.assertEqual(skill.required_fields, ["thing"])
        self.assertEqual(skill.input_schema["times"].default, 2)
        self.assertIn("Do {thing}", skill.prompt_template)

    def test_optional_argument_uses_its_default(self):
        skill = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        rendered = skill.render_prompt({"thing": "a ketone"})
        self.assertEqual(rendered, "Do a ketone 2 times, in plain spoken prose.")

    def test_supplied_argument_overrides_default(self):
        skill = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        self.assertIn("5 times", skill.render_prompt({"thing": "x", "times": 5}))

    def test_folder_and_name_must_match(self):
        path = write_skill(self.root, VALID_SKILL, folder="wrong_folder")
        with self.assertRaisesRegex(SkillError, "must match"):
            load_skill(path, TIERS)

    def test_unknown_tier_is_rejected(self):
        with self.assertRaisesRegex(SkillError, "not a configured tier"):
            load_skill(write_skill(self.root, VALID_SKILL.replace("tier: super", "tier: gigantic")), TIERS)

    def test_required_plus_default_is_rejected(self):
        bad = VALID_SKILL.replace('"type": "integer", "default": 2', '"type": "integer", "default": 2, "required": true')
        with self.assertRaisesRegex(SkillError, "contradict"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_default_of_the_wrong_type_is_rejected(self):
        bad = VALID_SKILL.replace('"integer", "default": 2', '"integer", "default": "two"')
        with self.assertRaisesRegex(SkillError, "not a"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_unsupported_field_type_is_rejected(self):
        bad = VALID_SKILL.replace('"type": "string", "required": true', '"type": "list", "required": true')
        with self.assertRaisesRegex(SkillError, "unsupported type"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_missing_prompt_section_is_rejected(self):
        bad = VALID_SKILL.split("## Prompt")[0]
        with self.assertRaisesRegex(SkillError, "## Prompt"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_missing_input_section_is_rejected(self):
        bad = VALID_SKILL.replace("## Input", "## Args")
        with self.assertRaisesRegex(SkillError, "## Input"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_non_json_input_block_is_rejected(self):
        bad = VALID_SKILL.replace('"thing": {', "thing: {").replace('"type": "string"', "type: string")
        with self.assertRaisesRegex(SkillError, "not valid JSON"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_requires_confirmation_must_be_a_bool(self):
        bad = VALID_SKILL.replace("requires_confirmation: false", 'requires_confirmation: "false"')
        with self.assertRaisesRegex(SkillError, "true or false"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_action_defaults_to_prompt(self):
        skill = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        self.assertEqual(skill.action, "prompt")

    def test_an_unknown_action_is_rejected_rather_than_ignored(self):
        """A typo'd action would otherwise ship as a prompt skill that never runs."""
        bad = VALID_SKILL.replace("tier: super", "tier: super\naction: remeber")
        with self.assertRaisesRegex(SkillError, "not one of"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_an_action_skill_needs_no_prompt_section(self):
        """Its behaviour is code, so demanding a template nothing renders is a lie."""
        body = VALID_SKILL.split("## Prompt")[0].replace(
            "tier: super", "tier: super\naction: remember"
        )
        skill = load_skill(write_skill(self.root, body), TIERS)
        self.assertEqual(skill.action, "remember")
        self.assertEqual(skill.prompt_template, "")

    def test_a_prompt_skill_still_needs_its_prompt(self):
        body = VALID_SKILL.split("## Prompt")[0]
        with self.assertRaisesRegex(SkillError, "## Prompt"):
            load_skill(write_skill(self.root, body), TIERS)

    def test_an_enum_reaches_the_router_and_says_its_options(self):
        """A description is never sent to the router; an enum is."""
        text = VALID_SKILL.replace(
            '"type": "string", "required": true',
            '"type": "string", "required": true, "enum": ["a", "b"]',
        )
        write_skill(self.root, text, folder="demo_skill")
        card = SkillRegistry(self.root, TIERS).load().routing_cards()[0]
        self.assertEqual(card["args"]["thing"], "one of: a | b (required)")

    def test_a_field_without_an_enum_renders_exactly_as_before(self):
        write_skill(self.root, VALID_SKILL, folder="demo_skill")
        card = SkillRegistry(self.root, TIERS).load().routing_cards()[0]
        self.assertEqual(card["args"]["thing"], "string (required)")

    def test_an_enum_with_a_value_of_the_wrong_type_is_rejected(self):
        bad = VALID_SKILL.replace(
            '"type": "integer", "default": 2',
            '"type": "integer", "default": 2, "enum": ["two"]',
        )
        with self.assertRaisesRegex(SkillError, "not a integer"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_a_default_outside_its_own_enum_is_rejected(self):
        bad = VALID_SKILL.replace(
            '"type": "string", "required": true',
            '"type": "string", "required": true, "enum": ["a", "b"]',
        ).replace('"times": {"type": "integer", "default": 2', '"times": {"type": "integer", "default": 2, "enum": [1, 3]')
        with self.assertRaisesRegex(SkillError, "not in its enum"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_an_empty_enum_is_rejected(self):
        bad = VALID_SKILL.replace(
            '"type": "string", "required": true',
            '"type": "string", "required": true, "enum": []',
        )
        with self.assertRaisesRegex(SkillError, "non-empty list"):
            load_skill(write_skill(self.root, bad), TIERS)

    def test_describe_exposes_the_enum_for_the_skills_endpoint(self):
        text = VALID_SKILL.replace(
            '"type": "string", "required": true',
            '"type": "string", "required": true, "enum": ["a", "b"]',
        )
        write_skill(self.root, text, folder="demo_skill")
        described = SkillRegistry(self.root, TIERS).load().describe()[0]
        self.assertEqual(described["args"]["thing"]["enum"], ["a", "b"])
        self.assertNotIn("enum", described["args"]["times"])

    def test_action_is_not_leaked_into_the_routing_card(self):
        """The card is paid for on every request; action is not a routing signal."""
        text = VALID_SKILL.replace("tier: super", "tier: super\naction: remember")
        write_skill(self.root, text, folder="demo_skill")
        card = SkillRegistry(self.root, TIERS).load().routing_cards()[0]
        self.assertNotIn("action", card)

    def test_missing_when_to_use_is_rejected(self):
        bad = "\n".join(
            line for line in VALID_SKILL.splitlines() if not line.startswith("when_to_use:")
        )
        with self.assertRaisesRegex(SkillError, "when_to_use"):
            load_skill(write_skill(self.root, bad), TIERS)


class TestDeclaredContext(unittest.TestCase):
    """A skill is shown exactly what it declared, and a typo is a load error.

    This is the boundary that keeps the page the student asked us to read from
    drifting into skills that never asked for it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def with_context(self, value):
        text = VALID_SKILL.replace("tier: super", f"tier: super\ncontext: {value}")
        return load_skill(write_skill(self.root, text), TIERS)

    def test_context_is_empty_by_default(self):
        skill = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        self.assertEqual(skill.context, ())

    def test_a_declared_list_is_kept_in_order(self):
        self.assertEqual(self.with_context("[page, previous]").context, ("page", "previous"))

    def test_a_single_source_without_brackets_works(self):
        self.assertEqual(self.with_context("page").context, ("page",))

    def test_duplicates_are_collapsed(self):
        self.assertEqual(self.with_context("[page, page]").context, ("page",))

    def test_an_unknown_source_is_rejected_rather_than_ignored(self):
        """Silently meaning \"gets nothing\" looks exactly like a working skill."""
        with self.assertRaisesRegex(SkillError, "not one of"):
            self.with_context("[screenshot]")

    def test_a_non_prompt_action_may_not_declare_context(self):
        """An action handler is never handed context, so declaring it would lie."""
        text = VALID_SKILL.replace(
            "tier: super", "tier: super\naction: remember\ncontext: [page]"
        )
        with self.assertRaisesRegex(SkillError, "not given context"):
            load_skill(write_skill(self.root, text), TIERS)

    def test_the_card_carries_context_but_only_when_declared(self):
        with_context = self.with_context("[page, memory]")
        self.assertEqual(with_context.routing_card()["context"], ["page", "memory"])
        without = load_skill(write_skill(self.root, VALID_SKILL), TIERS)
        self.assertNotIn("context", without.routing_card())

    def test_the_skills_endpoint_reports_context(self):
        skill = self.with_context("[previous]")
        self.assertEqual(skill.context, ("previous",))


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_broken_skill_does_not_take_down_the_rest(self):
        write_skill(self.root, VALID_SKILL, folder="demo_skill")
        write_skill(self.root, "---\nname: broken\n---\nno sections\n", folder="broken")
        registry = SkillRegistry(self.root, TIERS).load()

        self.assertEqual(registry.names(), ["demo_skill"])
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("broken", registry.errors[0])

    def test_duplicate_names_are_reported_not_silently_merged(self):
        write_skill(self.root, VALID_SKILL, folder="demo_skill")
        nested = self.root / "other" / "demo_skill" / "SKILL.md"
        nested.parent.mkdir(parents=True)
        nested.write_text(VALID_SKILL, encoding="utf-8")

        registry = SkillRegistry(self.root, TIERS).load()
        self.assertEqual(len(registry.skills), 1)
        self.assertTrue(any("duplicate" in e for e in registry.errors))

    def test_not_for_is_carried_into_the_routing_card(self):
        """Without the negative case the router over-applies whatever exists."""
        text = VALID_SKILL.replace(
            'when_to_use: "when the student wants a demo"',
            'when_to_use: "when the student wants a demo"\nnot_for: "factual questions"',
        )
        write_skill(self.root, text, folder="demo_skill")
        registry = SkillRegistry(self.root, TIERS).load()
        card = registry.routing_cards()[0]
        self.assertEqual(card["not_for"], "factual questions")

    def test_card_omits_not_for_when_a_skill_does_not_declare_it(self):
        write_skill(self.root, VALID_SKILL, folder="demo_skill")
        registry = SkillRegistry(self.root, TIERS).load()
        self.assertNotIn("not_for", registry.routing_cards()[0])

    def test_routing_cards_stay_small(self):
        write_skill(self.root, VALID_SKILL, folder="demo_skill")
        registry = SkillRegistry(self.root, TIERS).load()
        card = registry.routing_cards()[0]
        self.assertEqual(sorted(card), ["args", "name", "when"])
        self.assertEqual(card["args"], {"thing": "string (required)", "times": "integer"})


class TestShippedSkills(unittest.TestCase):
    """The real skills in the repo must always load - a broken one ships broken."""

    def test_every_shipped_skill_is_valid(self):
        root = Path(__file__).resolve().parent.parent.parent / "skills"
        registry = SkillRegistry(root, TIERS).load()
        self.assertEqual(registry.errors, [], f"skill load errors: {registry.errors}")
        self.assertTrue(registry.skills, "no skills were loaded at all")

    def test_shipped_skill_declares_when_not_to_use_it(self):
        root = Path(__file__).resolve().parent.parent.parent / "skills"
        registry = SkillRegistry(root, TIERS).load()
        for skill in registry.skills.values():
            with self.subTest(skill=skill.name):
                self.assertTrue(
                    skill.not_for,
                    f"{skill.name} has no not_for, so the router cannot rule it out",
                )

    def _shipped(self):
        root = Path(__file__).resolve().parent.parent.parent / "skills"
        return SkillRegistry(root, TIERS).load()

    def _card(self, registry, name):
        """Look a card up by name - an index would drift as skills are added."""
        return next(c for c in registry.routing_cards() if c["name"] == name)

    def test_study_breakdown_is_routable(self):
        registry = self._shipped()
        skill = registry.get("study_breakdown")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.required_fields, ["topic"])
        self.assertFalse(skill.requires_confirmation)
        # The shipped skill must tell the router when NOT to use it, or a
        # one-skill library answers every question with a study plan.
        self.assertTrue(skill.not_for, "study_breakdown should declare not_for")
        self.assertIn("not_for", self._card(registry, "study_breakdown"))

    def test_remember_is_shipped_as_a_deterministic_action(self):
        """`remember` writes to disk, so it must not be a prompt-based skill."""
        registry = self._shipped()
        skill = registry.get("remember")
        self.assertIsNotNone(skill, "the explicit write path must ship")
        self.assertEqual(skill.action, "remember")
        self.assertEqual(skill.required_fields, ["text"])
        # Writing is what the student asked for, so it needs no second gate.
        self.assertFalse(skill.requires_confirmation)
        self.assertTrue(skill.not_for, "remember must rule out un-asked-for saving")

    def test_the_action_skills_are_remember_and_quiz(self):
        """Only two skills have side effects, and each writes in one place."""
        registry = self._shipped()
        writers = sorted(s.name for s in registry.skills.values() if s.action != "prompt")
        self.assertEqual(writers, ["quiz", "remember"])
        # Exactly one of them writes to *memory*: an always-on extractor is the
        # thing we refused, and a second writer would quietly reintroduce it.
        memory_writers = [
            s.name for s in registry.skills.values() if s.action == "remember"
        ]
        self.assertEqual(memory_writers, ["remember"])

    def test_quiz_is_an_action_that_reads_the_page_and_memory(self):
        registry = self._shipped()
        skill = registry.get("quiz")
        self.assertIsNotNone(skill, "the quiz skill must ship")
        self.assertEqual(skill.action, "quiz")
        self.assertTrue(skill.not_for, "quiz must rule itself out somewhere")
        # Nothing is required: "quiz me" on a page, or on what they struggle
        # with, is a complete request.
        self.assertEqual(skill.required_fields, [])
        self.assertEqual(skill.context, ("memory", "page"))
        # Asking to be tested is the student's own request, so it needs no
        # second gate. The gate that matters is the explicit-request one.
        self.assertFalse(skill.requires_confirmation)
        self.assertEqual(skill.input_schema["count"].default, 5)

    def test_the_step_5_skills_ship(self):
        registry = self._shipped()
        for name in ["explain", "rephrase", "plan"]:
            with self.subTest(skill=name):
                skill = registry.get(name)
                self.assertIsNotNone(skill, f"{name} must ship")
                self.assertTrue(skill.not_for, f"{name} must rule itself out somewhere")
        self.assertEqual(registry.get("explain").required_fields, ["subject"])
        self.assertEqual(registry.get("plan").required_fields, ["goal"])
        # rephrase is the one skill with nothing required: the words to restate can
        # come from the utterance, from the page, or from what we just said, so
        # demanding them as an argument would fail a request it can actually do.
        self.assertEqual(registry.get("rephrase").required_fields, [])

    def test_rephrase_keeps_the_passage_out_of_its_instruction(self):
        """A placeholder that renders empty is what a model restates instead.

        The passage travels as a labelled block, so the instruction never carries
        a hole where the passage should be.
        """
        from server import context_blocks

        skill = self._shipped().get("rephrase")
        prompt = skill.render_prompt({"text": "a buffer resists pH change", "style": "shorter"})
        self.assertNotIn("a buffer resists pH change", prompt)
        self.assertNotIn("not specified", prompt)
        self.assertIn("shorter", prompt)
        # The prompt has to name the block the passage arrives in, or the model is
        # asked to restate something it cannot locate.
        self.assertIn("QUOTED PASSAGE", prompt)
        self.assertIn("PAGE", prompt)

        block = context_blocks(skill.context, {"quoted": "a buffer resists pH change"})
        self.assertIn("a buffer resists pH change", block)
        self.assertIn("quoted passage", block)

    def test_rephrase_is_the_only_skill_that_reads_a_quoted_passage(self):
        registry = self._shipped()
        quoted = sorted(s.name for s in registry.skills.values() if "quoted" in s.context)
        self.assertEqual(quoted, ["rephrase"])

    def test_plan_is_the_only_ultra_skill(self):
        """Ultra is fenced to study-time allocation; the fence is checkable."""
        registry = self._shipped()
        ultra = sorted(s.name for s in registry.skills.values() if s.tier == "ultra")
        self.assertEqual(ultra, ["plan"])

    def test_only_skills_that_work_from_the_page_declare_it(self):
        registry = self._shipped()
        with_page = sorted(
            s.name for s in registry.skills.values() if "page" in s.context
        )
        self.assertEqual(
            with_page, ["explain", "page_summary", "plan", "quiz", "rephrase"]
        )
        # study_breakdown is pure planning and remembers nothing, so it asks for
        # nothing - the smallest context that does the job.
        self.assertEqual(registry.get("study_breakdown").context, ())
        # rephrase is the one that needs a referent for "that".
        self.assertIn("previous", registry.get("rephrase").context)

    def test_no_skill_asks_for_more_context_than_it_uses(self):
        registry = self._shipped()
        self.assertEqual(registry.get("explain").context, ("page",))
        self.assertEqual(registry.get("plan").context, ("memory", "page"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
