#!/usr/bin/env python3
"""Tests for the memory store.

    python3 backend/tests/test_memory.py

Everything runs against a temp directory, so these never touch the student's real
memory and never cost anything.

The assertion that matters most is not "the fact came back" - it is that
`memory/*.md` stays readable as ordinary Markdown, because that is the promise
made to the student and the reason this is not a database.
"""

import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from frontmatter import parse  # noqa: E402
from memory import (  # noqa: E402
    MAX_FACT_CHARS,
    MemoryError_,
    MemoryStore,
    derive_id,
    validate_topic,
)


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root).load()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, topic, text, added="2026-10-14"):
        return self.store.add(topic, text, added=added)

    def read(self, name):
        return (self.root / f"{name}.md").read_text(encoding="utf-8")


class TestTopicValidation(MemoryTestCase):
    def test_accepts_a_normal_slug(self):
        self.assertEqual(validate_topic("weak_topics"), "weak_topics")
        self.assertEqual(validate_topic("  Deadlines  "), "deadlines")

    def test_rejects_anything_that_could_escape_the_directory(self):
        for bad in ["../evil", "..", "a/b", "a\\b", ".hidden", "notes.md", "", "   ", "a" * 40]:
            with self.subTest(topic=bad):
                with self.assertRaises(MemoryError_):
                    validate_topic(bad)

    def test_rejects_non_strings_rather_than_coercing(self):
        with self.assertRaises(MemoryError_):
            validate_topic(None)
        with self.assertRaises(MemoryError_):
            validate_topic(7)

    def test_a_rejected_topic_writes_nothing_at_all(self):
        with self.assertRaises(MemoryError_):
            self.add("../../etc/passwd", "escape attempt")
        self.assertEqual(list(self.root.glob("*")), [])

    def test_empty_and_overlong_text_are_refused(self):
        with self.assertRaises(MemoryError_):
            self.add("general", "   ")
        with self.assertRaises(MemoryError_):
            self.add("general", "x" * (MAX_FACT_CHARS + 1))


class TestWriting(MemoryTestCase):
    def test_a_fact_survives_a_reload_verbatim(self):
        self.add("deadlines", "organic chemistry midterm is on 2026-11-12")
        self.store = MemoryStore(self.root).load()
        facts = self.store.facts("deadlines")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].text, "organic chemistry midterm is on 2026-11-12")
        self.assertEqual(facts[0].added, "2026-10-14")

    def test_the_file_is_readable_markdown_with_working_frontmatter(self):
        """The student's promise: open it in any editor and it makes sense."""
        self.add("deadlines", "midterm is on 2026-11-12")
        raw = self.read("deadlines")
        meta, body = parse(raw)

        self.assertEqual(meta["topic"], "deadlines")
        self.assertEqual(meta["title"], "Deadlines")
        self.assertIn("# Deadlines", body)
        # The fact is a plain bullet, and the id hides in a comment so the line
        # still reads as a sentence once rendered.
        self.assertIn("<!-- id:", body)
        rendered = re.sub(r"<!--.*?-->", "", body)
        self.assertIn("- midterm is on 2026-11-12", rendered)
        self.assertNotIn("id:", rendered, "the id must not be visible when rendered")

    def test_whitespace_in_a_fact_is_normalised(self):
        fact = self.add("general", "  lots\n of   space\there ")
        self.assertEqual(fact.text, "lots of space here")

    def test_two_identical_facts_are_two_facts(self):
        """Deduping on text would silently refuse a legitimate second note."""
        first = self.add("general", "same words")
        second = self.add("general", "same words")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.store.facts("general")), 2)

    def test_the_topic_file_is_created_on_first_use(self):
        self.assertFalse((self.root / "classes.md").exists())
        self.add("classes", "taking organic chemistry and calculus")
        self.assertTrue((self.root / "classes.md").exists())
        self.assertEqual(self.store.get("classes").topic, "classes")

    def test_no_temp_file_is_left_behind(self):
        self.add("general", "a fact")
        self.assertEqual(sorted(p.name for p in self.root.glob("*")), ["general.md"])

    def test_a_new_topic_gets_a_readable_title(self):
        self.add("weak_topics", "stoichiometry")
        self.assertEqual(self.store.get("weak_topics").title, "Weak topics")


