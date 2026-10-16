#!/usr/bin/env python3
"""Tests for Step 10's public surface: the panel as a page, and the visitor cap.

    python3 backend/tests/test_demo.py

`/demo` is the first route in this backend that reads a file named by the
request, and the first that is meant to be reachable by someone who is not the
student. So the tests are about the two ways that goes wrong:

1. **Reading something we did not mean to serve.** Traversal out of the build,
   a source map, a dotfile, a shell script, an unknown content type. Each one is
   a separate test because each one has a different reason for being refused,
   and the reasons are the point - a single "bad path" test would pass even if
   the route happily served `/.env`.
2. **Serving a page that does not work.** The HTML asks for its script and
   stylesheet as root-absolute URLs, so a route that only answered under
   `/demo/` would produce a blank page with no error anywhere. The test reads the
   built HTML, extracts the URLs it actually asks for, and fetches them.

Then the sample course load, which is checked through the real `MemoryStore` and
`QuizStore` rather than by eye, and the cap, which is checked by using it up: the
21st request must be refused *and* must not have reached the model.

The safety property worth stating out loud: with a token set, `/demo` is behind
the gate like every other route, because a browser cannot send a bearer token
from an address bar. A visitor can therefore never load the panel against
someone's real memory - and in demo mode there is no token and no real memory to
reach.
"""

import email.message
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402
from spend import Spend  # noqa: E402
from webapp import StaticApp  # noqa: E402


def build_dir(root, files=None):
    """A stand-in for `extension/dist`, with the files a build actually has."""
    root.mkdir(parents=True, exist_ok=True)
    contents = {
        "sidepanel.html": (
            '<!doctype html><html><head>'
            '<script type="module" crossorigin src="/assets/sidepanel.js"></script>'
            '<link rel="stylesheet" crossorigin href="/assets/sidepanel.css">'
            "</head><body><div id=\"root\"></div></body></html>"
        ),
        "assets/sidepanel.js": "console.log('cadence')\n",
        "assets/sidepanel.css": "body{color:#111}\n",
        "assets/sidepanel.js.map": '{"version":3,"sources":["src/SidePanel.jsx"]}',
        ".env": "NEBIUS_API_KEY=should-never-be-served\n",
        "setup.sh": "#!/bin/sh\necho hi\n",
        "manifest.json": '{"manifest_version":3}',
    }
    contents.update(files or {})
    for name, text in contents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


