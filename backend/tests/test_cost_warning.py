#!/usr/bin/env python3
"""Tests for Step 11's last piece: the per-request cost ceiling, finally wired.

    python3 backend/tests/test_cost_warning.py

`warn_over_usd_per_request` was written into `config/models.json` in Step 0 and read
by nothing until now. A ceiling nobody checks is worse than no ceiling, because it
reads like a safeguard - so these tests are mostly about the three ways a warning
goes wrong:

1. **It fires when it should not.** An ordinary turn is a tenth of a cent; warning
   on it would train the student to ignore the one that matters.
2. **It does not fire when it should.** A misspelled or deleted config line must
   fall back to the default rather than switch a safeguard off silently.
3. **It is said without saying anything.** A cost notice that does not carry the
   amount, the ceiling, and whether it *stopped* anything is noise; and one that
   only exists in the JSON payload is a notice this student never hears, because
   the panel speaks the reply and nothing else.
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
from approvals import Approvals  # noqa: E402
from memory import MemoryStore  # noqa: E402
from nemotron import cost_usd  # noqa: E402
from quiz import QuizStore  # noqa: E402
from skills import SkillRegistry  # noqa: E402
from spend import (  # noqa: E402
    DEFAULT_WARN_OVER_USD_PER_REQUEST,
    per_request_warning,
    warn_ceiling_usd,
)

# A turn big enough to be worth talking about: one router call and one executor
# call, both reporting this much. The expected cost is computed from the config
# below rather than hard-coded, so a price change does not silently turn these
# tests into a test of nothing.
USAGE = {"prompt_tokens": 1000, "completion_tokens": 400}
ANSWER = "It is on the 12th."

# A skill that has to be confirmed, for the path that does not use the router for
# its answer.
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


def budget(config, value):
    """A config whose ceiling is `value`. Copies, so the real one is untouched."""
    copied = json.loads(json.dumps(config))
    copied["budgets"]["warn_over_usd_per_request"] = value
    return copied


class TestTheCeiling(unittest.TestCase):
    def test_the_config_number_is_the_ceiling(self):
        self.assertEqual(warn_ceiling_usd(budget(server.CONFIG, 0.02)), 0.02)

    def test_a_config_without_the_key_falls_back_to_the_default(self):
        """Deleting the line must not silently switch the safeguard off."""
        config = json.loads(json.dumps(server.CONFIG))
        config["budgets"].pop("warn_over_usd_per_request")
        self.assertEqual(warn_ceiling_usd(config), DEFAULT_WARN_OVER_USD_PER_REQUEST)
        self.assertEqual(warn_ceiling_usd({}), DEFAULT_WARN_OVER_USD_PER_REQUEST)

    def test_an_unusable_value_falls_back_rather_than_guessing(self):
        for raw in ("five cents", None, True, [], {}, "0.05x"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    warn_ceiling_usd(budget(server.CONFIG, raw)),
                    DEFAULT_WARN_OVER_USD_PER_REQUEST,
                )

    def test_an_explicit_zero_or_negative_turns_it_off(self):
        for raw in (0, 0.0, -1):
            with self.subTest(raw=raw):
                config = budget(server.CONFIG, raw)
                self.assertEqual(warn_ceiling_usd(config), 0.0)
                self.assertIsNone(per_request_warning(config, 5.0))

    def test_an_ordinary_turn_does_not_warn(self):
        self.assertIsNone(per_request_warning(server.CONFIG, 0.001057))

    def test_a_turn_under_or_exactly_on_the_ceiling_does_not_warn(self):
        """The name says *over*: a turn that obeyed the ceiling did not break it."""
        config = budget(server.CONFIG, 0.05)
        self.assertIsNone(per_request_warning(config, 0.049))
        self.assertIsNone(per_request_warning(config, 0.05))

    def test_a_turn_over_the_ceiling_warns_with_three_numbers(self):
        warning = per_request_warning(budget(server.CONFIG, 0.05), 0.0612)
        self.assertEqual(warning["ceiling_usd"], 0.05)
        self.assertEqual(warning["cost_usd"], 0.0612)
        self.assertAlmostEqual(warning["over_by_usd"], 0.0112, places=6)

    def test_the_cost_is_rounded_the_way_the_display_rounds_it(self):
        """One number, not two roundings of the same spend."""
        config = budget(server.CONFIG, 0.05)
        self.assertIsNone(per_request_warning(config, 0.0500001), "half a micro-dollar over")
        self.assertEqual(per_request_warning(config, 0.050001)["cost_usd"], 0.050001)

    def test_nonsense_costs_warn_about_nothing_rather_than_crashing(self):
        config = budget(server.CONFIG, 0.05)
        for raw in (None, "abc", -3, [], {}):
            with self.subTest(raw=raw):
                self.assertIsNone(per_request_warning(config, raw))


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_approvals.py)."""

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
        head, _, body = text.partition("\r\n\r\n")
        return int(head.split(" ")[1]), (json.loads(body) if body.strip() else None)


