#!/usr/bin/env python3
"""End-to-end smoke test: the server a student actually runs, over a socket.

    python3 backend/tests/test_smoke.py

Every other suite imports `server` and drives the handler directly. That is
faster and can reach into internals, but it cannot prove the thing the README
promises - that `python3 backend/server.py` starts, binds a port, loads the
skills directory, and answers. All of that lives outside any imported module: the
banner, the registry resolving `skills/` relative to its own file, the socket,
and the shutdown. A broken import or a bad path in a static initialiser would
leave every other test green and the app dead on arrival.

So this suite copies the app into a temp tree, points the model at a local stub,
runs the real process, and talks to it with real HTTP requests. Three properties
it is designed around:

1. **Nothing real is touched.** The copy is the root, so `memory/` and
   `progress/` are empty directories inside a temp dir and the student's own
   files are never read or written. There is no `.env` in the copy either, so
   the credentials in the environment are exactly the ones set below.
2. **Nothing is spent.** Every model call lands on the stub, which records what
   it was asked for. That is what makes "a 401 must not cost a model call" a
   testable claim rather than a comment.
3. **It fails loudly.** If the process does not come up, the assertion carries
   its actual output and exit status, not "connection refused".
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent

TOKEN = "smoke-test-token"
# Matches ORIGIN_RE in server.py: the extension id is 32 chars from a-p.
EXTENSION_ORIGIN = "chrome-extension://" + ("a" * 32)
DUMMY_API_KEY = "smoke-test-key"

ROUTER_DECISION = {"skill": "general", "args": {}, "needs_confirmation": False}
ANSWER_TEXT = "A ketone is a carbonyl group bonded to two carbon atoms."

# The stub picks its script from the transcript, which is how the two Step 9 paths
# get driven end to end without a second stub. `TOOL_RESULT_MARKER` has to be
# checked first: it is what the *second* model call contains, and it is the only
# signal that a tool has already run.
TOOL_TRANSCRIPT = "add two and two for me"
SEARCH_TRANSCRIPT = "search the web for my syllabus"
TOOL_RESULT_MARKER = "TOOL RESULT - "
TOOL_ANSWER = "That comes to four."
TOOL_REQUEST = {"tool": "calculate", "args": {"expression": "2 + 2"}}
SEARCH_REQUEST = {"tool": "web_search", "args": {"query": "syllabus"}}

START_TIMEOUT = 20.0
READY_TIMEOUT = 10.0
HTTP_TIMEOUT = 10.0


def free_port():
    """An unused loopback port.

    Picked here rather than by asking the server: `_resolve_port()` deliberately
    ignores port 0, because a server that silently relocates looks exactly like
    a server that is down.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def completion(content, prompt_tokens=100, completion_tokens=20):
    return {
        "id": "stub-1",
        "object": "chat.completion",
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.lock = threading.Lock()


class StubTokenFactory(BaseHTTPRequestHandler):
    """Stands in for Token Factory: records the call, answers like the real API.

    The two model calls in a turn are told apart exactly the way a reader would
    - by the system prompt. The router announces itself ("You are a router"), so
    it gets a routing decision; everything else is a skill or the general
    fallback and gets prose.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _record(self, payload):
        with self.server.lock:
            self.server.calls.append(
                {
                    "path": self.path,
                    "model": payload.get("model"),
                    "authorization": self.headers.get("Authorization"),
                    "messages": payload.get("messages") or [],
                }
            )

    def _respond(self, body, status=200):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Not needed by the server, which only lists models from the spike, but
        # answering keeps the stub a plausible stand-in.
        self._respond({"object": "list", "data": []})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        self._record(payload)

        text = " ".join(
            str(message.get("content") or "")
            for message in payload.get("messages") or []
            if isinstance(message, dict)
        )
        if "You are a router" in text:
            self._respond(completion(json.dumps(ROUTER_DECISION)))
        elif TOOL_RESULT_MARKER in text:
            # The tool has run: answer, the way the loop's wrap-up step expects.
            self._respond(completion(json.dumps({"final": TOOL_ANSWER})))
        elif TOOL_TRANSCRIPT in text:
            self._respond(completion(json.dumps(TOOL_REQUEST)))
        elif SEARCH_TRANSCRIPT in text:
            self._respond(completion(json.dumps(SEARCH_REQUEST)))
        else:
            self._respond(completion(ANSWER_TEXT))


def build_app_tree(root, base_url, memory=None):
    """A throwaway copy of the app, wired to the stub instead of Nebius.

    `memory` seeds `memory/<name>` files before the server starts. They have to be
    written *before* it starts: MemoryStore re-reads a topic whose file changed on
    disk, but a file it has never seen is only picked up by a reload, which is the
    documented behaviour and not something the test should paper over.
    """
    shutil.copytree(
        BACKEND,
        root / "backend",
        ignore=shutil.ignore_patterns("tests", "logs", "__pycache__"),
    )
    shutil.copytree(ROOT / "skills", root / "skills")
    # No memory/ and no progress/ on purpose. They are the student's own data and
    # are not in version control, so a fresh clone does not have them either; the
    # server has to come up without them (and create them on first write). This is
    # both the isolation that keeps the real files safe and the shape of the
    # checkout CI gets.
    (root / "config").mkdir()

    for name, body in (memory or {}).items():
        (root / "memory").mkdir(exist_ok=True)
        (root / "memory" / name).write_text(body, encoding="utf-8")

    config = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    config["provider"]["base_url"] = base_url
    (root / "config" / "models.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    return config


class RunningServer:
    """The real process, started the way the README starts it."""

    def __init__(self, root, config, port, token):
        self.root = Path(root)
        self.config = config
        self.port = port
        self.token = token
        self.process = None
        self.banner = []
        self._lock = threading.Lock()

    def _drain(self):
        # A thread, not a blocking readline: a server that comes up but says
        # nothing must time out rather than hang the suite.
        for line in self.process.stdout:
            with self._lock:
                self.banner.append(line.rstrip("\n"))
        self.process.stdout.close()

    def output(self):
        with self._lock:
            return "\n".join(self.banner)

    def start(self):
        env = dict(os.environ)
        env.update(
            {
                "CADENCE_HOST": "127.0.0.1",
                "CADENCE_PORT": str(self.port),
                "CADENCE_TOKEN": self.token,
                # Cleared on purpose. A developer with a real search key in their
                # shell must not turn this suite into live API calls, and "no key"
                # is itself a path worth testing end to end.
                "EXA_API_KEY": "",
                "TAVILY_API_KEY": "",
                "NEMOTRON_CONFIG_PATH": str(self.root / "config" / "models.json"),
                "PYTHONDONTWRITEBYTECODE": "1",
                # A proxy in the environment would swallow the loopback calls.
                "no_proxy": "127.0.0.1,localhost",
                "NO_PROXY": "127.0.0.1,localhost",
                # Named by the config, so this follows a rename there.
                self.config["provider"]["api_key_env"]: DUMMY_API_KEY,
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, str(self.root / "backend" / "server.py")],
            cwd=str(self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        threading.Thread(target=self._drain, daemon=True).start()

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if "listening on" in self.output():
                return
            if self.process.poll() is not None:
                raise AssertionError(
                    "the server exited before it listened (exit "
                    f"{self.process.returncode}):\n{self.output()}"
                )
            time.sleep(0.05)
        raise AssertionError(
            f"the server did not listen within {START_TIMEOUT:.0f}s:\n{self.output()}"
        )

    def wait_until_ready(self):
        """Answering HTTP, not merely bound. The banner alone is not proof."""
        deadline = time.time() + READY_TIMEOUT
        last = None
        while time.time() < deadline:
            try:
                status, _, _ = self.request("GET", "/health")
                if status == 200:
                    return
                last = f"HTTP {status}"
            except OSError as exc:  # connection refused while it warms up
                last = str(exc)
            time.sleep(0.05)
        raise AssertionError(f"/health never answered 200 ({last}):\n{self.output()}")

    def request(self, method, path, body=None, token=None, origin=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        if origin is not None:
            request.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.status, response.headers, _parse(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, _parse(exc.read())

    def stop(self):
        """Ask it to stop, then insist. Returns a complaint, or ''."""
        if self.process is None:
            return ""
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            return f"the server ignored SIGTERM and had to be killed:\n{self.output()}"
        if self.process.returncode not in (0, -15):
            return (
                f"the server exited {self.process.returncode} on shutdown:\n"
                f"{self.output()}"
            )
        return ""


def _parse(raw):
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"raw": raw.decode("utf-8", "replace")}


class SmokeTest(unittest.TestCase):
    """One stub, one server, started once for the whole file."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cadence-smoke-")
        cls.root = Path(cls.temp.name) / "app"

        cls.stub = StubServer(("127.0.0.1", free_port()), StubTokenFactory)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        stub_port = cls.stub.server_address[1]

        cls.config = build_app_tree(cls.root, f"http://127.0.0.1:{stub_port}")
        cls.server = RunningServer(cls.root, cls.config, free_port(), TOKEN)
        cls.server.start()
        cls.server.wait_until_ready()

    @classmethod
    def tearDownClass(cls):
        complaint = cls.server.stop()
        cls.stub.shutdown()
        cls.stub.server_close()
        cls.temp.cleanup()
        if complaint:
            raise AssertionError(complaint)

    def calls(self):
        with self.stub.lock:
            return list(self.stub.calls)

    # ------------------------------------------------ the promise: it starts --

    def test_health_is_open_and_reports_what_loaded(self):
        before = len(self.calls())
        status, _, body = self.server.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "cadence-backend")
        self.assertIn("explain", body["skills_loaded"])
        self.assertEqual(body["skill_errors"], [])
        self.assertTrue(body["api_key_present"])
        self.assertEqual(body["memory"]["facts"], 0)
        self.assertEqual(body["page_reading"]["mode"], "explicit")
        # Bumped to 12 with Step 12's read-only calendar (the done-ness of the code
        # is what this field reports; Step 7 is written and verified but not
        # deployed, and has its own field for that).
        self.assertEqual(body["step"], 12)
        # Step 10's three additions are part of the contract the panel reads on
        # load: what this backend has spent, whether a demo is running, and
        # whether there is a panel to serve at /demo.
        # Deltas rather than absolutes: this suite shares one server process, so
        # other tests may already have spent. The figure's *shape* is the contract.
        self.assertIsInstance(body["spend"]["turns"], int)
        self.assertEqual(body["spend"]["credit_usd"], 25.0)
        self.assertEqual(body["spend"]["session_cap"], 20)
        self.assertFalse(body["demo"]["on"])
        self.assertIsNone(body["demo"]["session_cap"])
        self.assertIn("built", body["panel"])
        # The tool set, and whether search can actually run. Reported because a tool
        # we offer the model without a key is a promise this backend cannot keep.
        self.assertEqual(
            body["tools"]["names"], ["web_search", "calculate", "propose_skill"]
        )
        self.assertEqual(body["tools"]["search"], {"configured": False, "provider": None})
        self.assertIn("not a code sandbox", body["tools"]["code_execution"])
        # The health payload is unauthenticated, so no credential may appear in it.
        self.assertNotIn(DUMMY_API_KEY, json.dumps(body))
        self.assertNoNewCalls(before, "GET /health must not spend a model call")

    def test_the_banner_describes_the_tree_it_actually_loaded(self):
        """The startup output is the only place the resolved paths are visible.

        That is worth asserting: the app reads `memory/` and `progress/` relative
        to its own file, so a copy that resolved to the real checkout would show
        the student's own facts here. Resolved on both sides, because the child
        resolves its own path and on macOS a temp dir is reached through a
        symlink (`/var` -> `/private/var`).
        """
        banner = self.server.output()
        self.assertIn(str((self.root / "memory").resolve()), banner)
        # And it says what the general path can do, in the same place.
        self.assertIn("tool: web_search", banner)
        self.assertIn("tool: calculate", banner)
        self.assertIn("web search: not configured", banner)
        self.assertIn("memory: 0 fact(s)", banner)
        self.assertIn("progress: 0 quiz(zes)", banner)
        self.assertIn("auth: bearer token required", banner)
        self.assertNotIn("WARNING: no API key", banner)
        # It served all of this without either data directory existing, which is
        # what a fresh clone looks like.
        self.assertFalse((self.root / "memory").exists())
        self.assertFalse((self.root / "progress").exists())

    # ------------------------------------------------- the gate, end to end --

    def test_every_route_but_health_requires_the_token(self):
        before = len(self.calls())
        for method, path in (
            ("GET", "/skills"),
            ("GET", "/memory"),
            ("GET", "/progress"),
            ("GET", "/brief"),
            ("GET", "/log"),
            # The panel page is behind the token too, and that is what makes
            # /demo safe to have in the same process as a student's own memory:
            # a browser cannot send a bearer token from an address bar, so
            # nobody can load the page against a gated backend.
            ("GET", "/demo"),
            ("GET", "/assets/sidepanel.js"),
            ("POST", "/turn"),
            ("DELETE", "/memory?topic=general&fact=abcd1234"),
        ):
            with self.subTest(route=f"{method} {path}"):
                body = {"transcript": "what is a ketone"} if method == "POST" else None
                status, _, payload = self.server.request(method, path, body=body)
                self.assertEqual(status, 401)
                self.assertIn("bearer token", payload.get("error", ""))
        self.assertNoNewCalls(before, "a refused request must not cost a model call")

    def assertNoNewCalls(self, before, message):
        """The stub is shared by the class, so this is about *new* calls only.

        Only the model names are reported: the router prompt is 20k characters
        and a failed assertion should not paste it into the terminal.
        """
        self.assertEqual(
            [call["model"] for call in self.calls()[before:]], [], message
        )

    def test_a_wrong_token_is_refused_while_health_stays_open(self):
        wrong = "not-the-token"
        self.assertEqual(self.server.request("GET", "/health", token=wrong)[0], 200)
        self.assertEqual(self.server.request("GET", "/memory", token=wrong)[0], 401)

    def test_preflight_needs_no_token(self):
        """If preflight were gated, the panel could never send its token at all."""
        status, headers, _ = self.server.request(
            "OPTIONS", "/turn", origin=EXTENSION_ORIGIN
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), EXTENSION_ORIGIN)
        self.assertIn(
            "Authorization", headers.get("Access-Control-Allow-Headers", "")
        )

    # ---------------------------------------------------- the turn, end to end

    def test_a_turn_reaches_the_model_and_answers(self):
        before = len(self.calls())
        status, headers, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "what is a ketone", "page_title": None},
            token=TOKEN,
            origin=EXTENSION_ORIGIN,
        )
        self.assertEqual(status, 200, body)

        self.assertEqual(body["reply"], ANSWER_TEXT)
        self.assertEqual(body["skill"], "general")
        self.assertEqual(body["tier"], "super")
        self.assertGreater(body["cost_usd"], 0, "cost must come from real usage")
        self.assertGreaterEqual(body["steps"], 2)
        # Step 9's first promise, proven over HTTP: the loop is opt-in, so a model
        # that just answers costs one call and runs no tools at all.
        self.assertEqual(body["tools"], [])
        self.assertFalse(body["steps_truncated"])
        self.assertLessEqual(body["steps"], body["max_steps"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), EXTENSION_ORIGIN)

        calls = self.calls()[before:]
        self.assertEqual(len(calls), 2, "one router call and one answer call")
        for call in calls:
            self.assertEqual(call["authorization"], "Bearer " + DUMMY_API_KEY)
            self.assertTrue(
                call["path"].endswith(self.config["provider"]["chat_completions_path"]),
                call["path"],
            )
        # The router runs on its declared tier and the answer on the skill's, so
        # the tier mapping in config/models.json is exercised for real.
        self.assertEqual(calls[0]["model"], self.config["tiers"]["nano"]["model"])
        self.assertEqual(calls[1]["model"], self.config["tiers"]["super"]["model"])

    def test_the_general_path_runs_a_tool_and_reports_what_it_used(self):
        before = len(self.calls())
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": TOOL_TRANSCRIPT, "page_title": None},
            token=TOKEN,
            origin=EXTENSION_ORIGIN,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["reply"], TOOL_ANSWER)
        self.assertEqual([item["tool"] for item in body["tools"]], ["calculate"])
        self.assertTrue(body["tools"][0]["ok"])
        self.assertFalse(body["steps_truncated"])
        self.assertLessEqual(body["steps"], body["max_steps"])

        calls = self.calls()[before:]
        self.assertEqual(len(calls), 3, "router, the tool step, then the answer")
        answer_call = " ".join(
            str(message.get("content") or "") for message in calls[2]["messages"]
        )
        # The tool really ran here - 2 + 2 was evaluated by our code, not imagined
        # by the stub - and what went back is labelled as tool data.
        self.assertIn("TOOL RESULT - calculate: ok", answer_call)
        self.assertIn("data returned by a tool", answer_call)
        self.assertIn("2 + 2", answer_call)
        self.assertIn("4", answer_call)

    def test_a_search_without_a_key_fails_honestly_instead_of_pretending(self):
        before = len(self.calls())
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": SEARCH_TRANSCRIPT, "page_title": None},
            token=TOKEN,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["search_configured"], False)
        self.assertEqual(len(body["tools"]), 1)
        self.assertEqual((body["tools"][0]["tool"], body["tools"][0]["ok"]), ("web_search", False))
        self.assertEqual(body["tools"][0]["reason"], "no_key")
        # The turn still ends with a sentence: a missing key is a limitation to
        # report, not an error page.
        self.assertEqual(body["reply"], TOOL_ANSWER)
        answer_call = " ".join(
            str(message.get("content") or "")
            for message in self.calls()[before:][2]["messages"]
        )
        self.assertIn("TOOL FAILED", answer_call)
        self.assertIn("EXA_API_KEY", answer_call)

    def test_the_brief_is_a_get_route_that_is_never_cached_or_billed(self):
        before = len(self.calls())
        status, headers, body = self.server.request("GET", "/brief", token=TOKEN)
        self.assertEqual(status, 200)
        # A brief is only true for the day it was made.
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual((body["source"], body["cost_usd"]), ("code", 0.0))
        self.assertEqual(body["headline"], "Nothing due that I know about.")
        self.assertEqual(
            body["counts"], {"dated": 0, "overdue": 0, "due_soon": 0, "undated": 0}
        )
        self.assertNoNewCalls(before, "the brief must never spend a model call")
        # It is a GET route: nothing is created by asking for it.
        self.assertEqual(
            self.server.request("POST", "/brief", body={}, token=TOKEN)[0], 404
        )

    # ------------------------------------------------- Step 10, end to end --

    def test_the_action_log_is_readable_over_the_wire(self):
        """The Log tab's route, against the file this server is writing itself."""
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "what is a ketone"},
            token=TOKEN,
            origin=EXTENSION_ORIGIN,
        )
        self.assertEqual(status, 200, body)
        status, headers, log = self.server.request("GET", "/log?limit=5", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertGreaterEqual(log["count"], 1)
        self.assertEqual(log["entries"][0]["event"], "turn")
        # The rule, read back rather than trusted: the sentence the student said
        # is not in the log, and the cost of the turn is.
        self.assertNotIn("ketone", json.dumps(log))
        self.assertIn("transcript_chars", log["entries"][0])
        self.assertGreater(log["entries"][0]["cost_usd"], 0)

    def test_a_turn_reports_the_spend_and_the_budget(self):
        _, _, before = self.server.request("GET", "/health", token=TOKEN)
        body = self.a_turn_for_step_10()
        self.assertEqual(body["spend"]["turns"], before["spend"]["turns"] + 1)
        self.assertGreater(body["spend"]["cost_usd"], before["spend"]["cost_usd"])
        self.assertLess(body["spend"]["remaining_credit_usd"], 25.0)
        self.assertIn(body["tier"], body["spend"]["by_tier"])
        # Not a demo, so there is no cap to report - and the field says so
        # rather than reporting a limit that is not being enforced.
        self.assertFalse(body["session"]["demo"])
        self.assertIsNone(body["session"]["cap"])
        self.assertIsNone(body["confirmation"], "an ordinary turn asked for nothing")

    def test_a_turn_with_nothing_to_confirm_has_no_approval_to_give(self):
        status, _, payload = self.server.request(
            "POST", "/turn", body={"confirm": "ap0001"}, token=TOKEN
        )
        self.assertEqual(status, 409)
        self.assertIn("not waiting", payload["error"])

    def a_turn_for_step_10(self):
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "what is a ketone"},
            token=TOKEN,
            origin=EXTENSION_ORIGIN,
        )
        self.assertEqual(status, 200, body)
        return body

    def test_a_bad_request_is_rejected_before_any_model_call(self):
        before = len(self.calls())
        status, _, payload = self.server.request(
            "POST", "/turn", body={"transcript": "   "}, token=TOKEN
        )
        self.assertEqual(status, 400)
        self.assertIn("transcript", payload.get("error", ""))
        self.assertNoNewCalls(before, "a malformed turn must not spend")

    def test_page_text_without_an_explicit_request_is_refused(self):
        """The promise the whole page-read design rests on, over the wire."""
        before = len(self.calls())
        status, _, payload = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "what is a ketone", "page_text": "secret page text"},
            token=TOKEN,
        )
        self.assertEqual(status, 400)
        self.assertIn("explicit request", payload.get("error", ""))
        self.assertNoNewCalls(before, "a refused turn must not spend")

    def test_a_foreign_origin_is_never_granted_cors(self):
        """Origin is not a security boundary here - the token is.

        The server answers such a request (a non-browser client can send any
        Origin it likes); what it must never do is hand back a grant that lets a
        web page read the reply.
        """
        status, headers, _ = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "what is a ketone"},
            token=TOKEN,
            origin="https://not-our-origin.example",
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
        self.assertIsNone(headers.get("Vary"))


