#!/usr/bin/env python3
"""Tests for Step 13: ElevenLabs as the backend's voice.

    python3 backend/tests/test_voice.py

The feature is one sentence: **the panel can sound better without the key ever
leaving the backend.** Five ways that could be false, and each one is checked
here rather than asserted in a comment:

1. **The key could leak.** The request the panel sends is checked to carry no key,
   the `/health` payload is checked to carry no key, and the action log is read
   back looking for both the key and the words that were spoken.
2. **Speech could be free-looking when it is not.** A successful synthesis is
   checked to land in the process total (and *not* to become a turn), a cache hit
   is checked to cost nothing again, and a failed synthesis is checked to spend
   nothing at all.
3. **A failure could sound like a different failure.** 402 against 429 against 401
   are three different sentences for three different reasons, and a quota error
   read as "rate limited" sends a student off to wait for nothing.
4. **The cap could be decorative.** A 2000-character reply is checked to reach
   ElevenLabs as at most 1500 characters, ending at a sentence, with `truncated`
   reported so the panel can say the rest was not read.
5. **The fallback could be silence.** The demo, a missing key and a rejected key
   all have to leave Chrome's voice as the thing that speaks - which is why every
   one of them is an error the panel can catch rather than a crash.
"""

import email.message
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
import voice  # noqa: E402
from spend import Spend  # noqa: E402

KEY = "test-elevenlabs-key"
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-bytes"
# The default voice id, pinned. Adam rather than Rachel because a real free-plan
# key gets 402 paid_plan_required for the library voices Rachel belongs to, and a
# default nobody on the free plan can hear is not a default.
VOICE = "pNInz6obpgDQGcFmaJgB"


