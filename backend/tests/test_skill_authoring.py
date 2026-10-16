#!/usr/bin/env python3
"""Tests for Step 11's second slice: the agent drafts a skill, the student approves it.

    python3 backend/tests/test_skill_authoring.py

Step 9's own label said the agent would write its own skills once a task had been
confirmed, and left the machinery for it with no write path. This is the write
path, and the tests are mostly about what it refuses.

Six properties, each a way this goes wrong:

1. **Nothing is written before the student has read the file.** The tool that
   drafts is read-only, and the file on disk after a turn that drafted one is the
   same file that was there before it.
2. **What is written is what was shown**, byte for byte. A "yes" that saves a
   different file from the one on screen makes the question a formality.
3. **A file written this way may only say things.** `action: prompt`, always: a
   drafted skill cannot select a handler that writes, however the student reads the
   preview.
4. **The file has to load before it exists** - checked with the registry's own
   validator, so a draft cannot become a broken skill that only shows up as a
   registry error at the next restart.
5. **Saved means routable.** The registry is re-read, so the next request can use
   the skill; a file that only becomes real after a restart has been written, not
   saved.
6. **A refusal is free, final, and single-use.** No model call, no write, and an
   approval that has been used cannot be used again.
"""

import email.message
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
import skillwriter  # noqa: E402
from agent import agent_reply  # noqa: E402
from approvals import Approvals  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402
from skills import SkillRegistry  # noqa: E402
from tools import TOOL_NAMES, run_tool  # noqa: E402

DRAFT_NAME = "lab_blocks"
# Inside the `## Prompt` fence: the most sensitive text in the file, and the string
# the log must never hold.
MARKER = "LAB-BLOCKS-MARKER"
# Longer than a skill file is allowed to be: a model generating at length is a real
# refusal, not a hypothetical one.
GENERATED_FILLER = "z" * (skillwriter.MAX_SKILL_CHARS + 10)

SKILL_TEMPLATE = """---
name: {name}
description: "turn a lab report into a few sittings of work"
version: 0.1.0
tier: super
when_to_use: "the student asks how to break a lab report into study blocks"
not_for: "a question that wants one short factual answer"
tools: []
metadata:
  hermes:
    tags: [study, lab]
---

# {name}

## When to Use

When a big piece of writing needs splitting into sittings.

## Input

```json
{{
  "goal": {{"type": "string", "required": true, "description": "the piece of work"}}
}}
```

## Prompt

```
Break this into study blocks: {{goal}}. {marker}
```
"""


def draft_text(name=DRAFT_NAME, marker=MARKER):
    return SKILL_TEMPLATE.format(name=name, marker=marker)


class WriterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tiers = list(server.CONFIG["tiers"])

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

    def refusal(self, *args, **kwargs):
        """The message from a refused file, or a failure if it was accepted."""
        try:
            skillwriter.check(*args, **kwargs)
        except skillwriter.SkillWriteError as exc:
            return str(exc), exc.code
        self.fail("that file was accepted")


class TestTheWriterChecksWithoutWriting(WriterTestCase):
    def test_a_valid_file_is_accepted(self):
        skill = skillwriter.check(self.root, DRAFT_NAME, draft_text(), self.tiers)
        self.assertEqual(skill.name, DRAFT_NAME)
        self.assertEqual(skill.tier, "super")
        self.assertEqual(skill.action, "prompt")
        self.assertEqual(list(skill.input_schema), ["goal"])

    def test_nothing_is_written_by_the_check(self):
        skillwriter.check(self.root, DRAFT_NAME, draft_text(), self.tiers)
        self.assertEqual(self.files(), [], "checking a file wrote something")

    def test_the_name_has_to_be_an_identifier(self):
        """A name goes into a path, so the grammar is the guard."""
        for bad in ("../evil", "..", "sub/dir", "Lab", "lab blocks", "lab.blocks",
                    "1lab", "_lab", "", "l" * 62):
            message, code = self.refusal(self.root, bad, draft_text("whatever"), self.tiers)
            self.assertEqual(code, "bad_name", f"{bad!r} was refused for the wrong reason")
            self.assertIn("folder", message)

    def test_an_empty_file_is_refused(self):
        message, code = self.refusal(self.root, DRAFT_NAME, "   \n", self.tiers)
        self.assertEqual(code, "empty")

    def test_a_file_too_long_to_read_is_refused(self):
        text = draft_text().replace(MARKER, GENERATED_FILLER)
        self.assertGreater(len(text), skillwriter.MAX_SKILL_CHARS)
        message, code = self.refusal(self.root, DRAFT_NAME, text, self.tiers)
        self.assertEqual(code, "too_long")
        self.assertIn(str(skillwriter.MAX_SKILL_CHARS), message)

    def test_an_existing_skill_is_refused_rather_than_overwritten(self):
        path, _ = skillwriter.save(self.root, DRAFT_NAME, draft_text(), self.tiers)
        before = path.read_text(encoding="utf-8")
        message, code = self.refusal(
            self.root, DRAFT_NAME, draft_text(marker="OTHER-MARKER"), self.tiers
        )
        self.assertEqual(code, "exists")
        self.assertEqual(path.read_text(encoding="utf-8"), before, "the existing skill changed")


class TestTheWriterObeysTheRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.tiers = list(server.CONFIG["tiers"])

    def tearDown(self):
        self.tmp.cleanup()

    def refusal(self, text, tiers=None):
        try:
            skillwriter.check(self.root, DRAFT_NAME, text, self.tiers if tiers is None else tiers)
        except skillwriter.SkillWriteError as exc:
            return str(exc), exc.code
        self.fail("that file was accepted")

    def test_a_file_the_registry_would_reject_is_refused_here(self):
        """No ## Input block is a registry error at startup; it is one now instead."""
        text = draft_text().replace("## Input", "## Notes")
        message, code = self.refusal(text)
        self.assertEqual(code, "invalid")
        self.assertIn("Input", message)

    def test_a_broken_frontmatter_block_is_refused(self):
        message, code = self.refusal("name: " + DRAFT_NAME + "\n\n## Prompt\n\n```\nx\n```\n")
        self.assertEqual(code, "invalid")

    def test_a_tier_the_server_cannot_run_is_refused_with_the_vocabulary(self):
        """The model is not told the tier names in prose, so the error carries them."""
        text = draft_text().replace("tier: super", "tier: mega")
        message, code = self.refusal(text)
        self.assertEqual(code, "invalid")
        self.assertIn("mega", message)
        self.assertIn("ultra", message, "the refusal has to name the real tiers")

    def test_a_file_that_selects_a_handler_that_writes_is_refused(self):
        """The safety property: a skill we wrote may say things, and nothing else."""
        for action in ("remember", "quiz"):
            text = draft_text().replace("tier: super", f"tier: super\naction: {action}")
            message, code = self.refusal(text)
            self.assertEqual(code, "action", f"{action} was refused for the wrong reason")
            self.assertIn(action, message)
            self.assertIn("prompt", message)

    def test_an_action_that_is_not_on_the_allowlist_is_still_a_registry_error(self):
        text = draft_text().replace("tier: super", "tier: super\naction: curl")
        message, code = self.refusal(text)
        self.assertEqual(code, "invalid")
        self.assertIn("curl", message)