class TestWhatThePanelRouteWillServe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_dir(Path(self.tmp.name) / "dist")
        self.app = StaticApp(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_entry_point_is_the_bare_demo_url(self):
        for path in ("/demo", "/demo/", "/", ""):
            with self.subTest(path=path):
                status, content_type, body = self.app.read(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", content_type)
                self.assertIn(b'id="root"', body)

    def test_the_assets_the_page_asks_for_are_served_with_the_right_types(self):
        status, content_type, body = self.app.read("/assets/sidepanel.js")
        self.assertEqual((status, content_type), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"cadence", body)
        status, content_type, _ = self.app.read("/assets/sidepanel.css")
        self.assertEqual((status, content_type), (200, "text/css; charset=utf-8"))

    def test_the_built_page_actually_loads(self):
        """Read the HTML, fetch what it asks for. A blank page is the failure."""
        _, _, html = self.app.read("/demo")
        urls = re.findall(r'(?:src|href)="([^"]+)"', html.decode("utf-8"))
        self.assertTrue(urls, "the built page asks for nothing, which cannot be right")
        for url in urls:
            with self.subTest(url=url):
                status, content_type, _ = self.app.read(url)
                self.assertEqual(status, 200, f"{url} would 404 in a browser")
                self.assertNotIn("json", content_type, f"{url} came back as an error")

    def test_demo_relative_asset_paths_work_too(self):
        status, content_type, _ = self.app.read("/demo/assets/sidepanel.css")
        self.assertEqual((status, content_type), (200, "text/css; charset=utf-8"))

    def test_traversal_out_of_the_build_is_refused(self):
        outside = self.root.parent / "secret.txt"
        outside.write_text("private\n", encoding="utf-8")
        for path in (
            "/demo/../secret.txt",
            "/../secret.txt",
            "/demo/assets/../../secret.txt",
            "/demo/....//secret.txt",
        ):
            with self.subTest(path=path):
                status, _, _ = self.app.read(path)
                self.assertEqual(status, 404)

    def test_a_dotfile_is_not_served(self):
        self.assertEqual(self.app.read("/demo/.env")[0], 404)
        self.assertEqual(self.app.read("/.env")[0], 404)

    def test_a_source_map_is_not_public(self):
        """A map in a public demo hands a visitor our source."""
        self.assertEqual(self.app.read("/assets/sidepanel.js.map")[0], 404)

    def test_an_unknown_type_is_a_404_rather_than_a_guess(self):
        self.assertEqual(self.app.read("/demo/setup.sh")[0], 404)

    def test_the_extensions_own_files_are_not_part_of_the_page(self):
        """The manifest and service worker sit in the same build, and are not ours to serve."""
        self.assertEqual(self.app.read("/manifest.json")[0], 404)

    def test_a_directory_is_not_listed(self):
        self.assertEqual(self.app.read("/demo/assets/")[0], 404)

    def test_a_missing_build_says_how_to_make_one(self):
        empty = StaticApp(Path(self.tmp.name) / "not-built")
        self.assertFalse(empty.available)
        status, content_type, body = empty.read("/demo")
        self.assertEqual(status, 503)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertIn(b"npm run build", body)

    def test_a_file_over_the_size_limit_is_refused(self):
        big = self.root / "assets" / "huge.js"
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.app.read("/assets/huge.js")[0], 413)


class TestTheSampleCourseLoad(unittest.TestCase):
    """The demo's data, checked through the stores that will read it."""

    def test_the_samples_parse_with_no_errors(self):
        memory = MemoryStore(ROOT / "demo" / "memory").load()
        progress = QuizStore(ROOT / "demo" / "progress").load()
        self.assertEqual(memory.errors, [])
        self.assertEqual(progress.errors, [])
        self.assertGreaterEqual(len(memory.topic_names()), 4)
        self.assertGreaterEqual(memory.count(), 8)
        self.assertGreaterEqual(len(progress.ordered()), 2)

    def test_the_samples_produce_a_brief_worth_showing(self):
        """What a visitor sees first: real dates, real nudges, and one honest blank."""
        from datetime import date

        from scheduler import compose_brief

        memory = MemoryStore(ROOT / "demo" / "memory").load()
        brief = compose_brief(
            facts=[fact.as_dict() for fact in memory.facts("deadlines")],
            today=date(2026, 10, 16),
            horizon_days=7,
            weak_topics=[fact.text for fact in memory.facts("weak_topics")],
            open_quiz=None,
            generated_at="09:00",
        )
        self.assertEqual(brief["counts"]["dated"], 3)
        self.assertEqual(brief["counts"]["overdue"], 0)
        self.assertGreaterEqual(brief["counts"]["due_soon"], 1)
        # The sample deliberately includes one deadline with no date, so the demo
        # shows the rule that matters: undated is reported as undated.
        self.assertEqual(brief["counts"]["undated"], 1)
        self.assertTrue(brief["nudges"])
        self.assertEqual(brief["cost_usd"], 0.0)
        self.assertTrue(brief["weak_topics"])

    def test_the_samples_have_a_real_quiz_history(self):
        progress = QuizStore(ROOT / "demo" / "progress").load()
        totals = progress.describe(include_questions=True)["totals"]
        self.assertEqual(totals["quizzes"], 2)
        self.assertGreaterEqual(totals["correct"], 3)
        weakness = progress.weakness()
        self.assertTrue(weakness)
        self.assertTrue(all(row["asked"] > 0 for row in weakness))

    def test_a_demo_workspace_is_a_copy_and_not_the_originals(self):
        """Writes have to land somewhere - in a throwaway copy, not in demo/."""
        original = ROOT / "demo" / "memory" / "deadlines.md"
        before = original.read_text(encoding="utf-8")
        workspace = server.demo_workspace(ROOT / "demo")
        copy = MemoryStore(workspace / "memory").load()
        copy.add("deadlines", "the visitor typed this", added="2026-10-16")
        self.assertEqual(
            original.read_text(encoding="utf-8"), before, "the demo wrote into the repo"
        )
        self.assertIn("the visitor typed this", (workspace / "memory" / "deadlines.md").read_text())
        self.assertEqual(copy.errors, [])

    def test_a_workspace_survives_a_missing_sample_folder(self):
        with tempfile.TemporaryDirectory() as empty:
            workspace = server.demo_workspace(Path(empty) / "nothing-here")
            self.assertTrue((workspace / "memory").is_dir())
            self.assertTrue((workspace / "progress").is_dir())


class HandlerStub(server.Handler):
    """The real handler, without a socket (see test_deploy.py for the reasoning)."""

    def __init__(self, path="/turn", body=None, command="GET", address="127.0.0.1",
                 authorization=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = f"{command} {path} HTTP/1.0"
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        lines = [f"Content-Length: {len(raw)}", "Content-Type: application/json"]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        if authorization is not None:
            lines.append(f"Authorization: {authorization}")
        self.headers = email.message_from_string("\r\n".join(lines) + "\r\n\r\n")
        self.client_address = (address, 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        getattr(self, f"do_{self.command}")()
        text = self.wfile.getvalue().decode("utf-8", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        parsed = None
        if body.strip() and "application/json" in head:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
        return int(head.split(" ")[1]), parsed, head


class ScriptedClient:
    """Routes everything to `general`, then answers in prose. The cheap path."""

    def __init__(self):
        self.calls = []

    def chat(self, tier, messages, temperature=0.3, max_tokens=None, extra=None):
        self.calls.append(tier)
        if messages and "You are a router" in messages[0]["content"]:
            content = json.dumps(
                {
                    "skill": "general",
                    "args": {"request": "hello"},
                    "needs_confirmation": False,
                }
            )
        else:
            content = "Sure - here is the short answer."
        return (
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            0.01,
        )


class DemoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._client = server.get_client
        self._log_path = server.LOG_PATH
        self._panel = server.PANEL
        self._spend = server.SPEND
        self._demo = server.DEMO_MODE
        self._demo_root = server.DEMO_ROOT
        self._token = server.AUTH_TOKEN
        self._conflict = server.DEMO_CONFLICT
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        # A demo workspace, exactly as demo mode builds one at startup.
        workspace = server.demo_workspace(ROOT / "demo")
        server.DEMO_ROOT = workspace
        server.MEMORY = MemoryStore(workspace / "memory").load()
        server.PROGRESS = QuizStore(workspace / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        server.PANEL = StaticApp(build_dir(self.root / "dist"))
        server.AUTH_TOKEN = ""
        server.DEMO_MODE = True
        server.DEMO_CONFLICT = ""
        server.SPEND = Spend(server.CONFIG, cap=3)  # small, so the cap is reachable
        self.client = ScriptedClient()
        server.get_client = lambda: self.client

    def tearDown(self):
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.get_client = self._client
        server.LOG_PATH = self._log_path
        server.PANEL = self._panel
        server.SPEND = self._spend
        server.DEMO_MODE = self._demo
        server.DEMO_ROOT = self._demo_root
        server.AUTH_TOKEN = self._token
        server.DEMO_CONFLICT = self._conflict
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def turn(self, transcript="what is a ketone", address="127.0.0.1", **fields):
        return HandlerStub(
            path="/turn",
            body={"transcript": transcript, **fields},
            command="POST",
            address=address,
        ).send()


class TestTheDemoPage(DemoTestCase):
    def test_the_page_is_served_with_no_token_in_demo_mode(self):
        status, _, head = HandlerStub("/demo").send()
        self.assertEqual(status, 200)
        self.assertIn("text/html", head)

    def test_the_assets_it_asks_for_come_back_from_the_same_server(self):
        status, _, head = HandlerStub("/assets/sidepanel.js").send()
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", head)

    def test_a_token_gate_still_covers_the_page(self):
        """A browser cannot send a bearer token from an address bar - by design."""
        server.DEMO_MODE = False
        server.AUTH_TOKEN = "a" * 64
        status, payload, _ = HandlerStub("/demo").send()
        self.assertEqual(status, 401)
        self.assertIn("bearer token", payload["error"])

    def test_an_unbuilt_panel_says_what_to_do(self):
        server.PANEL = StaticApp(self.root / "not-built")
        status, payload, _ = HandlerStub("/demo").send()
        self.assertEqual(status, 503)
        self.assertIn("npm run build", payload["error"])

    def test_health_reports_the_demo_and_the_spend(self):
        status, payload, _ = HandlerStub("/health").send()
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo"]["on"])
        self.assertEqual(payload["demo"]["session_cap"], 3)
        self.assertTrue(payload["demo"]["sample_data"])
        self.assertEqual(payload["spend"]["turns"], 0)
        self.assertTrue(payload["panel"]["built"])


class TestTheVisitorBudget(DemoTestCase):
    def test_questions_are_counted_and_the_cap_refuses_the_next_one(self):
        for index in range(1, 4):
            status, payload, _ = self.turn(f"question {index}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["session"]["used"], index)
            self.assertEqual(payload["session"]["remaining"], 3 - index)
            self.assertTrue(payload["session"]["demo"])
        status, payload, _ = self.turn("one too many")
        self.assertEqual(status, 429)
        self.assertIn("3 questions", payload["error"])
        self.assertEqual(payload["session"]["used"], 3)

    def test_a_refused_question_never_reaches_the_model(self):
        for index in range(3):
            self.turn(f"question {index}")
        calls = len(self.client.calls)
        self.turn("one too many")
        self.assertEqual(len(self.client.calls), calls, "a 429 still spent money")

    def test_the_refusal_is_logged(self):
        for index in range(4):
            self.turn(f"question {index}")
        self.assertIn("session_capped", self.log_text())

    def test_the_budget_is_counted_per_visitor(self):
        for index in range(3):
            self.turn(f"question {index}", address="203.0.113.7")
        self.assertEqual(self.turn("too many", address="203.0.113.7")[0], 429)
        self.assertEqual(self.turn("hello", address="198.51.100.4")[0], 200)

    def test_a_header_cannot_reset_a_budget(self):
        """The identifier a client supplies is one it can reset, so it is ignored."""
        for index in range(3):
            HandlerStub(
                path="/turn",
                body={"transcript": f"question {index}"},
                command="POST",
                headers={"X-Cadence-Session": f"fresh-{index}"},
            ).send()
        status, _, _ = HandlerStub(
            path="/turn",
            body={"transcript": "one too many"},
            command="POST",
            headers={"X-Cadence-Session": "brand-new-session-id"},
        ).send()
        self.assertEqual(status, 429)

    def test_a_turn_in_local_mode_is_not_capped(self):
        """The cap belongs to the public demo, not to the student's own panel."""
        server.DEMO_MODE = False
        server.SPEND = Spend(server.CONFIG, cap=0)
        for index in range(3):
            status, payload, _ = self.turn(f"question {index}")
            self.assertEqual(status, 200)
            self.assertFalse(payload["session"]["demo"])
            self.assertIsNone(payload["session"]["cap"])

    def test_the_cost_display_adds_up_over_a_session(self):
        for index in range(2):
            status, payload, _ = self.turn(f"question {index}")
            self.assertEqual(status, 200)
        self.assertEqual(payload["spend"]["turns"], 2)
        self.assertGreater(payload["spend"]["cost_usd"], 0)
        self.assertLess(payload["spend"]["remaining_credit_usd"], 25.0)
        self.assertIn("super", payload["spend"]["by_tier"])

    def test_health_does_not_charge_a_question(self):
        for _ in range(3):
            HandlerStub("/health").send()
        status, payload, _ = self.turn("the first real question")
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"]["used"], 1)


class TestTheDemoRunsOnTheSampleData(DemoTestCase):
    def test_the_brief_is_the_invented_student_and_not_the_real_one(self):
        status, payload, _ = HandlerStub("/brief").send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["dated"], 3)
        self.assertEqual(payload["counts"]["undated"], 1)
        self.assertTrue(any("midterm" in item["text"] for item in payload["deadlines"]))

    def test_the_study_record_comes_from_the_samples(self):
        status, payload, _ = HandlerStub("/progress").send()
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_sessions"], 2)
        self.assertTrue(payload["declared_weak"])

    def test_a_visitor_can_write_and_it_lands_in_the_copy(self):
        """The demo is not read-only - it is a copy, which is the better promise."""
        before = (ROOT / "demo" / "memory" / "deadlines.md").read_text(encoding="utf-8")
        server.SPEND = Spend(server.CONFIG, cap=5)
        status, payload, _ = self.turn("remember that my stats exam moved to December 3")
        self.assertEqual(status, 200)
        self.assertEqual(
            (ROOT / "demo" / "memory" / "deadlines.md").read_text(encoding="utf-8"),
            before,
            "a visitor wrote into the repository",
        )
        brief = HandlerStub("/brief").send()[1]
        self.assertIn(brief["counts"]["dated"], (3, 4))


class TestTheStartupConflict(unittest.TestCase):
    def test_a_public_demo_and_a_token_cannot_both_be_configured(self):
        message = server.demo_conflict(True, "a" * 64)
        self.assertIn("mutually exclusive", message)
        self.assertEqual(server.demo_conflict(True, ""), "")
        self.assertEqual(server.demo_conflict(False, "a" * 64), "")
        self.assertEqual(server.demo_conflict(False, ""), "")

    def test_the_server_exits_rather_than_starting_with_both_set(self):
        original = server.DEMO_CONFLICT
        server.DEMO_CONFLICT = "nope"
        try:
            with self.assertRaises(SystemExit) as caught:
                server.main()
        finally:
            server.DEMO_CONFLICT = original
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