class ScriptedClient:
    """A router call and an executor call, both reporting a real usage block."""

    def __init__(self, decision=None):
        self.decision = decision or {
            "skill": "general",
            "args": {"request": "when is the midterm"},
            "needs_confirmation": False,
        }
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        content = (
            json.dumps(self.decision)
            if "You are a router" in messages[0]["content"]
            else ANSWER
        )
        return (
            {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": dict(USAGE)},
            0.01,
        )

    def expected_total(self, config):
        """What these two calls cost, from the config's own prices."""
        return cost_usd(config, "nano", USAGE) + cost_usd(config, "super", USAGE)


class WarningTestCase(unittest.TestCase):
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
        self._ceiling = server.CONFIG["budgets"].get("warn_over_usd_per_request")
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.APPROVALS = Approvals()
        server.SPEND = type(server.SPEND)(server.CONFIG)
        skills = self.root / "skills"
        folder = skills / "rearrange_tabs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(CONFIRMING_SKILL, encoding="utf-8")
        server.REGISTRY = SkillRegistry(skills, tiers=list(server.CONFIG["tiers"])).load()
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
        server.CONFIG["budgets"]["warn_over_usd_per_request"] = self._ceiling
        sys.stdout = self._stdout
        self.tmp.cleanup()

    # --- helpers ---

    def set_ceiling(self, value):
        server.CONFIG["budgets"]["warn_over_usd_per_request"] = value

    def turn(self, transcript="when is the midterm", **fields):
        return HandlerStub(body={"transcript": transcript, **fields}).send()

    def answer(self, **fields):
        return HandlerStub(body=fields).send()

    def log_lines(self, event):
        path = self.root / "action-log.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if f'"event": "{event}"' in line
        ]


