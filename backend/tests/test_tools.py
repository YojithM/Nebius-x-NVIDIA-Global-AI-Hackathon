#!/usr/bin/env python3
"""Tests for the agent's tools: search, arithmetic, and the refusals.

    python3 backend/tests/test_tools.py

No network anywhere in this file. The provider endpoints are replaced by a stand-in
that records the request and replays a recorded response body, so every branch that
matters - a rejected key, a rate limit, a provider that answers with something that
is not JSON - is testable without a key and without a live provider.

Two things here are security properties rather than features:

1. **The key never comes back out.** It goes into a header, and the text the model
   and the logs see is checked for it. A tool that echoes a credential into a prompt
   is a leak, and it is one string comparison to notice.
2. **The evaluator has no way to reach anything but numbers.** `calculate` is a
   whitelist AST walk, not `eval`, and the tests below are the attempts a model
   would actually make: import, attribute access, a comprehension, a lambda. None of
   them get a name resolved, because the grammar has no name lookup to resolve.
"""

import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import (  # noqa: E402
    MAX_EXPRESSION_CHARS,
    MAX_QUERY_CHARS,
    MAX_SNIPPET_CHARS,
    PROVIDERS,
    TOOL_NAMES,
    ToolError,
    calculate,
    run_tool,
    search_available,
    search_provider,
    tool_cards,
    tool_help,
    web_search,
)

SECRET = "tvly-THIS-IS-THE-KEY"


@contextlib.contextmanager
def keys(**values):
    """Set the provider keys for one block, then put the environment back."""
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FakeEndpoint:
    """Stands in for a provider: records the request, replays a body."""

    def __init__(self, body="", status=200):
        self.body = body if isinstance(body, str) else json.dumps(body)
        self.status = status
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        return self.status, self.body


def exa_body(*titles):
    return json.dumps(
        {
            "results": [
                {"title": title, "url": "https://example.org/{0}".format(i), "text": "snippet {0}".format(i)}
                for i, title in enumerate(titles, 1)
            ]
        }
    )


