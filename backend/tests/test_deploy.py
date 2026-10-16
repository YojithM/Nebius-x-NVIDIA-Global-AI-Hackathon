#!/usr/bin/env python3
"""Tests for the Step 7 deployment surface: the token gate and the CORS origin.

    python3 backend/tests/test_deploy.py

These cover the two things that are different the moment the backend is not on
localhost, and they are the two things that are dangerous to get wrong:

1. **The token gate.** A deployed backend spends the student's Nebius credit, so
   an open URL is an open wallet. Every route except `GET /health` must refuse a
   request with no token, and refuse it *before* any model call or any write -
   a 401 that still cost a request, or still deleted a quiz, is not a gate.
2. **The CORS origin.** The extension origin is allowed from anywhere; the panel
   served as a page is allowed only if we were built for that exact origin. A
   typo here is the difference between "works" and "silently blocked by Chrome".
"""

import contextlib
import email.message
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import Question, QuizStore  # noqa: E402

TOKEN = "a" * 64

# Settings the server reads at import time, so a reload test must clear them
# from the real environment or the .env file under test would not be the source.
SAVED_KEYS = (
    "CADENCE_TOKEN",
    "CADENCE_ALLOW_ORIGIN",
    "CADENCE_HOST",
    "CADENCE_PORT",
)


class HandlerStub(server.Handler):
    """The real handler, driven without a socket.

    Returns the raw response so tests can read the CORS headers, not just the
    body - a gate that returns the right JSON with the wrong headers still fails
    in a browser.
    """

    def __init__(self, path, body=None, command="GET", origin=None, authorization=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"{command} {path} HTTP/1.0"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        headers = [f"Content-Length: {len(raw)}", "Content-Type: application/json"]
        if origin:
            headers.append(f"Origin: {origin}")
        if authorization is not None:
            headers.append(f"Authorization: {authorization}")
        self.headers = email.message_from_string("\r\n".join(headers) + "\r\n\r\n")
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        getattr(self, f"do_{self.command}")()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, body = text.partition("\r\n\r\n")
        status = int(head.split(" ")[1])
        payload = json.loads(body) if body.strip() else None
        return status, payload, head


class ScriptedClient:
    """Counts model calls so 'the gate refused before spending anything' is a fact."""

    def __init__(self):
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append(tier)
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": "Hi."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            0.01,
        )


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._token = server.AUTH_TOKEN
        self._allow = server.ALLOW_ORIGIN
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        # Default: local mode. Individual tests turn the gate on.
        server.AUTH_TOKEN = ""
        server.ALLOW_ORIGIN = ""
        self.client = ScriptedClient()
        server.get_client = lambda: self.client

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.AUTH_TOKEN = self._token
        server.ALLOW_ORIGIN = self._allow
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def seed_quiz(self):
        return server.PROGRESS.start(
            "organic chemistry",
            "topic",
            [Question(text="What is an SN1 reaction?", expected="a substitution")],
            started="2026-10-14",
        )


class TestTheTokenGate(DeployTestCase):
    def test_local_mode_stays_open_so_development_is_unchanged(self):
        status, payload, _ = HandlerStub("/progress").send()
        self.assertEqual(status, 200)
        self.assertIn("sessions", payload)

    def test_with_a_token_set_a_request_without_one_is_refused(self):
        server.AUTH_TOKEN = TOKEN
        status, payload, _ = HandlerStub("/progress").send()
        self.assertEqual(status, 401)
        self.assertIn("bearer token", payload["error"])

    def test_health_stays_open_even_with_a_token_set(self):
        """It is how a deploy is checked, and it carries no student data."""
        server.AUTH_TOKEN = TOKEN
        status, payload, _ = HandlerStub("/health").send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "cadence-backend")

    def test_the_right_token_is_accepted(self):
        server.AUTH_TOKEN = TOKEN
        status, _, _ = HandlerStub(
            "/progress", authorization=f"Bearer {TOKEN}"
        ).send()
        self.assertEqual(status, 200)

    def test_a_wrong_scheme_or_token_is_refused(self):
        server.AUTH_TOKEN = TOKEN
        for header in ["Bearer wrong", f"Basic {TOKEN}", TOKEN, "Bearer ", ""]:
            with self.subTest(header=header):
                status, _, _ = HandlerStub("/progress", authorization=header).send()
                self.assertEqual(status, 401)

    def test_a_refused_turn_makes_no_model_call(self):
        """The gate must come before the router, or 'refused' still costs money."""
        server.AUTH_TOKEN = TOKEN
        self.client.calls.clear()
        status, _, _ = HandlerStub(
            "/turn", body={"transcript": "what is a ketone?"}, command="POST"
        ).send()
        self.assertEqual(status, 401)
        self.assertEqual(self.client.calls, [], "a 401 spent a model call")

    def test_a_refused_delete_deletes_nothing(self):
        server.AUTH_TOKEN = TOKEN
        session = self.seed_quiz()
        status, _, _ = HandlerStub(
            f"/progress?session={session.id}", command="DELETE"
        ).send()
        self.assertEqual(status, 401)
        self.assertIsNotNone(server.PROGRESS.get(session.id))

    def test_a_refused_delete_all_deletes_nothing(self):
        server.AUTH_TOKEN = TOKEN
        self.seed_quiz()
        status, _, _ = HandlerStub("/progress?all=1", command="DELETE").send()
        self.assertEqual(status, 401)
        self.assertEqual(len(server.PROGRESS.ordered()), 1)

    def test_the_attempt_is_logged_without_the_presented_value(self):
        server.AUTH_TOKEN = TOKEN
        HandlerStub("/progress", authorization="Bearer hunter2").send()
        text = self.log_text()
        self.assertIn("unauthorized", text)
        self.assertNotIn("hunter2", text)

    def test_allowed_methods_include_authorization_for_preflight(self):
        """Without this Chrome drops the real request before sending the token."""
        status, _, head = HandlerStub("/turn", command="OPTIONS").send()
        self.assertEqual(status, 204)
        self.assertIn("Authorization", head)