class TestTheWrite(WriterTestCase):
    def test_the_bytes_written_are_the_bytes_checked(self):
        text = draft_text()
        path, skill = skillwriter.save(self.root, DRAFT_NAME, text, self.tiers)
        self.assertEqual(path, self.root / DRAFT_NAME / "SKILL.md")
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(skill.path, path)

    def test_the_no_longer_a_skill_the_registry_cannot_see_it(self):
        text = draft_text()
        skillwriter.save(self.root, DRAFT_NAME, text, self.tiers)
        registry = SkillRegistry(self.root, tiers=self.tiers).load()
        self.assertEqual(registry.errors, [])
        self.assertEqual(registry.names(), [DRAFT_NAME])
        self.assertEqual(registry.get(DRAFT_NAME).render_prompt({"goal": "lab 3"})
                         .count(MARKER), 1)

    def test_a_staging_file_is_never_left_behind_for_the_registry_to_find(self):
        path, _ = skillwriter.save(self.root, DRAFT_NAME, draft_text(), self.tiers)
        leftovers = [p for p in self.root.rglob("*") if p.name != "SKILL.md" and p.is_file()]
        self.assertEqual(leftovers, [])
        self.assertEqual([p.name for p in self.root.rglob("SKILL.md")], ["SKILL.md"])

    def test_saving_refuses_what_checking_refuses(self):
        """The second check is the one that has to be true when the student answers."""
        skillwriter.save(self.root, DRAFT_NAME, draft_text(), self.tiers)
        with self.assertRaises(skillwriter.SkillWriteError):
            skillwriter.save(self.root, DRAFT_NAME, draft_text(), self.tiers)

    def test_the_path_a_student_is_shown_is_relative_to_the_repo(self):
        self.assertEqual(skillwriter.relative_path(DRAFT_NAME),
                         f"skills/{DRAFT_NAME}/SKILL.md")


class TestTheToolIsReadOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, name=DRAFT_NAME, text=None):
        return run_tool(
            "propose_skill",
            {"name": name, "skill_md": draft_text(name) if text is None else text},
            skills_root=self.root,
        )

    def test_a_good_file_comes_back_as_a_draft_and_not_as_a_file(self):
        result = self.propose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"]["name"], DRAFT_NAME)
        self.assertEqual(result["draft"]["text"], draft_text())
        self.assertIn("NOT SAVED YET", result["text"])
        self.assertEqual(list(self.root.rglob("*")), [], "the tool wrote something")

    def test_every_return_path_answers_with_the_same_keys(self):
        """The hole the citation tests found in `sources`, closed for `draft`."""
        shape = set(self.propose())
        paths = [
            run_tool("not_a_tool", {}),
            run_tool("calculate", {"expression": "2 + 2"}, skills_root=self.root),
            run_tool("web_search", {}, skills_root=self.root),
            self.propose(name="Lab"),
            self.propose(name=DRAFT_NAME, text="not a skill at all"),
        ]
        for result in paths:
            self.assertEqual(set(result), shape, f"{result['name']} answers differently")

    def test_a_refused_file_says_why_and_carries_no_draft(self):
        result = self.propose(name="Lab")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_name")
        self.assertIsNone(result["draft"])

    def test_a_file_that_would_write_is_refused_by_the_tool_too(self):
        text = draft_text().replace("tier: super", "tier: super\naction: remember")
        result = self.propose(text=text)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "action")
        self.assertIsNone(result["draft"])

    def test_the_skill_name_is_advertised_to_the_model(self):
        self.assertIn("propose_skill", TOOL_NAMES)


