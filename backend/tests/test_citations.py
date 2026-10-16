#!/usr/bin/env python3
"""Tests for grounded citations: a searched answer the student can check.

    python3 backend/tests/test_citations.py

Step 9 gave the loop `web_search`, and the reply that came back never named a
single page it read. So a searched fact was *unverifiable*: the student had a
number, our word for it, and no way to tell the difference between a page we
actually read and a sentence the model produced from memory - which is the exact
defect that made the first version of the agent prompt inert.

The fix is one rule, and this file is mostly about holding to it:

**A link is a page we handed the model, never a page the model named.**

Everything else follows from that. `web_search` returns the pages it read;
`agent.py` numbers them, tells the model which numbers exist, and removes any
marker that points at none of them; the panel builds links from that list and
from nothing else. So the three ways this can fail are each tested here:

1. **The list is wrong** - a page that never loaded, a result the budget cut in
   half, or a `javascript:` URL gets a number. (See `TestWhatTheToolReturns`.)
2. **The numbering drifts** - two searches in one turn, or the same page twice,
   and `[2]` means two different pages. (See `TestTheLoopNumbersThem`.)
3. **A marker with nothing behind it reaches the student**, which is worse than
   no citation at all: it looks like a check, and a student who follows it and
   finds nothing learns to distrust the ones that are real.
   (See `TestAMarkerWithNoPageBehindIt` and `TestOverHTTP`.)
"""

import io
import json
import os
import sys
import tempfile
import unittest
from email import message_from_string
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import agent  # noqa: E402
import server  # noqa: E402
import tools  # noqa: E402
from agent import MAX_CITATIONS, agent_reply, ground_reply  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402
from tools import web_search  # noqa: E402

PAGES = [
    {"title": "Release notes", "url": "https://docs.example.org/notes", "text": "3.13 shipped."},
    {"title": "Download page", "url": "https://example.org/download", "text": "Get 3.13."},
    {"title": "Changelog", "url": "https://example.org/changelog", "text": "Every change."},
]


def exa_body(pages=PAGES):
    """A provider response in Exa's shape, which is what the tool parses."""
    return json.dumps({"results": [dict(page) for page in pages]})


def opener_returning(body, status=200):
    """A stand-in for the network. This is the `opener` seam `web_search` takes."""

    def opener(url, headers, payload, timeout):
        return status, body

    return opener


def post_returning(body, status=200):
    """A stand-in for the HTTP call, for tests that drive the route rather than the tool.

    The route builds its own `run_tool`, so there is no `opener` to hand it - the
    seam has to be `_post_json` itself, which takes the opener as its fifth
    argument and ignores it here.
    """

    def post(url, headers, payload, timeout, opener=None):
        return status, body

    return post


def key(name="EXA_API_KEY", value="test-key"):
    """`web_search` reads its key from the environment at call time."""
    return mock.patch.dict(os.environ, {name: value})


class ToolTests(unittest.TestCase):
    def search(self, body=None, status=200):
        with key():
            return web_search("python version", opener=opener_returning(body or exa_body(), status))