class TestTheTurnSaysSo(WarningTestCase):
    def test_an_ordinary_turn_answers_with_the_shipped_ceiling(self):
        """The common case, against the real number: a tenth of a cent."""
        status, payload = self.turn()
        self.assertEqual(status, 200)
        self.assertIsNone(payload["cost_warning"])
        self.assertEqual(payload["reply"], ANSWER, "an ordinary reply was touched")
        self.assertNotIn("over the $", payload["reply"])
        self.assertFalse(self.log_lines("cost_warning"))

    def test_a_turn_over_the_ceiling_says_the_three_things(self):
        cheap = self.client.expected_total(server.CONFIG)
        self.set_ceiling(round(cheap / 2, 8))
        status, payload = self.turn()
        self.assertEqual(status, 200)
        warning = payload["cost_warning"]
        self.assertIsNotNone(warning, "a turn over the ceiling said nothing")
        self.assertEqual(warning["ceiling_usd"], round(cheap / 2, 8))
        self.assertAlmostEqual(warning["cost_usd"], payload["cost_usd"], places=6)
        # The answer survives, and the sentence that follows it is ours, not the
        # model's - it names the amount and the ceiling, and says nothing stopped.
        self.assertTrue(payload["reply"].startswith(ANSWER))
        self.assertEqual(
            payload["reply"], ANSWER + " " + server.cost_warning_reply(warning)
        )
        self.assertIn("$", payload["reply"])
        self.assertIn("Nothing was blocked", payload["reply"])
        self.assertEqual(payload["reply"].count("Nothing was blocked"), 1, "said twice")

    def test_the_amount_matches_what_the_cost_line_shows(self):
        """One number in two places, not two roundings of the same spend."""
        self.set_ceiling(0.0000001)
        _, payload = self.turn()
        self.assertEqual(
            payload["cost_warning"]["cost_usd"], round(payload["cost_usd"], 6)
        )

    def test_the_log_holds_the_amounts_and_the_flag(self):
        self.set_ceiling(0.0000001)
        self.turn()
        warnings = self.log_lines("cost_warning")
        self.assertEqual(len(warnings), 1)
        self.assertGreater(warnings[0]["cost_usd"], warnings[0]["ceiling_usd"])
        self.assertEqual(warnings[0]["skill"], "general")
        turns = self.log_lines("turn")
        self.assertIs(turns[-1]["cost_over_ceiling"], True)
        # Counts and amounts only: the student's own sentence never lands here.
        raw = (self.root / "action-log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("when is the midterm", raw)

    def test_a_turn_under_the_ceiling_reads_the_same_keys(self):
        _, warned = self.turn()  # the shipped ceiling: not crossed
        self.set_ceiling(0.0000001)
        _, over = self.turn()
        self.assertEqual(set(warned), set(over), "the two paths will drift")
        self.assertIsNone(warned["cost_warning"])
        self.assertIsNotNone(over["cost_warning"])

    def test_it_is_not_sticky(self):
        """One expensive turn does not make the next one sound expensive."""
        self.set_ceiling(0.0000001)
        _, over = self.turn()
        self.assertIsNotNone(over["cost_warning"])
        self.set_ceiling(0.05)
        _, ordinary = self.turn()
        self.assertIsNone(ordinary["cost_warning"])

    def test_turning_it_off_is_quiet_even_on_an_expensive_turn(self):
        self.set_ceiling(0)
        _, payload = self.turn()
        self.assertIsNone(payload["cost_warning"])
        self.assertEqual(payload["reply"], ANSWER)

    def test_a_confirmed_action_warns_too(self):
        """The path that does not route its answer is not allowed to stay quiet."""
        self.client.decision = {
            "skill": "rearrange_tabs",
            "args": {"group_by": "course"},
            "needs_confirmation": False,
        }
        status, asked = self.turn("tidy up my tabs")
        self.assertEqual(status, 200)
        approval_id = asked["confirmation"]["id"]

        self.set_ceiling(0.0000001)
        status, payload = self.answer(confirm=approval_id)
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["cost_warning"], "a confirmed action stayed quiet")
        self.assertIn("Nothing was blocked", payload["reply"])
        self.assertTrue(self.log_lines("cost_warning"))
        self.assertIs(self.log_lines("turn")[-1]["cost_over_ceiling"], True)

    def test_a_free_turn_never_warns(self):
        """A refusal costs nothing, so there is nothing to warn about."""
        self.client.decision = {
            "skill": "rearrange_tabs",
            "args": {"group_by": "course"},
            "needs_confirmation": False,
        }
        self.set_ceiling(0.0000001)
        _, asked = self.turn("tidy up my tabs")
        # The asking turn may itself have warned - it spent a router call, and the
        # warning belongs to the turn that spent the money rather than to the one
        # after it. What matters here is that the refusal adds nothing.
        before = len(self.log_lines("cost_warning"))
        status, declined = self.answer(cancel=asked["confirmation"]["id"])
        self.assertEqual(status, 200)
        self.assertEqual(declined["cost_usd"], 0.0)
        self.assertIsNone(declined["cost_warning"])
        self.assertEqual(
            len(self.log_lines("cost_warning")), before, "a free refusal warned"
        )

    def test_the_turn_that_spent_the_money_is_the_one_that_warns(self):
        """Not the next one: a warning about a turn two answers ago is a mystery."""
        self.client.decision = {
            "skill": "rearrange_tabs",
            "args": {"group_by": "course"},
            "needs_confirmation": False,
        }
        self.set_ceiling(0.0000001)
        _, asked = self.turn("tidy up my tabs")
        self.assertIsNotNone(asked["cost_warning"], "the asking turn stayed quiet")
        self.assertIn("Nothing was blocked", asked["reply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