class TestTheLoopOnlyProposes(unittest.TestCase):
    """`agent_reply` carries the draft out; it never writes one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    class Runner:
        def __init__(self, draft=True):
            self.draft = draft
            self.calls = []

        def __call__(self, tool, args):
            self.calls.append(tool)
            return {
                "name": tool,
                "ok": True,
                "text": "NOT SAVED YET. The file is a valid skill.",
                "sources": [],
                "reason": "",
                "draft": (
                    {"name": DRAFT_NAME, "text": draft_text()}
                    if self.draft and tool == "propose_skill"
                    else None
                ),
            }

    def loop(self, runner, *replies):
        """A scripted loop: each scripted model reply is one step."""
        scripted = list(replies)

        def ask(tier, messages, log=None):
            text = scripted.pop(0) if scripted else '{"final": "done"}'
            return text, {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}, 0.01

        return agent_reply("save that as a skill", ask, tier="super", run=runner)

    def test_a_proposed_file_is_handed_out_rather_than_written(self):
        runner = self.Runner()
        outcome = self.loop(
            runner,
            json.dumps({"tool": "propose_skill",
                        "args": {"name": DRAFT_NAME, "skill_md": draft_text()}}),
            '{"final": "I have drafted it."}',
        )
        self.assertEqual(outcome["draft"]["name"], DRAFT_NAME)
        self.assertEqual(outcome["draft"]["text"], draft_text())
        self.assertEqual(outcome["reply"], "I have drafted it.")
        self.assertEqual(list(self.root.rglob("*")), [], "the loop wrote a file")

    def test_a_turn_that_proposes_nothing_carries_no_draft(self):
        runner = self.Runner(draft=False)
        outcome = self.loop(runner, '{"final": "Just an answer."}')
        self.assertIsNone(outcome["draft"])

    def test_only_one_file_is_drafted_per_turn(self):
        """A student shown one file must not be told about two."""
        runner = self.Runner()
        second = draft_text("second_skill", marker="SECOND")
        outcome = self.loop(
            runner,
            json.dumps({"tool": "propose_skill",
                        "args": {"name": DRAFT_NAME, "skill_md": draft_text()}}),
            json.dumps({"tool": "propose_skill",
                        "args": {"name": "second_skill", "skill_md": second}}),
            '{"final": "I have drafted it."}',
        )
        self.assertEqual(outcome["draft"]["name"], DRAFT_NAME, "the second draft replaced the first")
        self.assertIn("second skill", " ".join(outcome["notes"]))


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_approvals.py)."""

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
    """Routes to `general`, drafts one skill file, then answers."""

    def __init__(self, name=DRAFT_NAME, text=None, decision=None):
        self.name = name
        self.text = draft_text(name) if text is None else text
        self.decision = decision or {
            "skill": "general",
            "args": {"request": "save that as a skill"},
            "needs_confirmation": False,
        }
        self.calls = []
        self.drafted = 0

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        head = messages[0]["content"]
        if "You are a router" in head:
            content = json.dumps(self.decision)
        elif self.drafted == 0:
            self.drafted += 1
            content = json.dumps(
                {"tool": "propose_skill", "args": {"name": self.name, "skill_md": self.text}}
            )
        else:
            content = "I have drafted it, and I have not saved it."
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            0.01,
        )

    @property
    def executor_prompts(self):
        return [
            call["messages"][-1]["content"]
            for call in self.calls
            if "You are a router" not in call["messages"][0]["content"]
        ]


class DraftingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills_dir = self.root / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._registry = server.REGISTRY
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._approvals = server.APPROVALS
        self._sent = server.SPEND
        self._demo = server.DEMO_MODE
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.APPROVALS = Approvals()
        server.SPEND = type(server.SPEND)(server.CONFIG)
        server.REGISTRY = SkillRegistry(self.skills_dir, tiers=list(server.CONFIG["tiers"])).load()
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
        server.DEMO_MODE = self._demo
        sys.stdout = self._stdout
        self.tmp.cleanup()

    # --- helpers ---

    def skill_file(self, name=DRAFT_NAME):
        return self.skills_dir / name / "SKILL.md"

    def turn(self, transcript, **fields):
        return HandlerStub(body={"transcript": transcript, **fields}).send()

    def answer(self, **fields):
        return HandlerStub(body=fields).send()

    def get(self, path):
        return HandlerStub(path=path, command="GET").send()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def seed_skill(self, name=DRAFT_NAME):
        folder = self.skills_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(draft_text(name), encoding="utf-8")
        server.REGISTRY = SkillRegistry(self.skills_dir, tiers=list(server.CONFIG["tiers"])).load()

    def ask(self, client=None):
        """Save something as a skill. -> (status, payload)."""
        status, payload = self.turn("save that as a skill please")
        self.assertEqual(status, 200, payload)
        return payload