class TestWhatTheToolReturns(ToolTests):
    def test_the_pages_it_read_are_named_in_order(self):
        result = self.search()
        self.assertEqual(
            result["sources"],
            [
                {"title": "Release notes", "url": "https://docs.example.org/notes"},
                {"title": "Download page", "url": "https://example.org/download"},
                {"title": "Changelog", "url": "https://example.org/changelog"},
            ],
        )

    def test_the_text_is_not_numbered(self):
        """The loop owns citation numbers, so the tool must not start a second list.

        With both numbering, a turn that searched twice contained two independent
        "1."s, and "[2]" pointed at whichever list the reader had in mind.
        """
        text = self.search()["text"]
        self.assertIn("Release notes - https://docs.example.org/notes", text)
        self.assertNotIn("1. Release notes", text)
        self.assertNotIn("2. Download", text)

    def test_a_page_the_model_may_not_link_to_is_still_readable_but_unnumbered(self):
        """A scheme we will not put in an href still shows up as text."""
        body = exa_body(
            [
                {"title": "Hostile", "url": "javascript:alert(1)", "text": "not a page"},
                {"title": "Real", "url": "https://example.org/real", "text": "a page"},
                {"title": "Also not", "url": "ftp://example.org/file", "text": "not http"},
            ]
        )
        result = self.search(body)
        self.assertEqual(
            result["sources"],
            [{"title": "Real", "url": "https://example.org/real"}],
            "only an http(s) page may become a link",
        )
        self.assertIn("javascript:alert(1)", result["text"])

    def test_a_search_with_no_results_has_nothing_to_cite(self):
        result = self.search(json.dumps({"results": []}))
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["kept"], 0)
        self.assertIn("no results", result["text"])

    def test_a_rejected_key_leaves_nothing_to_cite(self):
        """The failed search is the whole reason the empty list matters.

        The old prompt failure was a model that could not search and answered from
        memory anyway. It must not be able to point at a page while it does it.
        """
        with key():
            with self.assertRaises(tools.ToolError):
                web_search("python version", opener=opener_returning("nope", status=401))
        result = tools.run_tool("web_search", {"query": "python version"}, opener=opener_returning("nope", 401))
        self.assertFalse(result["ok"])
        self.assertEqual(result["sources"], [])

    def test_the_calculator_has_no_pages(self):
        result = tools.run_tool("calculate", {"expression": "2 + 2"})
        self.assertEqual(result["text"], "4")
        self.assertEqual(result["sources"], [])

    def test_an_unknown_tool_has_no_pages(self):
        self.assertEqual(tools.run_tool("remember", {"text": "x"})["sources"], [])


class TestTheBudgetDoesNotInventASource(ToolTests):
    """A block cut in half is a page the model was never shown."""

    def blocks(self, count=3):
        return [
            {
                "title": "T{0}".format(index),
                "url": "https://example.org/{0}".format(index),
                "snippet": "s" * 300,
            }
            for index in range(count)
        ]

    def test_a_block_that_does_not_fit_is_omitted_whole_and_said_so(self):
        text, kept = tools._search_text(self.blocks(), budget=500)
        self.assertEqual(kept, 1)
        self.assertIn("T0", text)
        self.assertNotIn("T1", text, "the second block was cut into the text")
        self.assertIn("2 more result(s) omitted", text)

    def test_the_first_block_is_kept_however_long_it_is(self):
        """An empty result text would read as "the search found nothing"."""
        text, kept = tools._search_text(self.blocks(1), budget=10)
        self.assertEqual(kept, 1)
        self.assertIn("T0", text)

    def test_only_the_pages_inside_the_text_may_be_cited(self):
        results = self.blocks()
        self.assertEqual(len(tools._sources(results, 3)), 3)
        self.assertEqual(
            tools._sources(results, 1),
            [{"title": "T0", "url": "https://example.org/0"}],
        )
        self.assertEqual(tools._sources(results, 0), [])

    def test_the_default_budget_fits_three_full_results(self):
        """The cap is only allowed to bite on results bigger than we ever ask for.

        Every field is clipped before it is rendered, so this is the largest a
        result can be - and if three of those did not fit, a normal turn would
        quietly lose its third source to a budget nobody can see.
        """
        results = [
            {
                "title": "t" * tools.MAX_TITLE_CHARS,
                "url": "https://example.org/{0}".format("u" * 200),
                "snippet": "s" * tools.MAX_SNIPPET_CHARS,
            }
            for _ in range(tools.MAX_RESULTS)
        ]
        _, kept = tools._search_text(results)
        self.assertEqual(kept, tools.MAX_RESULTS)


PAGE_A = {"title": "A", "url": "https://a.test/page"}
PAGE_B = {"title": "B", "url": "https://b.test/page"}
PAGE_C = {"title": "C", "url": "https://c.test/page"}