class TestDeleting(MemoryTestCase):
    def test_delete_removes_exactly_one_fact(self):
        first = self.add("general", "one")
        second = self.add("general", "two")
        third = self.add("general", "three")

        self.assertTrue(self.store.delete("general", second.id))

        remaining = [f.text for f in self.store.facts("general")]
        self.assertEqual(remaining, ["one", "three"])
        # The neighbours keep their ids - deleting did not renumber them.
        self.assertEqual([f.id for f in self.store.facts("general")], [first.id, third.id])

    def test_delete_survives_a_reload(self):
        fact = self.add("general", "gone soon")
        self.assertTrue(self.store.delete("general", fact.id))
        self.store = MemoryStore(self.root).load()
        self.assertEqual(self.store.facts("general"), [])

    def test_deleting_something_already_gone_is_false_not_an_error(self):
        self.add("general", "here")
        self.assertFalse(self.store.delete("general", "nope1234"))
        self.assertFalse(self.store.delete("nosuchtopic", "nope1234"))

    def test_delete_needs_an_id(self):
        self.add("general", "here")
        for bad in ["", "   ", None, 3]:
            with self.subTest(fact_id=bad):
                with self.assertRaises(MemoryError_):
                    self.store.delete("general", bad)


class TestHandEditing(MemoryTestCase):
    """The student owns these files, so their edits have to be respected."""

    def test_a_bullet_with_no_id_gets_a_stable_derived_id(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\ntitle: Notes\n---\n\n# Notes\n\n- review the lab safety rules\n",
            encoding="utf-8",
        )
        first = MemoryStore(self.root).load().facts("notes")[0]
        second = MemoryStore(self.root).load().facts("notes")[0]
        self.assertEqual(first.id, derive_id("review the lab safety rules"))
        self.assertTrue(first.derived)
        self.assertEqual(first.id, second.id)

    def test_a_derived_id_can_still_be_deleted(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\n- review the lab safety rules\n- buy a lab coat\n",
            encoding="utf-8",
        )
        store = MemoryStore(self.root).load()
        target = store.facts("notes")[1]
        self.assertTrue(store.delete("notes", target.id))
        self.assertEqual([f.text for f in store.facts("notes")], ["review the lab safety rules"])

    def test_asterisk_and_plus_bullets_are_read_too(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\n* one\n+ two\n- three\n", encoding="utf-8"
        )
        self.assertEqual(
            [f.text for f in MemoryStore(self.root).load().facts("notes")],
            ["one", "two", "three"],
        )

    def test_a_bullet_inside_a_code_fence_is_an_example_not_a_memory(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\nReal:\n\n- actual fact\n\nExample:\n\n```\n- not a fact\n```\n",
            encoding="utf-8",
        )
        self.assertEqual(
            [f.text for f in MemoryStore(self.root).load().facts("notes")], ["actual fact"]
        )

    def test_prose_and_headings_are_ignored_not_choked_on(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\n# Notes\n\nSome prose.\n\n## Section\n\n- a fact\n\nMore prose.\n",
            encoding="utf-8",
        )
        store = MemoryStore(self.root).load()
        self.assertEqual(store.errors, [])
        self.assertEqual([f.text for f in store.facts("notes")], ["a fact"])

    def test_an_edit_on_disk_shows_up_without_a_restart(self):
        """Otherwise "edit the file yourself" is only true until we boot."""
        self.add("general", "first fact")
        path = self.root / "general.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("first fact", "edited fact"),
            encoding="utf-8",
        )
        self.assertEqual([f.text for f in self.store.facts("general")], ["edited fact"])

    def test_a_deleted_line_disappears_without_a_restart(self):
        self.add("general", "keep me")
        self.add("general", "delete me")
        path = self.root / "general.md"
        path.write_text(
            "\n".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if "delete me" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual([f.text for f in self.store.facts("general")], ["keep me"])

    def test_duplicate_ids_do_not_make_delete_ambiguous(self):
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\n"
            "- one <!-- id: same1 -->\n"
            "- two <!-- id: same1 -->\n",
            encoding="utf-8",
        )
        store = MemoryStore(self.root).load()
        facts = store.facts("notes")
        self.assertEqual(len({f.id for f in facts}), 2, "ids must be unique after loading")
        self.assertTrue(store.delete("notes", facts[0].id))
        self.assertEqual([f.text for f in store.facts("notes")], ["two"])

    def test_a_filename_that_is_not_a_slug_falls_back_to_frontmatter(self):
        (self.root / "My Notes.md").write_text(
            "---\ntopic: my_notes\n---\n\n- a fact\n", encoding="utf-8"
        )
        store = MemoryStore(self.root).load()
        self.assertIn("my_notes", store.topic_names())

    def test_a_capitalised_frontmatter_topic_does_not_lose_the_file(self):
        """Failing to read this would throw away memory to punish a capital letter."""
        (self.root / "deadlines.md").write_text(
            "---\ntopic: Deadlines\ntitle: Deadlines\n---\n\n- midterm on the 12th\n",
            encoding="utf-8",
        )
        store = MemoryStore(self.root).load()
        self.assertEqual(store.errors, [])
        self.assertEqual([f.text for f in store.facts("deadlines")], ["midterm on the 12th"])

    def test_an_unusable_file_is_reported_and_the_others_still_load(self):
        (self.root / "good.md").write_text(
            "---\ntopic: good\n---\n\n- fine\n", encoding="utf-8"
        )
        (self.root / "Bad Name.md").write_text("no frontmatter\n", encoding="utf-8")
        store = MemoryStore(self.root).load()
        self.assertIn("good", store.topic_names())
        self.assertEqual(len(store.errors), 1)
        self.assertIn("usable topic name", store.errors[0])