class SearchTests(unittest.TestCase):
    def test_without_a_key_the_tool_says_so_instead_of_searching(self):
        with keys(EXA_API_KEY=None, TAVILY_API_KEY=None):
            self.assertFalse(search_available())
            self.assertIsNone(search_provider())
            with self.assertRaises(ToolError) as caught:
                web_search("who won the 2026 world cup")
        self.assertEqual(caught.exception.code, "no_key")
        message = str(caught.exception)
        self.assertIn("EXA_API_KEY", message)
        self.assertIn("TAVILY_API_KEY", message)
        # The model is told what to do instead of just what failed - and, since the
        # panel showed the alternative, told *not* to answer from memory. The first
        # version of this message said "answer from what you already know and say you
        # could not check the web", which is exactly what the model then did: a
        # current-fact question answered with a stale version number and a disclaimer.
        self.assertIn("could not check", message)
        self.assertIn("Do not fill the gap from memory", message)

    def test_a_clean_search_returns_titles_urls_and_snippets(self):
        endpoint = FakeEndpoint(exa_body("Nebius Token Factory", "Nemotron 3"))
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            result = web_search("nebius token factory", opener=endpoint)
        self.assertEqual(result["provider"], "exa")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "Nebius Token Factory")
        self.assertTrue(result["results"][0]["url"].startswith("https://"))
        self.assertIn("snippet 1", result["text"])
        self.assertIn("Nebius Token Factory - https://example.org/1", result["text"])
        # The tool does not number its results. The loop owns citation numbers,
        # and two independent lists of 1s over the same pages is how "[2]" ends up
        # pointing at a different page depending on who is reading.
        self.assertNotIn("1. Nebius", result["text"])
        # What the reply is allowed to cite, in the order it will be numbered.
        self.assertEqual(
            result["sources"],
            [
                {"title": "Nebius Token Factory", "url": "https://example.org/1"},
                {"title": "Nemotron 3", "url": "https://example.org/2"},
            ],
        )
        self.assertEqual(result["kept"], 2)
        # The key went out in a header and is in none of the text we keep.
        self.assertEqual(endpoint.calls[0]["headers"]["x-api-key"], SECRET)
        self.assertNotIn(SECRET, result["text"])
        self.assertNotIn(SECRET, json.dumps(result["results"]))
        self.assertNotIn("api_key", json.dumps(endpoint.calls[0]["payload"]))
        self.assertEqual(endpoint.calls[0]["url"], PROVIDERS["exa"]["url"])

    def test_tavily_is_used_when_it_is_the_key_that_exists(self):
        endpoint = FakeEndpoint(
            {"results": [{"title": "A page", "url": "https://a.test", "content": "text"}]}
        )
        with keys(EXA_API_KEY=None, TAVILY_API_KEY=SECRET):
            self.assertEqual(search_provider(), "tavily")
            result = web_search("anything", opener=endpoint)
        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(endpoint.calls[0]["headers"]["Authorization"], "Bearer " + SECRET)
        # Tavily's shape: max_results, and raw page content left off on purpose -
        # whole pages would blow the prompt budget on the first search.
        self.assertEqual(endpoint.calls[0]["payload"]["max_results"], 3)
        self.assertFalse(endpoint.calls[0]["payload"]["include_raw_content"])

    def test_exa_wins_when_both_keys_are_set(self):
        with keys(EXA_API_KEY="a", TAVILY_API_KEY="b"):
            self.assertEqual(search_provider(), "exa")

    def test_an_empty_key_counts_as_no_key(self):
        with keys(EXA_API_KEY="   ", TAVILY_API_KEY=None):
            self.assertFalse(search_available())

    def test_a_rejected_key_is_reported_as_a_key_problem(self):
        endpoint = FakeEndpoint({"detail": "invalid api key"}, status=401)
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            with self.assertRaises(ToolError) as caught:
                web_search("q", opener=endpoint)
        self.assertEqual(caught.exception.code, "rejected_key")
        self.assertIn("EXA_API_KEY", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception), "the key must not travel in an error")

    def test_rate_limits_and_provider_errors_are_told_apart(self):
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            for status, code in ((429, "rate_limited"), (500, "provider_error")):
                with self.subTest(status=status):
                    with self.assertRaises(ToolError) as caught:
                        web_search("q", opener=FakeEndpoint("{}", status=status))
                    self.assertEqual(caught.exception.code, code)

    def test_a_provider_that_is_unreachable_says_so(self):
        def broken(url, headers, payload, timeout):
            raise URLError("nodename nor servname provided")

        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            with self.assertRaises(ToolError) as caught:
                web_search("q", opener=broken)
        self.assertEqual(caught.exception.code, "unreachable")

    def test_a_response_that_is_not_json_is_not_guessed_at(self):
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            with self.assertRaises(ToolError) as caught:
                web_search("q", opener=FakeEndpoint("<html>502</html>"))
        self.assertEqual(caught.exception.code, "bad_response")

    def test_no_results_is_a_result_not_an_error(self):
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            result = web_search("q", opener=FakeEndpoint({"results": []}))
        self.assertEqual(result["results"], [])
        self.assertIn("no results", result["text"])

    def test_a_long_query_and_a_long_snippet_are_both_bounded(self):
        long_snippet = "x" * (MAX_SNIPPET_CHARS + 500)
        endpoint = FakeEndpoint(
            {"results": [{"title": "t", "url": "u", "text": long_snippet}]}
        )
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            result = web_search("q" * (MAX_QUERY_CHARS + 100), opener=endpoint)
        sent = endpoint.calls[0]["payload"]["query"]
        self.assertEqual(len(sent), MAX_QUERY_CHARS)
        self.assertLessEqual(len(result["results"][0]["snippet"]), MAX_SNIPPET_CHARS + 3)
        self.assertTrue(result["results"][0]["snippet"].endswith("..."))

    def test_an_empty_query_is_refused_before_any_request(self):
        endpoint = FakeEndpoint(exa_body("x"))
        with keys(EXA_API_KEY=SECRET, TAVILY_API_KEY=None):
            with self.assertRaises(ToolError) as caught:
                web_search("   ", opener=endpoint)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(endpoint.calls, [], "a refused search must not reach the network")


class CalculateTests(unittest.TestCase):
    def test_arithmetic_a_student_actually_asks_for(self):
        for expression, expected in (
            ("2 + 2", "4"),
            ("(84 + 91) / 2", "87.5"),
            ("85 * 0.25", "21.25"),
            ("100 - 37", "63"),
            ("10 / 4", "2.5"),
            ("7 % 3", "1"),
            ("2 ** 10", "1024"),
            ("sqrt(144)", "12"),
            ("round(2.345, 2)", "2.35"),
            ("mean([84, 91, 78])", "84.33333333"),
            ("sum([1, 2, 3])", "6"),
            ("min(4, 2, 9)", "2"),
            ("pi", "3.141592654"),
            ("radians(180)", "3.141592654"),
            ("abs(0 - 7)", "7"),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression)["text"], expected)

    def test_a_third_is_formatted_like_a_person_writes_it(self):
        self.assertEqual(calculate("1 / 3")["text"], "0.3333333333")

    def test_the_refusals_a_model_would_actually_try(self):
        attempts = (
            '__import__("os").system("rm -rf /")',
            "os.getcwd()",
            "open('/etc/passwd').read()",
            "(1).__class__.__mro__",
            "[x for x in range(10)]",
            "(lambda: 1)()",
            "sorted([3, 1])",
            "'a' + 'b'",
            "True",
            "print(1)",
            "exec('x = 1')",
        )
        for expression in attempts:
            with self.subTest(expression=expression):
                with self.assertRaises(ToolError):
                    calculate(expression)

    def test_the_shapes_that_could_make_a_machine_stop_responding(self):
        with self.assertRaises(ToolError):
            calculate("9 ** 9 ** 9")
        with self.assertRaises(ToolError):
            calculate("1 / 0")
        with self.assertRaises(ToolError):
            calculate("sqrt(0 - 1)")
        # Nesting, three ways it arrives, and each one is stopped by a different
        # guard. Short-but-deep is the depth guard (this expression is 240 chars and
        # nests 61 levels). Deep parentheses are harmless *and* are not AST nodes, so
        # the depth guard never sees them - the length cap stops a storm of them
        # first, and a sub-300-char version of them just evaluates.
        with self.assertRaises(ToolError) as caught:
            calculate("1 + " * 60 + "1")
        self.assertIn("nest", str(caught.exception))
        with self.assertRaises(ToolError):
            calculate("2 + " * 400 + "2")
        with self.assertRaises(ToolError):
            calculate("(" * 500 + "1" + ")" * 500)
        with self.assertRaises(ToolError):
            calculate("")
        with self.assertRaises(ToolError):
            calculate("   ")
        with self.assertRaises(ToolError):
            calculate("2 +")
        with self.assertRaises(ToolError):
            calculate("y" * (MAX_EXPRESSION_CHARS + 1))

    def test_parentheses_give_grouping_and_nothing_else(self):
        self.assertEqual(calculate("(" * 60 + "1" + ")" * 60)["text"], "1")
        self.assertEqual(calculate("(2 + 3) * 4")["text"], "20")

    def test_a_huge_but_legal_exponent_still_works(self):
        self.assertEqual(calculate("2 ** 100")["text"], str(2 ** 100))

    def test_mean_needs_at_least_one_number(self):
        with self.assertRaises(ToolError):
            calculate("mean([])")