class TestTheQuestion(DraftingTestCase):
    def test_a_drafted_file_is_a_question_and_not_a_file(self):
        payload = self.ask()
        self.assertTrue(payload["needs_confirmation"])
        confirmation = payload["confirmation"]
        self.assertEqual(confirmation["kind"], "skill")
        self.assertEqual(confirmation["path"], f"skills/{DRAFT_NAME}/SKILL.md")
        # The exact text, so the panel can show what would be written.
        self.assertEqual(confirmation["draft"], draft_text())
        self.assertEqual(confirmation["expires_in_seconds"], 300)
        self.assertFalse(self.skill_file().exists(), "a file was written before the answer")
        self.assertNotIn(DRAFT_NAME, server.REGISTRY.names())

    def test_the_answer_waits_without_spending_anything_more(self):
        """The loop has already run; asking costs no second call."""
        self.ask()
        # router + two loop steps, and nothing after the draft.
        self.assertEqual(len(self.client.calls), 3)

    def test_the_question_itself_is_not_the_models_words(self):
        payload = self.ask()
        self.assertTrue(
            payload["reply"].endswith(server.draft_reply(f"skills/{DRAFT_NAME}/SKILL.md")),
            f"the model phrased the question: {payload['reply']!r}",
        )

    def test_the_file_is_logged_as_a_count_and_never_as_text(self):
        self.ask()
        text = self.log_text()
        self.assertIn("skill_draft", text)
        self.assertNotIn(MARKER, text, "the drafted file's prompt text reached the log")
        logged = [json.loads(line) for line in text.splitlines() if "skill_draft" in line][-1]
        self.assertEqual(logged["chars"], len(draft_text()))
        self.assertEqual(logged["skill"], DRAFT_NAME)

    def test_a_file_that_would_not_load_is_refused_without_asking(self):
        self.client = ScriptedClient(text=draft_text().replace("## Input", "## Notes"))
        server.get_client = lambda: self.client
        payload = self.ask()
        self.assertFalse(payload["needs_confirmation"], "a question was asked about a broken file")
        self.assertIsNone(payload["confirmation"])
        self.assertIn("nothing has been saved", payload["reply"])
        self.assertFalse(self.skill_file().exists())
        self.assertIn('"reason": "invalid"', self.log_text())

    def test_a_file_that_selects_a_handler_that_writes_is_refused_without_asking(self):
        self.client = ScriptedClient(
            text=draft_text().replace("tier: super", "tier: super\naction: remember")
        )
        server.get_client = lambda: self.client
        payload = self.ask()
        self.assertFalse(payload["needs_confirmation"])
        self.assertFalse(self.skill_file().exists())
        self.assertIn('"reason": "action"', self.log_text())

    def test_a_refused_file_is_never_reported_as_saved(self):
        """The reply that claims it worked is replaced by one we can vouch for."""
        self.client = ScriptedClient(text="not a skill file at all")
        server.get_client = lambda: self.client
        payload = self.ask()
        self.assertEqual(payload["reply"], server._DRAFT_FAILED)
        self.assertNotIn("drafted it", payload["reply"])
        self.assertIn("skill_draft_failed", self.log_text())
        self.assertFalse(self.skill_file().exists())

    def test_a_name_the_library_already_has_is_refused_without_asking(self):
        self.seed_skill()
        payload = self.ask()
        self.assertFalse(payload["needs_confirmation"])
        self.assertIn("already exists", payload["reply"])
        self.assertEqual(
            self.skill_file().read_text(encoding="utf-8"), draft_text(), "the existing file changed"
        )

    def test_a_demo_refuses_to_write_to_the_machine_hosting_it(self):
        server.DEMO_MODE = True
        payload = self.ask()
        self.assertFalse(payload["needs_confirmation"])
        self.assertIn("demo", payload["reply"])
        self.assertFalse(self.skill_file().exists())
        self.assertIn('"reason": "demo"', self.log_text())

    def test_the_same_keys_come_back_as_on_any_other_turn(self):
        drafting = self.ask()
        self.client.decision = {
            "skill": "general",
            "args": {"request": "hello"},
            "needs_confirmation": False,
        }
        self.client.drafted = 1  # answer instead of drafting
        status, plain = self.turn("what is a ketone")
        self.assertEqual(status, 200)
        self.assertEqual(set(drafting), set(plain))
        self.assertIsNone(plain["confirmation"])