class BriefOverHTTP(unittest.TestCase):
    """Step 8 end to end: a real memory file, through the server, over HTTP.

    The other class proves the contract; this one proves the plumbing - that a
    deadline written in the student's own words in `memory/deadlines.md` comes out
    of `GET /brief` with the right date and the right number of days, without a
    model call anywhere in the path.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cadence-brief-")
        cls.root = Path(cls.temp.name) / "app"

        cls.stub = StubServer(("127.0.0.1", free_port()), StubTokenFactory)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

        # Five days out, written as an exact date so the assertion is arithmetic
        # rather than a string we hoped for.
        cls.due = (date.today() + timedelta(days=5)).isoformat()
        cls.config = build_app_tree(
            cls.root,
            f"http://127.0.0.1:{cls.stub.server_address[1]}",
            memory={
                "deadlines.md": (
                    "---\ntopic: deadlines\ntitle: Deadlines\n---\n\n# Deadlines\n\n"
                    f"- my organic chemistry midterm is on {cls.due}"
                    " <!-- id: c042a5a1 added: 2026-10-14 -->\n"
                    "- my advisor is Dr. Chen\n"
                )
            },
        )
        cls.server = RunningServer(cls.root, cls.config, free_port(), TOKEN)
        cls.server.start()
        cls.server.wait_until_ready()

    @classmethod
    def tearDownClass(cls):
        complaint = cls.server.stop()
        cls.stub.shutdown()
        cls.stub.server_close()
        cls.temp.cleanup()
        if complaint:
            raise AssertionError(complaint)

    def test_a_date_question_is_answered_from_the_brief_with_no_model_call(self):
        """The measured bug behind this block, pinned over HTTP.

        Live, asked "how many days until my midterm", the model answered **49 days**
        for a deadline the brief puts 28 days away - and later, routed to `plan` on
        Ultra, it spent 1.9 s and $0.00098 asking how many hours the student had to
        study instead of reading out a date it had been given. The date is now
        answered from the brief itself, in code, before anything is spent: what this
        asserts is that no model was called *and* that the number in the reply is
        the number `/brief` computes - agreement with the panel, not a literal.
        """
        _, _, brief = self.server.request("GET", "/brief", token=TOKEN)
        deadline = brief["deadlines"][0]
        with self.stub.lock:
            before = len(self.stub.calls)
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={"transcript": "how many days until my midterm", "page_title": None},
            token=TOKEN,
        )
        self.assertEqual(status, 200, body)
        with self.stub.lock:
            calls = list(self.stub.calls)[before:]
        self.assertEqual(calls, [], "the router and a model were called for a date lookup")
        self.assertEqual(body["skill"], "brief")
        self.assertEqual(body["cost_usd"], 0.0)
        self.assertIn(deadline["due"], body["reply"])
        self.assertIn("{0} days from now".format(deadline["days_left"]), body["reply"])
        # And the == is anchored to the fixture, so a stale date cannot pass.
        self.assertEqual(deadline["days_left"], 5)

    def test_the_model_never_gets_the_chance_to_recompute_a_date(self):
        """The half that still routes: a real request gets the computed numbers.

        The dates block exists because a model asked for a countdown invented one
        (49 days for a 28-day deadline). A date *question* is now answered in code,
        but a request for work - a plan, a schedule, an explanation - still reaches
        a model, and must still be handed the arithmetic rather than the chance to
        guess it.
        """
        _, _, brief = self.server.request("GET", "/brief", token=TOKEN)
        deadline = brief["deadlines"][0]
        with self.stub.lock:
            before = len(self.stub.calls)
        status, _, body = self.server.request(
            "POST",
            "/turn",
            body={
                "transcript": "plan the next three evenings for my organic chemistry midterm",
                "page_title": None,
            },
            token=TOKEN,
        )
        self.assertEqual(status, 200, body)
        with self.stub.lock:
            calls = list(self.stub.calls)[before:]
        self.assertEqual(len(calls), 2, "router, then one answer - no tools needed")
        prompt = " ".join(
            str(message.get("content") or "") for message in calls[1]["messages"]
        )
        self.assertIn("never recompute a date yourself", prompt)
        self.assertIn(deadline["due"], prompt)
        self.assertIn("{0} days away".format(deadline["days_left"]), prompt)

    def brief(self):
        status, headers, body = self.server.request("GET", "/brief", token=TOKEN)
        self.assertEqual(status, 200)
        return headers, body

    def days_left(self, body):
        """Relative to the server's own idea of today, so midnight cannot flake it."""
        return (date.fromisoformat(self.due) - date.fromisoformat(body["date"])).days

    def test_a_remembered_deadline_reaches_the_brief(self):
        _, body = self.brief()
        left = self.days_left(body)
        self.assertEqual(body["counts"]["dated"], 1)
        self.assertEqual(body["counts"]["due_soon"], 1)
        deadline = body["deadlines"][0]
        self.assertEqual(deadline["due"], self.due)
        self.assertEqual(deadline["days_left"], left)
        self.assertFalse(deadline["overdue"])
        self.assertEqual(
            body["headline"], f"Next deadline is in {left} days, on {self.due}."
        )
        self.assertIn(
            f"in {left} days — my organic chemistry midterm is on {self.due}",
            body["nudges"],
        )

    def test_an_undated_note_is_reported_rather_than_given_a_date(self):
        _, body = self.brief()
        self.assertEqual(
            [note["text"] for note in body["undated"]], ["my advisor is Dr. Chen"]
        )
        for note in body["undated"]:
            self.assertTrue(note["id"], "every note keeps its id, so the panel can delete it")
            self.assertNotIn("due", note, "an undated note is never given a date")

    def test_the_brief_reaches_the_model_zero_times(self):
        with self.stub.lock:
            before = len(self.stub.calls)
        _, body = self.brief()
        with self.stub.lock:
            after = len(self.stub.calls)
        self.assertEqual(after, before, "composing a brief must not call a model")
        self.assertEqual(body["cost_usd"], 0.0)

    def test_the_rest_of_the_server_still_works_with_memory_seeded(self):
        status, _, body = self.server.request("GET", "/health", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["memory"]["facts"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