class TestConcurrency(MemoryTestCase):
    def test_parallel_writes_do_not_lose_facts(self):
        """The server is threaded, so two turns can overlap."""
        errors = []

        def worker(n):
            try:
                self.store.add("general", f"fact {n}")
            except Exception as exc:  # recorded, then asserted on
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.facts("general")), 20)
        # And on disk, not just in memory.
        reloaded = MemoryStore(self.root).load()
        self.assertEqual(len(reloaded.facts("general")), 20)


class TestDescribeAndPromptBlock(MemoryTestCase):
    def test_describe_counts_topics_and_facts(self):
        self.add("general", "one")
        self.add("deadlines", "two")
        self.add("deadlines", "three")
        described = self.store.describe()
        self.assertEqual(described["total"], 3)
        self.assertEqual([t["topic"] for t in described["topics"]], ["deadlines", "general"])
        self.assertEqual({t["topic"]: t["count"] for t in described["topics"]}, {"deadlines": 2, "general": 1})

    def test_prompt_block_is_empty_with_no_memory(self):
        """So callers can skip it instead of injecting a header with no facts."""
        self.assertEqual(self.store.prompt_block(), "")

    def test_prompt_block_names_the_topic_with_each_fact(self):
        self.add("deadlines", "midterm is on 2026-11-12")
        block = self.store.prompt_block()
        self.assertIn("deadlines: midterm is on 2026-11-12", block)

    def test_prompt_block_respects_its_caps(self):
        for n in range(30):
            self.add("general", f"fact number {n} padded out a little")
        block = self.store.prompt_block(max_facts=5)
        self.assertEqual(block.count("\n- "), 5)
        self.assertIn("25 more not shown", block)
        # Everything is still stored - only what we send is capped.
        self.assertEqual(len(self.store.facts()), 30)

    def test_prompt_block_honours_the_character_budget(self):
        """The character cap binds before the fact cap when facts are long."""
        for n in range(30):
            self.add("general", f"fact {n} " + "x" * 100)  # ~118 chars per line
        block = self.store.prompt_block(max_facts=30, max_chars=250)
        self.assertEqual(block.count("\n- "), 2)
        self.assertIn("28 more not shown", block)
        # And the budget is what cut it short, not the fact count.
        facts_chars = sum(len(line) + 1 for line in block.splitlines() if line.startswith("- "))
        self.assertLessEqual(facts_chars, 250)

    def test_a_readme_in_the_memory_directory_is_not_a_topic(self):
        """A directory we invite students to edit will eventually contain one."""
        (self.root / "README.md").write_text("# How memory works\n", encoding="utf-8")
        self.add("deadlines", "midterm is on 2026-11-12")
        store = MemoryStore(self.root).load()
        self.assertEqual(store.errors, [], "a README is documentation, not a broken topic")
        self.assertEqual(store.topic_names(), ["deadlines"])
        self.assertEqual(store.count(), 1)

    def test_notes_is_a_perfectly_valid_topic(self):
        """Only documentation names are skipped - `notes.md` is somebody's memory."""
        (self.root / "notes.md").write_text(
            "---\ntopic: notes\n---\n\n- a fact\n", encoding="utf-8"
        )
        store = MemoryStore(self.root).load()
        self.assertEqual(store.topic_names(), ["notes"])

    def test_an_empty_directory_is_not_an_error(self):
        store = MemoryStore(self.root / "does_not_exist").load()
        self.assertEqual(store.errors, [])
        self.assertEqual(store.describe()["total"], 0)
        self.assertEqual(store.prompt_block(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