class RunToolTests(unittest.TestCase):
    def test_an_unknown_tool_comes_back_as_a_result_naming_the_real_ones(self):
        result = run_tool("remember", {"text": "the exam moved"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_tool")
        for name in TOOL_NAMES:
            self.assertIn(name, result["text"])

    def test_a_failing_tool_never_raises_out_of_run_tool(self):
        with keys(EXA_API_KEY=None, TAVILY_API_KEY=None):
            result = run_tool("web_search", {"query": "anything"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_key")
        self.assertTrue(result["text"].startswith("TOOL FAILED:"))

    def test_calculate_through_run_tool(self):
        result = run_tool("calculate", {"expression": "3 * 3"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "9")
        self.assertEqual(result["reason"], "")

    def test_arguments_that_are_not_an_object_do_not_reach_a_handler(self):
        for args in (None, "2 + 2", ["2 + 2"]):
            with self.subTest(args=args):
                result = run_tool("calculate", args)
                self.assertFalse(result["ok"])

    def test_the_log_code_is_a_bucket_and_not_a_sentence(self):
        """The log records reasons, never the expression or the message."""
        result = run_tool("calculate", {"expression": "1 / 0"})
        self.assertFalse(result["ok"])
        self.assertNotIn("/", result["reason"])
        self.assertNotIn(" ", result["reason"])


class ToolCardTests(unittest.TestCase):
    def test_the_cards_and_the_handlers_are_the_same_set(self):
        self.assertEqual(tuple(card["name"] for card in tool_cards()), TOOL_NAMES)
        self.assertEqual(
            sorted(TOOL_NAMES), ["calculate", "propose_skill", "web_search"]
        )

    def test_the_skill_card_says_it_does_not_save(self):
        """A card that let the model think it had saved would be a lie in the prompt."""
        card = [c for c in tool_cards() if c["name"] == "propose_skill"][0]
        self.assertIn("does not save it", card["when"])
        self.assertIn("never say a skill has been saved", card["when"])
        self.assertIn("may only say things", card["when"])
        self.assertIn("action must be prompt", " ".join(card["when"].split()))
        # The file format has to be in the card: the model is not shown a SKILL.md
        # anywhere else, and a draft that cannot load wastes the student's turn.
        for section in ("## When to Use", "## Input", "## Prompt"):
            self.assertIn(section, card["when"])

    def test_the_prompt_help_is_stable_and_json(self):
        payload = json.loads(tool_help())
        self.assertEqual([card["name"] for card in payload["tools"]], list(TOOL_NAMES))
        self.assertEqual(tool_help(), tool_help(), "the prompt must not drift between calls")

    def test_the_calculator_card_promises_only_what_the_evaluator_can_do(self):
        card = [c for c in tool_cards() if c["name"] == "calculate"][0]
        self.assertIn("cannot read files", card["when"])
        self.assertNotIn("sandbox", card["when"].lower())
        # The first version of this card sold "date gaps". The grammar has no dates
        # in it, and the live measurement below is why that promise had to go: a
        # model told it could compute a countdown computed the wrong one.
        self.assertNotIn("date gaps", card["when"])
        self.assertIn("no dates", card["when"])

    def test_dates_are_not_in_the_grammar(self):
        for expression in ('"2026-11-12" - "2026-10-15"', "date(2026, 11, 12)"):
            with self.subTest(expression=expression):
                with self.assertRaises(ToolError):
                    calculate(expression)


if __name__ == "__main__":
    unittest.main(verbosity=2)