class TestTheAnswer(DraftingTestCase):
    def confirm(self, approval_id):
        status, payload = self.answer(confirm=approval_id)
        self.assertEqual(status, 200, payload)
        return payload

    def test_a_yes_writes_exactly_what_was_shown(self):
        payload = self.ask()
        confirmation = payload["confirmation"]
        saved = self.confirm(confirmation["id"])
        self.assertEqual(self.skill_file().read_text(encoding="utf-8"), confirmation["draft"])
        self.assertEqual(saved["skill_saved"]["name"], DRAFT_NAME)
        self.assertEqual(saved["skill_saved"]["path"], f"skills/{DRAFT_NAME}/SKILL.md")
        self.assertEqual(saved["skill_saved"]["skills"], 1)
        self.assertEqual(saved["skill_saved"]["errors"], 0)
        self.assertIn("Saved", saved["reply"])
        self.assertIn(f"skills/{DRAFT_NAME}/SKILL.md", saved["reply"])
        self.assertFalse(saved["needs_confirmation"], "still asking after a yes")
        self.assertIsNone(saved["confirmation"])

    def test_saving_costs_nothing(self):
        payload = self.ask()
        saved = self.confirm(payload["confirmation"]["id"])
        self.assertEqual(saved["cost_usd"], 0.0)
        self.assertEqual(saved["tier"], "super")

    def test_the_very_next_request_can_use_it(self):
        """Saved means routable: the registry is re-read, not restarted."""
        self.client.decision = {
            "skill": DRAFT_NAME,
            "args": {"goal": "lab report 3"},
            "needs_confirmation": False,
        }
        self.client.drafted = 1
        self.client.calls.clear()
        status, before = self.turn("help me with my lab report")
        self.assertEqual(status, 200)
        self.assertNotEqual(
            before["skill"], DRAFT_NAME,
            "a skill that is not in the library was routed to",
        )
        self.assertFalse(
            any(MARKER in prompt for prompt in self.client.executor_prompts),
            "the skill's prompt ran before it existed",
        )
        self.client.calls.clear()

        self.client.decision = {
            "skill": "general",
            "args": {"request": "save that as a skill"},
            "needs_confirmation": False,
        }
        self.client.drafted = 0
        payload = self.ask()
        self.confirm(payload["confirmation"]["id"])

        self.client.decision = {
            "skill": DRAFT_NAME,
            "args": {"goal": "lab report 3"},
            "needs_confirmation": False,
        }
        status, after = self.turn("help me with my lab report")
        self.assertEqual(status, 200, after)
        self.assertEqual(after["skill"], DRAFT_NAME)
        self.assertEqual(after["skill_source"], "model")
        self.assertEqual(after["args"], {"goal": "lab report 3"})
        self.assertFalse(after["needs_confirmation"])
        self.assertTrue(
            any(MARKER in prompt for prompt in self.client.executor_prompts),
            f"the new skill's own prompt never ran: {self.client.executor_prompts}",
        )

    def test_the_new_skill_is_in_the_library_endpoint(self):
        self.confirm(self.ask()["confirmation"]["id"])
        status, payload = self.get("/skills")
        self.assertEqual(status, 200)
        self.assertEqual(payload["errors"], [])
        self.assertIn(DRAFT_NAME, [skill["name"] for skill in payload["skills"]])

    def test_a_no_writes_nothing_and_costs_nothing(self):
        payload = self.ask()
        self.client.calls.clear()
        status, declined = self.answer(cancel=payload["confirmation"]["id"])
        self.assertEqual(status, 200)
        self.assertIn("have not saved it", declined["reply"])
        self.assertEqual(declined["cost_usd"], 0.0)
        self.assertEqual(self.client.calls, [], "a refusal spent a model call")
        self.assertFalse(self.skill_file().exists())

    def test_the_file_is_never_written_twice(self):
        payload = self.ask()
        approval_id = payload["confirmation"]["id"]
        self.confirm(approval_id)
        status, again = self.answer(confirm=approval_id)
        self.assertEqual(status, 409, again)
        self.assertEqual(len(list(self.skills_dir.rglob("SKILL.md"))), 1)

    def test_the_log_says_it_was_written_and_holds_no_words_from_it(self):
        payload = self.ask()
        self.confirm(payload["confirmation"]["id"])
        text = self.log_text()
        self.assertIn("skill_written", text)
        self.assertNotIn(MARKER, text)
        written = [json.loads(line) for line in text.splitlines() if "skill_written" in line][0]
        self.assertEqual(written["skill"], DRAFT_NAME)
        self.assertEqual(written["chars"], len(draft_text()))
        self.assertEqual(written["skill_errors"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
