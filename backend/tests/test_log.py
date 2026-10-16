#!/usr/bin/env python3
"""Tests for Step 10's action-log view: `GET /log`, and the tail reader under it.

    python3 backend/tests/test_log.py

The log has existed since Step 8 - the scheduler writes a line for the day's
brief, every turn writes one, and the rule from the start has been counts and
events rather than the student's words. What Step 10 adds is a way to *look* at
it, which changes the stakes: a file nobody reads can be sloppy, and a file on
screen cannot.

Three properties, each a way a log viewer goes wrong:

1. **A tail is not a read.** The file grows forever by design, so the reader
   starts at the end. The test proves the shape (newest first, the requested
   number of lines, and a note when older lines were not read) rather than the
   buffer arithmetic.
2. **A torn line is normal.** Every request appends, so a half-written last line
   is a thing that happens, not a thing to fail on. It is skipped, and the lines
   around it are still shown.
3. **The log carries no student words.** That rule was already enforced at the
   write site; this is the test that reads it back, because the endpoint is what
   makes it a promise worth keeping - anyone reviewing a deploy can now read the
   whole thing.
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
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402


def line(event, **fields):
    return json.dumps({"ts": "2026-10-16T09:00:00", "event": event, **fields})


class TestTheTailReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "action-log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.path

    def test_the_newest_line_comes_first(self):
        self.write(line("one"), line("two"), line("three"))
        entries, note = server.log_tail(limit=2, path=self.path)
        self.assertEqual([entry["event"] for entry in entries], ["three", "two"])
        self.assertEqual(note, "")

    def test_the_limit_is_honoured_and_capped(self):
        self.write(*[line(f"e{index}") for index in range(500)])
        self.assertEqual(len(server.log_tail(limit=3, path=self.path)[0]), 3)
        capped = server.log_tail(limit=10_000, path=self.path)[0]
        self.assertLessEqual(len(capped), server.LOG_TAIL_LINES)

    def test_a_nonsensical_limit_still_answers(self):
        self.write(line("one"))
        for limit in ("abc", None, -5):
            with self.subTest(limit=limit):
                entries, _ = server.log_tail(limit=limit, path=self.path)
                self.assertEqual(len(entries), 1)

    def test_only_the_end_of_a_long_log_is_read(self):
        """The note is how the viewer knows there is more on disk."""
        pad = "x" * 200
        rows = [
            '{"ts": "2026-10-16T09:00:00", "event": "e%d", "pad": "%s"}' % (index, pad)
            for index in range(3000)
        ]
        self.write(*rows)
        self.assertGreater(self.path.stat().st_size, server.LOG_TAIL_BYTES)
        entries, note = server.log_tail(limit=5, path=self.path)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0]["event"], "e2999", "the tail is not the end")
        self.assertIn("end of the log", note)

    def test_a_torn_last_line_is_skipped_not_fatal(self):
        with self.path.open("w", encoding="utf-8") as handle:
            handle.write(line("complete") + "\n")
            handle.write('{"ts": "2026-10-16T09:00:01", "event": "half')
        entries, _ = server.log_tail(limit=10, path=self.path)
        self.assertEqual([entry["event"] for entry in entries], ["complete"])

    def test_a_line_that_is_not_json_is_skipped(self):
        self.write(line("good"), "not json at all", line("also-good"))
        entries, _ = server.log_tail(limit=10, path=self.path)
        self.assertEqual([entry["event"] for entry in entries], ["also-good", "good"])

    def test_a_missing_log_is_an_empty_list_with_a_reason(self):
        entries, note = server.log_tail(limit=10, path=Path(self.tmp.name) / "nope.jsonl")
        self.assertEqual(entries, [])
        self.assertIn("no action log", note)

    def test_an_empty_log_is_not_an_error(self):
        self.write()
        entries, _ = server.log_tail(limit=10, path=self.path)
        self.assertEqual(entries, [])


class HandlerStub(server.Handler):
    def __init__(self, path="/log", authorization=None):
        self.command = "GET"
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"GET {path} HTTP/1.0"
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        lines = ["Content-Length: 0"]
        if authorization is not None:
            lines.append(f"Authorization: {authorization}")
        self.headers = email.message_from_string("\r\n".join(lines) + "\r\n\r\n")
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        self.do_GET()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, body = text.partition("\r\n\r\n")
        return int(head.split(" ")[1]), (json.loads(body) if body.strip() else None), text


class TestTheLogRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._log_path = server.LOG_PATH
        self._token = server.AUTH_TOKEN
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.AUTH_TOKEN = ""

    def tearDown(self):
        server.LOG_PATH = self._log_path
        server.AUTH_TOKEN = self._token
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def write(self, *lines):
        server.LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_the_route_reports_the_newest_first(self):
        self.write(line("brief"), line("turn", skill="general"))
        status, payload, _ = HandlerStub().send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["entries"][0]["event"], "turn")
        self.assertEqual(payload["entries"][1]["event"], "brief")

    def test_the_limit_query_is_honoured(self):
        self.write(*[line(f"e{index}") for index in range(20)])
        status, payload, _ = HandlerStub("/log?limit=4").send()
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["entries"]), 4)

    def test_an_absent_log_is_an_empty_view_not_an_error(self):
        status, payload, _ = HandlerStub().send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["entries"], [])
        self.assertIn("no action log", payload["note"])

    def test_the_view_is_gated_by_the_token(self):
        server.AUTH_TOKEN = "a" * 64
        status, payload, _ = HandlerStub().send()
        self.assertEqual(status, 401)
        self.assertIn("bearer token", payload["error"])
        status, _, _ = HandlerStub(authorization=f"Bearer {'a' * 64}").send()
        self.assertEqual(status, 200)

    def test_the_path_is_reported_so_a_student_can_open_the_file_itself(self):
        self.write(line("brief"))
        _, payload, _ = HandlerStub().send()
        self.assertEqual(payload["path"], str(server.LOG_PATH))


class TestTheLogKeepsItsPromises(unittest.TestCase):
    """The endpoint is what makes 'counts, never the words' worth testing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._spend = server.SPEND
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.SPEND = type(server.SPEND)(server.CONFIG)

        class Stub:
            def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
                return (
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "skill": "general",
                                            "args": {"request": "x"},
                                            "needs_confirmation": False,
                                        }
                                    ),
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    },
                    0.01,
                )

        server.get_client = lambda: Stub()

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.SPEND = self._spend
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def test_a_real_turn_writes_a_line_the_view_can_read_back(self):
        body = json.dumps({"transcript": "what is a ketone"}).encode("utf-8")
        stub = server.Handler.__new__(server.Handler)
        stub.command, stub.path, stub.request_version = "POST", "/turn", "HTTP/1.0"
        stub.requestline = "POST /turn HTTP/1.0"
        stub.rfile, stub.wfile = io.BytesIO(body), io.BytesIO()
        stub.headers = email.message_from_string(
            f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n"
        )
        stub.client_address, stub.server = ("127.0.0.1", 0), None
        stub.log_message = lambda *args: None  # no access-log noise in the suite
        stub.do_POST()

        status, payload, raw = HandlerStub().send()
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertEqual(payload["entries"][0]["event"], "turn")
        self.assertEqual(payload["entries"][0]["skill"], "general")
        # The rule, checked from the outside: the student's sentence is not in
        # the log, and the log's own copy is not in the response either.
        self.assertNotIn("ketone", raw)
        self.assertIn("transcript_chars", payload["entries"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