class Scripted:
    """Stands in for the model: a queue of replies, and a record of what it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, tier, messages, log=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        text = self.replies.pop(0) if self.replies else ""
        response = {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return text, response, 0.02

    def prompt(self, index=-1):
        return "\n".join(m["content"] for m in self.calls[index]["messages"])

    def last_message(self):
        return self.calls[-1]["messages"][-1]["content"]


class Search:
    """Stands in for the search tool: returns the pages it is given."""

    def __init__(self, *batches, ok=True):
        self.batches = list(batches)
        self.ok = ok
        self.calls = []

    def __call__(self, name, args):
        self.calls.append({"name": name, "args": args})
        if name != "web_search":
            return {"name": name, "ok": True, "text": "4", "sources": [], "reason": ""}
        batch = self.batches.pop(0) if self.batches else []
        return {
            "name": name,
            "ok": self.ok,
            "text": "\n".join("{0} - {1}".format(p["title"], p["url"]) for p in batch)
            or "no results for that query.",
            "sources": batch if self.ok else [],
            "reason": "" if self.ok else "rejected_key",
        }


SEARCH = '{"tool": "web_search", "args": {"query": "when is the midterm"}}'


def run_loop(replies, *, run=None, max_steps=6):
    scripted = Scripted(replies)
    outcome = agent_reply(
        "what is the latest python",
        scripted,
        tier="super",
        max_steps=max_steps,
        run=run or Search([PAGE_A, PAGE_B, PAGE_C]),
    )
    return outcome, scripted


class TestTheLoopNumbersThem(unittest.TestCase):
    def test_the_pages_reach_the_reply_in_order(self):
        outcome, _ = run_loop([SEARCH, '{"final": "It is 3.13 [1]."}'])
        self.assertEqual(outcome["sources"], [PAGE_A, PAGE_B, PAGE_C])
        self.assertEqual(outcome["reply"], "It is 3.13 [1].")

    def test_the_model_is_told_which_numbers_it_may_use(self):
        _, scripted = run_loop([SEARCH, '{"final": "ok"}'])
        block = scripted.last_message()
        self.assertIn("SOURCES", block)
        self.assertIn("[1] A - https://a.test/page", block)
        self.assertIn("[3] C - https://c.test/page", block)
        self.assertIn("the only ones you may use", block)

    def test_the_numbers_continue_across_two_searches(self):
        """The defect this exists for: two searches, two lists of "1."."""
        second = [{"title": "D", "url": "https://d.test/page"}]
        outcome, scripted = run_loop(
            [SEARCH, SEARCH, '{"final": "Both [1] and [4]."}'],
            run=Search([PAGE_A, PAGE_B, PAGE_C], second),
        )
        self.assertEqual(
            outcome["sources"],
            [PAGE_A, PAGE_B, PAGE_C, {"title": "D", "url": "https://d.test/page"}],
        )
        self.assertIn("[4] D - https://d.test/page", scripted.last_message())
        self.assertIn(
            "[1]", outcome["reply"]
        ), "the first search's numbering must survive the second"
        self.assertIn("[4]", outcome["reply"])

    def test_a_page_seen_twice_keeps_its_first_number(self):
        outcome, scripted = run_loop(
            [SEARCH, SEARCH, '{"final": "See [1] and [3]."}'],
            run=Search([PAGE_A, PAGE_B], [PAGE_B, PAGE_C]),
        )
        self.assertEqual(
            outcome["sources"], [PAGE_A, PAGE_B, PAGE_C], "the second look renumbered a page"
        )
        block = scripted.last_message()
        self.assertIn("[3] C - https://c.test/page", block)
        self.assertNotIn("[4]", block, "the page we had already numbered got a second number")
        self.assertIn("[1]", outcome["reply"])

    def test_a_search_that_found_nothing_offers_no_numbers(self):
        outcome, scripted = run_loop(
            [SEARCH, '{"final": "I could not check that."}'], run=Search([])
        )
        self.assertEqual(outcome["sources"], [])
        self.assertIn("SOURCES - no new pages", scripted.last_message())

    def test_a_turn_that_never_searches_gets_no_sources_block(self):
        """A calculator step is not told about pages, because it has none."""
        _, scripted = run_loop(['{"tool": "calculate", "args": {"expression": "2 + 2"}}', '{"final": "4"}'])
        self.assertNotIn("SOURCES - ", scripted.prompt())

    def test_citations_are_capped(self):
        """The list is part of the reply, so a plan cannot hand over nine links."""

        def batch(prefix):
            return [
                {
                    "title": "{0}{1}".format(prefix, index),
                    "url": "https://{0}{1}.test/".format(prefix, index),
                }
                for index in range(4)
            ]

        outcome, _ = run_loop(
            [SEARCH, SEARCH, SEARCH, '{"final": "Done."}'],
            run=Search(batch("a"), batch("b"), batch("c")),
        )
        self.assertEqual(len(outcome["sources"]), MAX_CITATIONS)
        self.assertEqual([item["url"] for item in outcome["sources"]], [
            "https://a0.test/",
            "https://a1.test/",
            "https://a2.test/",
            "https://a3.test/",
            "https://b0.test/",
            "https://b1.test/",
        ])


class TestAMarkerWithNoPageBehindIt(unittest.TestCase):
    def test_a_marker_that_resolves_is_left_alone(self):
        outcome, _ = run_loop([SEARCH, '{"final": "It is 3.13 [2]. It shipped in June [1]."}'])
        self.assertEqual(outcome["reply"], "It is 3.13 [2]. It shipped in June [1].")
        self.assertEqual(outcome["notes"], [])

    def test_a_marker_that_resolves_to_nothing_is_removed(self):
        """The number a model invents is the one a student would click."""
        outcome, _ = run_loop([SEARCH, '{"final": "It is 3.13 [7]."}'])
        self.assertEqual(outcome["reply"], "It is 3.13.")
        self.assertIn("1 citation(s) pointed at no source", outcome["notes"])

    def test_the_removal_does_not_leave_a_gap_before_the_full_stop(self):
        outcome, _ = run_loop([SEARCH, '{"final": "It shipped in June [1], in 2024 [9]."}'])
        self.assertEqual(outcome["reply"], "It shipped in June [1], in 2024.")

    def test_a_turn_with_no_search_cannot_cite_anything(self):
        """No sources, no numbers - which is the whole point of the one-step path."""
        outcome, _ = run_loop(['{"final": "Python 3.13 is current [1]."}'])
        self.assertEqual(outcome["reply"], "Python 3.13 is current.")
        self.assertEqual(outcome["sources"], [])

    def test_a_reply_that_was_only_a_marker_is_not_emptied(self):
        """Silence is the one reply a voice assistant cannot give."""
        outcome, _ = run_loop([SEARCH, '{"final": "[8]"}'])
        self.assertEqual(outcome["reply"], "[8]")
        self.assertTrue(outcome["reply"].strip())

    def test_the_url_the_model_typed_is_not_a_source(self):
        """A URL in the reply is prose. Only the tool's list can become a link."""
        outcome, _ = run_loop(
            [SEARCH, '{"final": "See https://evil.test/pretend [1]."}']
        )
        self.assertEqual(
            [item["url"] for item in outcome["sources"]],
            ["https://a.test/page", "https://b.test/page", "https://c.test/page"],
        )
        self.assertNotIn("evil.test", json.dumps(outcome["sources"]))

    def test_the_one_step_path_still_returns_an_answer_and_no_pages(self):
        outcome, scripted = run_loop(["[1] Python 3.13"], max_steps=1)
        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(outcome["reply"], "Python 3.13")
        self.assertEqual(outcome["sources"], [])

    def test_the_rule_holds_without_the_loop(self):
        """`ground_reply` on its own, since the panel depends on the invariant."""
        citations = [{"n": 1, "title": "A", "url": "https://a.test"}]
        self.assertEqual(ground_reply("a [1] b", citations), ("a [1] b", 0))
        self.assertEqual(ground_reply("a [2] b", citations), ("a b", 1))
        self.assertEqual(ground_reply("a [2] [3] b", citations), ("a b", 2))
        self.assertEqual(ground_reply("[2]", citations), ("[2]", 1))


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_deploy.py for the reasoning)."""

    def __init__(self, path="/turn", body=None, command="POST"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"{command} {path} HTTP/1.0"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = message_from_string(
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


class LoopClient:
    """Routes to `general`, then plays the loop's steps in order."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append({"tier": tier, "messages": [dict(m) for m in messages]})
        if messages and "You are a router" in messages[0]["content"]:
            content = json.dumps(
                {"skill": "general", "args": {"request": "x"}, "needs_confirmation": False}
            )
        else:
            content = self.steps.pop(0) if self.steps else '{"final": "I could not check."}'
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            0.01,
        )


class TurnTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._token = server.AUTH_TOKEN
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.AUTH_TOKEN = ""

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.AUTH_TOKEN = self._token
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def turn(self, steps, *, search=None, status=200):
        self.client = LoopClient(steps)
        server.get_client = lambda: self.client
        endpoint = search or post_returning(exa_body())
        with key(), mock.patch.object(tools, "_post_json", endpoint):
            return HandlerStub(body={"transcript": "what is the latest python"}).send()

    def log_entries(self):
        path = self.root / "action-log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestOverHTTP(TurnTestCase):
    """The response the panel actually reads, driven through the real handler."""

    def test_the_pages_behind_the_reply_come_back_with_it(self):
        status, payload = self.turn([SEARCH, '{"final": "It is 3.13 [1]. It shipped in June [3]."}'])
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["sources"],
            [
                {"title": "Release notes", "url": "https://docs.example.org/notes"},
                {"title": "Download page", "url": "https://example.org/download"},
                {"title": "Changelog", "url": "https://example.org/changelog"},
            ],
        )
        self.assertEqual(payload["skill"], "general")
        self.assertEqual([item["tool"] for item in payload["tools"]], ["web_search"])
        # The markers the panel will resolve: [n] is sources[n - 1].
        self.assertIn("[1]", payload["reply"])
        self.assertIn("[3]", payload["reply"])

    def test_a_marker_with_no_page_behind_it_never_leaves_the_backend(self):
        status, payload = self.turn([SEARCH, '{"final": "It is 3.13 [1] and also [5]."}'])
        self.assertEqual(status, 200)
        self.assertEqual(payload["reply"], "It is 3.13 [1] and also.")
        self.assertEqual(len(payload["sources"]), 3, "[5] would index nothing")

    def test_a_failed_search_answers_with_the_pages_it_had_none_of(self):
        status, payload = self.turn(
            [SEARCH, '{"final": "I could not check that [1]."}'],
            search=post_returning("no", status=401),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["sources"], [])
        self.assertNotIn("[1]", payload["reply"])
        self.assertTrue(payload["tools"][0]["reason"], "the failure was not reported")

    def test_the_log_counts_citations_and_never_records_a_url(self):
        """Counts, never contents - a URL is somebody else's page, not our log."""
        self.turn([SEARCH, '{"final": "It is 3.13 [1]."}'])
        entries = self.log_entries()
        turn = [entry for entry in entries if entry["event"] == "turn"]
        self.assertEqual(len(turn), 1)
        self.assertEqual(turn[0]["citations"], 3)
        log_text = (self.root / "action-log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("docs.example.org", log_text)
        self.assertNotIn("Release notes", log_text)

    def test_an_unsearched_turn_reports_no_citations(self):
        self.turn(['{"final": "A ketone is a carbonyl group."}'])
        turn = [entry for entry in self.log_entries() if entry["event"] == "turn"]
        self.assertEqual(turn[0]["citations"], 0)

    def test_the_shared_response_shape_always_carries_a_sources_list(self):
        """Every path answers with the same keys - skills and approvals included.

        The panel reads this as one contract, so a path that forgot the field
        would be a runtime error in the UI rather than a missing list.
        """
        payload = HandlerStub()._turn_payload({"reply": "hi"}, session={})
        self.assertEqual(payload["sources"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