class TestTheCorsOrigin(DeployTestCase):
    def test_the_extension_origin_is_allowed_from_anywhere(self):
        origin = "chrome-extension://" + "a" * 32
        _, _, head = HandlerStub("/progress", origin=origin).send()
        self.assertIn(f"Access-Control-Allow-Origin: {origin}", head)

    def test_localhost_is_allowed(self):
        _, _, head = HandlerStub("/health", origin="http://localhost:5173").send()
        self.assertIn("Access-Control-Allow-Origin: http://localhost:5173", head)

    def test_a_configured_page_origin_is_allowed(self):
        server.ALLOW_ORIGIN = "https://cadence.example.com"
        _, _, head = HandlerStub(
            "/health", origin="https://cadence.example.com"
        ).send()
        self.assertIn("Access-Control-Allow-Origin: https://cadence.example.com", head)

    def test_a_trailing_slash_in_the_configured_origin_still_matches(self):
        server.ALLOW_ORIGIN = "https://cadence.example.com/"
        _, _, head = HandlerStub(
            "/health", origin="https://cadence.example.com"
        ).send()
        self.assertIn("Access-Control-Allow-Origin: https://cadence.example.com", head)

    def test_an_unrelated_origin_gets_no_header(self):
        server.ALLOW_ORIGIN = "https://cadence.example.com"
        _, _, head = HandlerStub("/health", origin="https://evil.example").send()
        self.assertNotIn("Access-Control-Allow-Origin", head)

    def test_the_origin_is_never_widened_to_a_wildcard(self):
        """A wildcard would let any page read the student's memory."""
        for origin in ["https://evil.example", "null", "http://10.0.0.5"]:
            with self.subTest(origin=origin):
                _, _, head = HandlerStub("/progress", origin=origin).send()
                self.assertNotIn("Access-Control-Allow-Origin: *", head)


class TestSettingsComeFromDotEnv(DeployTestCase):
    """A deploy setting that is only written to .env must still take effect.

    The import-time settings - bind address, port, token, allowed origin - are
    read while the module loads, so loading .env after them silently ignored it.
    For the token that failure is public rather than loud: the server would come
    up on a host with the gate off, because the token had "not been set".

    Reloading the module is how this is checked honestly: it is exactly what
    `python3 backend/server.py` does, so the test exercises the real ordering
    instead of a copy of it.
    """

    @contextlib.contextmanager
    def started_with_env_file(self, body):
        """Load the module the way `python3 backend/server.py` loads it.

        Reloading is the honest check here. The settings under test are read
        while the module loads, so a test that poked the globals after import
        would prove nothing about the ordering this is guarding - and the
        restoring reload in the finally is why the module is yielded instead of
        returned: `reload` mutates the one module object in place.
        """
        import nemotron

        path = self.root / ".env"
        path.write_text(body, encoding="utf-8")
        original = nemotron.ENV_PATH
        saved = {key: os.environ.pop(key, None) for key in SAVED_KEYS}
        nemotron.ENV_PATH = path
        try:
            reloaded = importlib.reload(server)
            # reload() re-executes the module, which re-reads EVERY module-level
            # setting - including the three that must never be the real ones. Left
            # alone, the tests below write an "unauthorized" line into the
            # checkout's own backend/logs/action-log.jsonl and read the student's
            # real progress/ instead of the fixtures setUp installed. The
            # settings actually under test (token, origin, host) are deliberately
            # left exactly as the reload read them from the file.
            reloaded.MEMORY = MemoryStore(self.root / "memory").load()
            reloaded.PROGRESS = QuizStore(self.root / "progress").load()
            reloaded.LOG_PATH = self.root / "action-log.jsonl"
            yield reloaded
        finally:
            nemotron.ENV_PATH = original
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value
            importlib.reload(server)

    def test_a_token_in_the_env_file_is_honoured(self):
        with self.started_with_env_file(f"CADENCE_TOKEN={TOKEN}\n") as reloaded:
            self.assertEqual(reloaded.AUTH_TOKEN, TOKEN)
            # It is the gate the handler actually consults, not just a constant.
            status, _, _ = HandlerStub("/progress").send()
            self.assertEqual(status, 401)

    def test_the_other_deploy_settings_come_from_the_env_file_too(self):
        with self.started_with_env_file(
            f"CADENCE_TOKEN={TOKEN}\n"
            "CADENCE_ALLOW_ORIGIN=https://cadence.example.com/\n"
            "CADENCE_HOST=0.0.0.0\n"
        ) as reloaded:
            self.assertEqual(reloaded.ALLOW_ORIGIN, "https://cadence.example.com")
            self.assertEqual(reloaded.HOST, "0.0.0.0")

    def test_an_env_file_without_a_token_leaves_local_mode_alone(self):
        with self.started_with_env_file("# nothing set\n") as reloaded:
            self.assertEqual(reloaded.AUTH_TOKEN, "")
            self.assertEqual(reloaded.HOST, "127.0.0.1")
            status, _, _ = HandlerStub("/progress").send()
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