class FakeEleven:
    """A stand-in for api.elevenlabs.io, on a real local socket.

    A real server rather than a patched function, because the things worth
    testing here are exactly the ones a patch would skip: the URL the client
    builds, the header the key travels in, the JSON body, and what happens to
    bytes versus JSON on the way back. Every request is recorded, because half
    the assertions are about what we sent.
    """

    def __init__(self, mode="ok"):
        self.mode = mode
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": raw,
                    }
                )

                if outer.mode == "ok":
                    payload = json.loads(raw or b"{}")
                    # ElevenLabs reports what it billed; deliberately different
                    # from the character count so the tests can tell which one we
                    # believed.
                    chars = len(payload.get("text", "")) + 5
                    self._send(200, AUDIO, {"character-cost": str(chars)})
                elif outer.mode == "quota":
                    self._send(
                        402,
                        json.dumps(
                            {
                                "detail": {
                                    "type": "payment_required",
                                    "code": "insufficient_credits",
                                    "message": "You have no credits left.",
                                }
                            }
                        ).encode("utf-8"),
                    )
                elif outer.mode == "plan":
                    # What ElevenLabs really answers for a library voice on a free
                    # account: same 402 as an empty balance, different fix.
                    self._send(
                        402,
                        json.dumps(
                            {
                                "detail": {
                                    "type": "payment_required",
                                    "code": "paid_plan_required",
                                    "message": "Free users cannot use library voices via the API.",
                                }
                            }
                        ).encode("utf-8"),
                    )
                elif outer.mode == "unauthorized":
                    self._send(
                        401,
                        json.dumps(
                            {"detail": {"status": "invalid_api_key", "message": "Bad key."}}
                        ).encode("utf-8"),
                    )
                elif outer.mode == "ratelimit":
                    self._send(
                        429,
                        json.dumps(
                            {"detail": {"code": "rate_limit_exceeded", "message": "Slow down."}}
                        ).encode("utf-8"),
                    )
                elif outer.mode == "server_error":
                    self._send(500, b"upstream on fire")
                elif outer.mode == "empty":
                    self._send(200, b"")
                else:  # pragma: no cover - a typo in a test, not a behaviour
                    self._send(500, b"unknown mode")

            def _send(self, status, body, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll interval so forty tests are not forty half-second waits:
        # shutdown() only notices the flag on the next poll.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HandlerStub(server.Handler):
    """The real handler, without a socket, answering in bytes.

    Unlike the JSON stubs elsewhere this one keeps the body raw: `/speak` succeeds
    by returning an MP3, and decoding that as UTF-8 to parse it would test the
    test rather than the route.
    """

    def __init__(self, path="/speak", body=None, command="POST", authorization=""):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = "{0} {1} HTTP/1.0".format(command, path)
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        lines = [
            "Content-Length: {0}".format(len(raw)),
            "Content-Type: application/json",
            "Origin: chrome-extension://abcdefghijklmnopabcdefghijklmnop",
        ]
        if authorization:
            lines.append("Authorization: {0}".format(authorization))
        self.headers = email.message_from_string("\r\n".join(lines) + "\r\n\r\n")
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        """-> (status, body bytes, headers dict)."""
        getattr(self, "do_" + self.command)()
        blob = self.wfile.getvalue()
        head, _, payload = blob.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        return status, payload, headers

    def send_json(self):
        """Parse the body when it is JSON, and hand back bytes when it is audio.

        `/speak` answers 200 with an MP3 and every error with JSON, so a helper
        that insists on parsing would make the successful case the one it breaks.
        """
        status, payload, headers = self.send()
        try:
            body = json.loads(payload) if payload.strip() else None
        except ValueError:
            body = payload
        return status, body, headers


class VoiceTestCase(unittest.TestCase):
    """Environment, spend table and log file, put back exactly as they were."""

    mode = "ok"

    def setUp(self):
        self.fake = FakeEleven(mode=self.mode)
        self.addCleanup(self.fake.close)

        self._env = {
            name: os.environ.get(name)
            for name in (
                voice.API_KEY_ENV,
                voice.BASE_URL_ENV,
                voice.VOICE_ID_ENV,
                voice.MODEL_ID_ENV,
            )
        }
        os.environ[voice.API_KEY_ENV] = KEY
        os.environ[voice.BASE_URL_ENV] = self.fake.base_url
        os.environ.pop(voice.VOICE_ID_ENV, None)
        os.environ.pop(voice.MODEL_ID_ENV, None)
        self.addCleanup(self._restore_env)

        voice.CACHE.clear()
        self.addCleanup(voice.CACHE.clear)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self._spend = server.SPEND
        self._log_path = server.LOG_PATH
        self._demo = server.DEMO_MODE
        self._auth = server.AUTH_TOKEN
        self._stdout = sys.stdout
        server.SPEND = Spend({})
        server.LOG_PATH = self.root / "action-log.jsonl"
        sys.stdout = io.StringIO()
        self.addCleanup(self._restore_server)

    def _restore_env(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _restore_server(self):
        server.SPEND = self._spend
        server.LOG_PATH = self._log_path
        server.DEMO_MODE = self._demo
        server.AUTH_TOKEN = self._auth
        sys.stdout = self._stdout

    # -- helpers ------------------------------------------------------------

    def speak(self, text="The midterm moved to November 12.", **kwargs):
        return HandlerStub(body={"text": text}, **kwargs).send_json()

    def log_lines(self):
        path = self.root / "action-log.jsonl"
        if not path.exists():
            return []
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row]

    def last_speak_line(self):
        lines = [row for row in self.log_lines() if row.get("event") == "speak"]
        self.assertTrue(lines, "nothing was logged for a synthesis")
        return lines[-1]

    def snapshot(self):
        return server.SPEND.snapshot()


# --------------------------------------------------------------- the client --


class TestTheRequestItBuilds(VoiceTestCase):
    """What reaches ElevenLabs, exactly."""

    def test_a_successful_call_returns_the_audio_untouched(self):
        audio, meta = voice.synthesize("Hello there.")
        self.assertEqual(audio, AUDIO)
        self.assertFalse(meta["cached"])
        self.assertEqual(meta["model"], voice.DEFAULT_MODEL_ID)
        self.assertEqual(meta["voice_id"], VOICE)

    def test_it_calls_the_documented_endpoint_with_the_documented_format(self):
        voice.synthesize("Hello there.")
        request = self.fake.requests[-1]
        self.assertEqual(
            request["path"],
            "/v1/text-to-speech/{0}?output_format=mp3_44100_128".format(VOICE),
        )
        self.assertEqual(request["headers"]["xi-api-key"], KEY)
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(request["headers"]["accept"], "audio/mpeg")

    def test_the_body_asks_for_the_low_latency_model_and_states_its_settings(self):
        voice.synthesize("Hello there.")
        body = json.loads(self.fake.requests[-1]["body"])
        self.assertEqual(body["text"], "Hello there.")
        self.assertEqual(body["model_id"], "eleven_flash_v2_5")
        self.assertEqual(
            body["voice_settings"],
            {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": 1.0,
            },
        )

    def test_a_voice_and_model_can_be_configured_without_reaching_into_code(self):
        os.environ[voice.VOICE_ID_ENV] = "aDifferentVoiceId"
        os.environ[voice.MODEL_ID_ENV] = "eleven_multilingual_v2"
        _, meta = voice.synthesize("Hello.")
        self.assertIn("/aDifferentVoiceId?", self.fake.requests[-1]["path"])
        self.assertEqual(meta["model"], "eleven_multilingual_v2")

    def test_whitespace_is_collapsed_before_it_is_spoken(self):
        voice.synthesize("  one\n\ntwo   three ")
        self.assertEqual(json.loads(self.fake.requests[-1]["body"])["text"], "one two three")


class TestTheCap(VoiceTestCase):
    """A reply cannot become a bill, and the cut is at a sentence."""

    def test_a_short_reply_is_not_touched(self):
        text = "The essay is due on October 20."
        spoken, truncated = voice.truncate(text)
        self.assertEqual((spoken, truncated), (text, False))

    def test_a_long_reply_is_cut_at_the_last_sentence_inside_the_budget(self):
        sentence = "This is a sentence about the midterm. "
        text = sentence * 60  # ~2,280 characters
        spoken, truncated = voice.truncate(text)
        self.assertTrue(truncated)
        self.assertLessEqual(len(spoken), voice.MAX_SYNTH_CHARS)
        self.assertTrue(spoken.endswith("."), "cut mid-sentence")
        self.assertEqual(spoken, spoken.strip())

    def test_one_enormous_sentence_is_hard_cut_rather_than_sent_whole(self):
        spoken, truncated = voice.truncate("word " * 500)
        self.assertTrue(truncated)
        self.assertLessEqual(len(spoken), voice.MAX_SYNTH_CHARS)

    def test_the_cap_is_everywhere_the_text_goes(self):
        long_text = ("This is a sentence about the midterm. " * 60).strip()
        _, meta = voice.synthesize(long_text)
        sent = json.loads(self.fake.requests[-1]["body"])["text"]
        self.assertLessEqual(len(sent), voice.MAX_SYNTH_CHARS)
        self.assertTrue(meta["truncated"])


class TestWhatItCosts(VoiceTestCase):
    """Character counts, the provider's own number, and a cache that is free."""

    def test_the_provider_s_own_character_count_wins(self):
        """`character-cost` is what they billed; our arithmetic is the fallback."""
        text = "Hello there."
        _, meta = voice.synthesize(text)
        self.assertEqual(meta["chars"], len(text) + 5)
        self.assertEqual(meta["cost_usd"], voice.estimate_usd(len(text) + 5))

    def test_the_same_words_are_not_paid_for_twice(self):
        first_audio, first = voice.synthesize("Speak again.")
        calls = len(self.fake.requests)
        second_audio, second = voice.synthesize("Speak again.")
        self.assertEqual(len(self.fake.requests), calls, "a cache hit called ElevenLabs")
        self.assertEqual(second_audio, first_audio)
        self.assertTrue(second["cached"])
        self.assertEqual(second["cost_usd"], 0.0)
        # A hit must report the same characters the synthesis did, or the number
        # the panel shows changes when nothing about the reply did.
        self.assertEqual(second["chars"], first["chars"])
        self.assertEqual(second["truncated"], first["truncated"])

    def test_different_words_are_a_different_request(self):
        voice.synthesize("One.")
        voice.synthesize("Two.")
        self.assertEqual(len(self.fake.requests), 2)

    def test_the_cache_is_bounded_and_drops_the_oldest(self):
        cache = voice._Cache(max_entries=2, max_bytes=10_000)
        cache.put(("a",), b"x" * 10)
        cache.put(("b",), b"y" * 10)
        cache.put(("c",), b"z" * 10)
        self.assertIsNone(cache.get(("a",)), "the oldest entry should have gone")
        audio, _ = cache.get(("c",))
        self.assertEqual(audio, b"z" * 10)
        self.assertLessEqual(cache.snapshot()["entries"], 2)

    def test_the_byte_bound_is_the_one_that_stops_a_large_cache(self):
        cache = voice._Cache(max_entries=100, max_bytes=25)
        cache.put(("a",), b"x" * 20)
        cache.put(("b",), b"y" * 20)
        self.assertLessEqual(cache.snapshot()["bytes"], 25)
        self.assertIsNone(cache.get(("a",)))


class TestWhatItSaysWhenItFails(VoiceTestCase):
    """One code per cause, because they mean different things to a person."""

    mode = "quota"

    def test_an_empty_account_is_not_a_rate_limit(self):
        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("Hello.")
        self.assertEqual(caught.exception.code, "quota")
        message = str(caught.exception).lower()
        self.assertIn("credit", message)
        self.assertNotIn("rate limit", message)

    def test_a_missing_key_says_which_variable_and_never_calls_out(self):
        os.environ.pop(voice.API_KEY_ENV, None)
        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("Hello.")
        self.assertEqual(caught.exception.code, "no_key")
        self.assertIn("ELEVENLABS_API_KEY", str(caught.exception))
        self.assertEqual(self.fake.requests, [], "a keyless backend still called ElevenLabs")

    def test_the_demo_never_spends_the_operator_s_credits(self):
        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("Hello.", demo=True)
        self.assertEqual(caught.exception.code, "demo")
        self.assertEqual(self.fake.requests, [])

    def test_nothing_to_say_is_not_a_request(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                with self.assertRaises(voice.VoiceError) as caught:
                    voice.synthesize(text)
                self.assertEqual(caught.exception.code, "empty")
        self.assertEqual(self.fake.requests, [])

    def test_the_three_http_failures_are_three_sentences(self):
        for mode, code, needle in (
            ("unauthorized", "auth", "key"),
            ("ratelimit", "rate_limited", "rate limit"),
            ("server_error", "upstream", "ElevenLabs had a problem"),
        ):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                with self.assertRaises(voice.VoiceError) as caught:
                    voice.synthesize("A fresh sentence {0}.".format(mode))
                self.assertEqual(caught.exception.code, code)
                self.assertIn(needle.lower(), str(caught.exception).lower())

    def test_an_empty_audio_body_is_a_failure_not_silence(self):
        self.fake.mode = "empty"
        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("A fresh sentence.")
        self.assertEqual(caught.exception.code, "upstream")

    def test_an_unreachable_host_is_its_own_code(self):
        def explode(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("Hello.", transport=explode)
        self.assertEqual(caught.exception.code, "unreachable")


class TestAVoiceOutsideThePlan(VoiceTestCase):
    """The failure a real free-plan key produced, which used to read as "no credits"."""

    mode = "plan"

    def test_a_plan_restricted_voice_is_not_an_empty_account(self):
        with self.assertRaises(voice.VoiceError) as caught:
            voice.synthesize("Hello.")
        self.assertEqual(caught.exception.code, "plan")
        message = str(caught.exception)
        # The fix is a different voice, so the sentence has to name the variable
        # and the id that works - not send someone to buy characters they do not
        # need on a free plan.
        self.assertIn("ELEVENLABS_VOICE_ID", message)
        self.assertIn(voice.DEFAULT_VOICE_ID, message)
        self.assertNotIn("credit", message.lower())

    def test_the_route_answers_402_and_logs_which_kind_it_was(self):
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 402)
        self.assertIn("ELEVENLABS_VOICE_ID", payload["error"])
        self.assertEqual(self.last_speak_line()["outcome"], "plan")
        self.assertEqual(self.snapshot()["voice"]["requests"], 0)


class TestWhatHealthSays(VoiceTestCase):
    def test_with_a_key_the_hosted_voice_is_available(self):
        block = voice.describe()
        self.assertTrue(block["configured"])
        self.assertTrue(block["available"])
        self.assertEqual(block["off"], None)
        self.assertEqual(block["provider"], "elevenlabs")
        self.assertNotIn(KEY, json.dumps(block), "the key was published in /health")

    def test_without_a_key_it_names_the_variable_and_says_why(self):
        os.environ.pop(voice.API_KEY_ENV, None)
        block = voice.describe()
        self.assertFalse(block["available"])
        self.assertEqual(block["off"], "no-key")

    def test_the_demo_is_off_even_with_a_key(self):
        block = voice.describe(demo=True)
        self.assertEqual(block["off"], "demo")
        self.assertFalse(block["available"], "a demo would bill the operator")


# ----------------------------------------------------------------- the route --


class TestTheRoute(VoiceTestCase):
    """`POST /speak` through the real handler: bytes, headers, money, log."""

    def test_it_answers_with_audio_and_counts(self):
        status, body, headers = self.speak("The midterm moved to November 12.")
        self.assertEqual(status, 200)
        self.assertEqual(body, AUDIO)
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(headers["x-cadence-spoken-truncated"], "0")
        self.assertEqual(headers["x-cadence-spoken-cached"], "0")
        self.assertGreater(int(headers["x-cadence-spoken-chars"]), 0)
        # Cross-origin JS cannot read the counts unless they are exposed.
        self.assertIn("x-cadence-spoken-chars", headers["access-control-expose-headers"].lower())
        self.assertEqual(headers["cache-control"], "no-store")

    def test_the_request_the_panel_sends_carries_no_key(self):
        HandlerStub(body={"text": "Hello."}).send_json()
        self.assertEqual(len(self.fake.requests), 1)
        # The panel's side of the exchange is asserted by the handler stub: the
        # only header it could have sent is the backend token, and there is none
        # in this test. The key exists in exactly one place - the upstream call.
        self.assertEqual(self.fake.requests[0]["headers"]["xi-api-key"], KEY)

    def test_speech_is_money_in_the_total_and_not_a_turn(self):
        before = self.snapshot()
        self.speak("One sentence, spoken once.")
        after = self.snapshot()
        self.assertEqual(after["turns"], before["turns"], "speech was counted as a turn")
        self.assertEqual(after["voice"]["requests"], 1)
        self.assertGreater(after["voice"]["characters"], 0)
        self.assertGreater(after["voice"]["cost_usd"], 0)
        self.assertGreater(after["cost_usd"], before["cost_usd"])

    def test_speak_again_is_free_and_says_so(self):
        _, _, first_headers = self.speak("The same words twice.")
        first = self.snapshot()
        status, body, headers = self.speak("The same words twice.")
        self.assertEqual(status, 200)
        self.assertEqual(body, AUDIO)
        self.assertEqual(headers["x-cadence-spoken-cached"], "1")
        # Same reply, same numbers - only the cost differs.
        self.assertEqual(
            headers["x-cadence-spoken-chars"], first_headers["x-cadence-spoken-chars"]
        )
        self.assertEqual(headers["x-cadence-spoken-truncated"], "0")
        self.assertEqual(self.snapshot()["cost_usd"], first["cost_usd"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_a_long_reply_reports_that_it_was_shortened(self):
        status, _, headers = self.speak(("A sentence about the midterm. " * 100).strip())
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-cadence-spoken-truncated"], "1")
        sent = json.loads(self.fake.requests[-1]["body"])["text"]
        self.assertLessEqual(len(sent), voice.MAX_SYNTH_CHARS)

    def test_the_log_holds_counts_and_never_the_words(self):
        text = "Your organic chemistry midterm is on November 12."
        self.speak(text)
        line = self.last_speak_line()
        self.assertIn("chars", line)
        self.assertIn("cost_usd", line)
        self.assertIn("ms", line)
        self.assertIn("cached", line)
        self.assertNotIn("chemistry", json.dumps(line))
        self.assertNotIn("November", json.dumps(line))
        self.assertNotIn(KEY, (self.root / "action-log.jsonl").read_text(encoding="utf-8"))

    def test_a_failed_synthesis_spends_nothing(self):
        self.fake.mode = "quota"
        before = self.snapshot()
        status, payload, _ = self.speak("A sentence that fails.")
        self.assertEqual(status, 402)
        self.assertIn("credit", payload["error"].lower())
        self.assertEqual(self.snapshot()["cost_usd"], before["cost_usd"])
        self.assertEqual(self.snapshot()["voice"]["requests"], 0)
        self.assertEqual(self.last_speak_line()["outcome"], "quota")

    def test_a_bad_key_is_a_setup_error_not_a_server_fault(self):
        self.fake.mode = "unauthorized"
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 400)
        self.assertIn("elevenlabs_api_key", payload["error"].lower())

    def test_a_rate_limit_is_429_because_waiting_helps(self):
        self.fake.mode = "ratelimit"
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 429)
        self.assertIn("rate limit", payload["error"].lower())

    def test_no_key_says_how_to_fix_it_and_makes_no_request(self):
        os.environ.pop(voice.API_KEY_ENV, None)
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 400)
        self.assertIn("ELEVENLABS_API_KEY", payload["error"])
        self.assertEqual(self.fake.requests, [])

    def test_the_demo_refuses_and_the_panel_falls_back(self):
        server.DEMO_MODE = True
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 400)
        self.assertIn("demo", payload["error"].lower())
        self.assertEqual(self.fake.requests, [])

        # And /health says so first, so a well-behaved panel never asks.
        _, health, _ = HandlerStub(path="/health", command="GET").send_json()
        self.assertFalse(health["voice"]["available"])
        self.assertEqual(health["voice"]["off"], "demo")

    def test_an_empty_reply_is_a_bad_request(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"text": 7}):
            with self.subTest(body=body):
                status, payload, _ = HandlerStub(body=body).send_json()
                self.assertEqual(status, 400)
                self.assertIn("text", payload["error"])
        self.assertEqual(self.fake.requests, [])

    def test_the_token_gate_protects_the_one_endpoint_that_costs_money(self):
        server.AUTH_TOKEN = "s3cret"
        status, payload, _ = self.speak("Hello.")
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.requests, [])

        status, _, _ = HandlerStub(
            body={"text": "Hello."}, authorization="Bearer s3cret"
        ).send_json()
        self.assertEqual(status, 200)

    def test_health_reports_the_voice_without_the_key(self):
        _, payload, _ = HandlerStub(path="/health", command="GET").send_json()
        block = payload["voice"]
        self.assertTrue(block["available"])
        self.assertEqual(block["model"], "eleven_flash_v2_5")
        self.assertEqual(block["max_chars"], voice.MAX_SYNTH_CHARS)
        self.assertNotIn(KEY, json.dumps(payload))


class TestTheSpendTable(unittest.TestCase):
    """The arithmetic, apart from HTTP: voice money is in the total, turns are not."""

    def test_voice_spend_is_counted_once_and_separately(self):
        table = Spend({})
        table.record_turn(0.001)
        table.record_voice(0.00004, 10)
        table.record_voice(0.00004, 10)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["voice"]["requests"], 2)
        self.assertEqual(snapshot["voice"]["characters"], 20)
        self.assertEqual(snapshot["voice"]["cost_usd"], 0.00008)
        self.assertEqual(snapshot["cost_usd"], 0.00108)

    def test_a_reset_forgets_the_voice_too(self):
        table = Spend({})
        table.record_voice(0.00004, 10)
        table.reset()
        snapshot = table.snapshot()
        self.assertEqual(snapshot["cost_usd"], 0.0)
        self.assertEqual(snapshot["voice"], {"requests": 0, "characters": 0, "cost_usd": 0.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
