#!/usr/bin/env python3
"""Tests for the Token Factory client.

Hermetic: the HTTP transport is replaced, so these assert on the request we
*would* send. That is the only way to pin down payload details (like the
per-tier thinking switch) without paying for calls on every test run.

    python3 backend/tests/test_nemotron.py
"""

import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemotron import (  # noqa: E402
    NebiusClient,
    NebiusError,
    cost_usd,
    load_config,
    reply_text,
)

CONFIG = load_config()


class CapturingClient(NebiusClient):
    """A client that records the payload instead of sending it."""

    def __init__(self, response=None):
        os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-used")
        super().__init__(CONFIG)
        self.sent = []
        self.response = response or {
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def _request(self, method, path, payload=None):
        self.sent.append({"method": method, "path": path, "payload": payload})
        return self.response, 0.01


class TestPayload(unittest.TestCase):
    def test_nano_disables_thinking(self):
        """Nano routes; reasoning there costs tokens and buys nothing."""
        client = CapturingClient()
        client.chat("nano", [{"role": "user", "content": "route this"}])
        payload = client.sent[0]["payload"]
        self.assertEqual(payload["chat_template_kwargs"]["enable_thinking"], False)

    def test_super_keeps_thinking_on(self):
        """Super explains; the reasoning trace measurably improves the answer."""
        client = CapturingClient()
        client.chat("super", [{"role": "user", "content": "explain this"}])
        payload = client.sent[0]["payload"]
        self.assertNotIn("chat_template_kwargs", payload)

    def test_thinking_can_be_overridden_by_extra(self):
        client = CapturingClient()
        client.chat(
            "super",
            [{"role": "user", "content": "x"}],
            extra={"chat_template_kwargs": {"enable_thinking": False}},
        )
        payload = client.sent[0]["payload"]
        self.assertEqual(payload["chat_template_kwargs"]["enable_thinking"], False)

    def test_model_id_comes_from_the_tier(self):
        client = CapturingClient()
        client.chat("ultra", [{"role": "user", "content": "x"}])
        self.assertEqual(client.sent[0]["payload"]["model"], CONFIG["tiers"]["ultra"]["model"])

    def test_output_token_budget_is_applied(self):
        client = CapturingClient()
        client.chat("super", [{"role": "user", "content": "x"}])
        self.assertEqual(
            client.sent[0]["payload"]["max_tokens"],
            CONFIG["budgets"]["max_output_tokens_per_request"],
        )

    def test_explicit_max_tokens_wins(self):
        client = CapturingClient()
        client.chat("nano", [{"role": "user", "content": "x"}], max_tokens=42)
        self.assertEqual(client.sent[0]["payload"]["max_tokens"], 42)

    def test_unknown_tier_is_rejected_before_any_request(self):
        client = CapturingClient()
        with self.assertRaises(NebiusError):
            client.chat("gigantic", [{"role": "user", "content": "x"}])
        self.assertEqual(client.sent, [])


class TestTransportFailures(unittest.TestCase):
    """A flaky socket must not kill the request.

    A mid-read reset arrives as ConnectionResetError, which is *not* a URLError,
    so it escaped the client entirely and the server closed the connection with
    no response - observed once on a live page-based turn. Everything the
    network can do to us has to surface as a NebiusError the handler can turn
    into a 502.
    """

    def setUp(self):
        os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-used")
        self.client = NebiusClient(CONFIG)
        self._urlopen = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    def opens_raising(self, error):
        def fake_urlopen(*args, **kwargs):
            raise error

        urllib.request.urlopen = fake_urlopen

    def test_a_reset_mid_read_is_wrapped(self):
        self.opens_raising(ConnectionResetError(54, "Connection reset by peer"))
        with self.assertRaises(NebiusError) as caught:
            self.client.chat("super", [{"role": "user", "content": "x"}])
        self.assertIn("Network error", str(caught.exception))

    def test_a_timeout_is_wrapped(self):
        self.opens_raising(TimeoutError("timed out"))
        with self.assertRaises(NebiusError):
            self.client.chat("super", [{"role": "user", "content": "x"}])

    def test_an_http_error_still_reports_the_status(self):
        import email

        error = urllib.error.HTTPError(
            "https://api.tokenfactory.nebius.com/v1/chat/completions",
            429,
            "Too Many Requests",
            email.message_from_string(""),
            None,
        )
        error.read = lambda: b'{"error": "rate limited"}'
        self.opens_raising(error)
        with self.assertRaises(NebiusError) as caught:
            self.client.chat("super", [{"role": "user", "content": "x"}])
        self.assertIn("429", str(caught.exception))


class TestReplyText(unittest.TestCase):
    def test_plain_content(self):
        self.assertEqual(reply_text({"choices": [{"message": {"content": "hi"}}]}), "hi")

    def test_content_parts_are_joined(self):
        response = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        self.assertEqual(reply_text(response), "ab")

    def test_null_content_becomes_empty_string(self):
        self.assertEqual(reply_text({"choices": [{"message": {"content": None}}]}), "")

    def test_no_choices(self):
        self.assertEqual(reply_text({}), "")


class TestCost(unittest.TestCase):
    def test_nano_cost_matches_the_published_price(self):
        cost = cost_usd(CONFIG, "nano", {"prompt_tokens": 1000, "completion_tokens": 1000})
        self.assertAlmostEqual(cost, (1000 * 0.06 + 1000 * 0.24) / 1_000_000, places=12)

    def test_ultra_is_the_expensive_tier(self):
        usage = {"prompt_tokens": 10_000, "completion_tokens": 10_000}
        self.assertGreater(
            cost_usd(CONFIG, "ultra", usage), cost_usd(CONFIG, "super", usage) * 3
        )

    def test_missing_usage_is_zero_not_a_crash(self):
        self.assertEqual(cost_usd(CONFIG, "super", {}), 0.0)


class TestPortResolution(unittest.TestCase):
    """The PORT=0 trap: a generic env var must not silently move the server."""

    def _resolve(self, env):
        import importlib.util

        for key in ("PORT", "CADENCE_PORT"):
            os.environ.pop(key, None)
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location(
            "cadence_server", Path(__file__).resolve().parent.parent / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._resolve_port()

    def test_cases(self):
        for env, expected in [
            ({}, 8787),
            ({"PORT": "0"}, 8787),
            ({"PORT": "abc"}, 8787),
            ({"PORT": ""}, 8787),
            ({"PORT": "9000"}, 9000),
            ({"CADENCE_PORT": "9111"}, 9111),
            ({"CADENCE_PORT": "9111", "PORT": "0"}, 9111),
        ]:
            with self.subTest(env=env):
                self.assertEqual(self._resolve(env), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
