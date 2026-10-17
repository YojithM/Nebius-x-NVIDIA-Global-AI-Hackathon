#!/usr/bin/env python3
"""Tests for Step 12: a read-only Google Calendar, and the promise that makes safe.

    python3 backend/tests/test_calendar.py

The feature has one sentence in it that everything else has to serve: **Cadence
may look at your calendar and may never change it.** Four ways that could be
false, and each one is tested here rather than asserted in a comment:

1. **We could ask for a wider scope.** The authorization URL is checked for the
   read-only scope, for the *absence* of every write scope, and for the two
   parameters that belong to the web-server flow rather than the installed-app
   flow this is.
2. **The credential could leak.** The refresh token file is checked for its mode,
   the action log is read back looking for a token or an event title, and the
   event titles are checked to never reach it.
3. **The handshake could be replayable.** The `state` is checked for single use, a
   ten-minute life, a bound on how many can exist, and a PKCE challenge that
   really is the SHA-256 of the verifier presented at the exchange.
4. **A failure could cost the student the rest of the brief.** Google is stubbed
   at the HTTP seam for the unhappy paths, and what is asserted is what a student
   would see: the deadlines stay, and the calendar says why it is quiet.
"""

import base64
import email.message
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import gcal  # noqa: E402
import server  # noqa: E402
from memory import MemoryStore  # noqa: E402
from quiz import QuizStore  # noqa: E402
from scheduler import compose_brief  # noqa: E402

CLIENT_ID = "1234.apps.googleusercontent.com"
CLIENT_SECRET = "not-a-real-secret"
TODAY = date(2026, 10, 16)

# A fixed "now" so nothing here waits for a clock. Friday 16 October 2026, 09:00
# local, which is a slot a morning brief would plausibly be read in.
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc).astimezone()


def timed_event(event_id, summary, start_iso, end_iso, **extra):
    payload = {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    payload.update(extra)
    return payload


def all_day_event(event_id, summary, day, end_day=None):
    return {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "start": {"date": day},
        # Google's all-day end is exclusive.
        "end": {"date": end_day or day},
    }


def iso(moment):
    """An RFC3339 timestamp with an offset, the way Google sends them."""
    return moment.astimezone().isoformat()


class FakeGoogle:
    """The three endpoints this module calls, as one scriptable stand-in.

    A `transport` rather than a patched function: it is handed to `gcal` exactly
    where urllib would be, so the URL building, the form encoding and the parsing
    under test are the real ones. Every call is recorded, because half the
    assertions here are about what we *asked for*.
    """

    def __init__(self, events=None, more=False, token_status=200, token_body=None,
                 refresh_body=None, events_status=200, events_text=None,
                 calendars=None, calendar_list_status=200,
                 events_by_calendar=None, failing_calendars=None):
        self.events = events if events is not None else []
        self.more = more
        self.token_status = token_status
        self.token_body = token_body
        self.refresh_body = refresh_body
        self.events_status = events_status
        self.events_text = events_text
        # None means "the account has one calendar", which is what every test
        # written before multi-calendar reading assumed.
        self.calendars = calendars
        self.calendar_list_status = calendar_list_status
        self.events_by_calendar = events_by_calendar
        self.failing_calendars = failing_calendars or set()
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        if "/token" in url:
            payload = body or ""
            if "grant_type=refresh_token" in payload:
                if self.refresh_body is not None:
                    return self.token_status, self.refresh_body
                return 200, json.dumps(
                    {"access_token": "fresh-access", "expires_in": 3600}
                )
            if self.token_body is not None:
                return self.token_status, self.token_body
            return 200, json.dumps(
                {
                    "access_token": "first-access",
                    "refresh_token": "the-refresh-token",
                    "expires_in": 3600,
                    "scope": gcal.SCOPE,
                }
            )
        if "/calendarList" in url:
            if self.calendar_list_status != 200:
                return self.calendar_list_status, json.dumps(
                    {"error": {"code": 403, "message": "forbidden"}}
                )
            items = self.calendars
            if items is None:
                items = [{"id": "primary", "summary": "Primary", "primary": True}]
            return 200, json.dumps({"items": list(items)})
        if "/events" in url:
            calendar_id = urllib.parse.unquote(
                url.split("/calendars/")[1].split("/events")[0]
            )
            if calendar_id in self.failing_calendars:
                return 403, json.dumps(
                    {"error": {"code": 403, "message": "forbidden"}}
                )
            if self.events_text is not None:
                return self.events_status, self.events_text
            items = self.events
            if self.events_by_calendar is not None:
                items = self.events_by_calendar.get(calendar_id, [])
            payload = {"items": list(items)}
            if self.more:
                payload["nextPageToken"] = "another-page"
            return self.events_status, json.dumps(payload)
        raise AssertionError("unexpected URL: {0}".format(url))

    @property
    def urls(self):
        return [call["url"] for call in self.calls]


def never_call(method, url, headers, body, timeout):
    raise AssertionError("a network call was made: {0} {1}".format(method, url))


class CalendarTestCase(unittest.TestCase):
    """Shared scratch space: a token file, an env, and the module caches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = {
            key: os.environ.get(key)
            for key in (gcal.CLIENT_ID_ENV, gcal.CLIENT_SECRET_ENV, gcal.TOKEN_PATH_ENV)
        }
        for key in self._env:
            os.environ.pop(key, None)
        self._calendar = server.CALENDAR
        self._pending = server.CALENDAR_PENDING
        self._demo = server.DEMO_MODE
        self._memory = server.MEMORY
        self._progress = server.PROGRESS
        self._log_path = server.LOG_PATH
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

        server.CALENDAR = gcal.TokenStore(self.root / "google-token.json")
        server.CALENDAR_PENDING = gcal.PendingFlows()
        server.MEMORY = MemoryStore(self.root / "memory").load()
        server.PROGRESS = QuizStore(self.root / "progress").load()
        server.LOG_PATH = self.root / "action-log.jsonl"
        gcal.forget()

    def tearDown(self):
        server.CALENDAR = self._calendar
        server.CALENDAR_PENDING = self._pending
        server.DEMO_MODE = self._demo
        server.MEMORY = self._memory
        server.PROGRESS = self._progress
        server.LOG_PATH = self._log_path
        gcal.forget()
        for key, value in self._env.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        sys.stdout = self._stdout
        self.tmp.cleanup()

    # --- helpers ---

    def configured(self, secret=True):
        os.environ[gcal.CLIENT_ID_ENV] = CLIENT_ID
        if secret:
            os.environ[gcal.CLIENT_SECRET_ENV] = CLIENT_SECRET

    def connected(self):
        """A store holding a token, without going through Google."""
        self.configured()
        server.CALENDAR.save(
            {
                "access_token": "live-access",
                "refresh_token": "the-refresh-token",
                "expires_at": 2_000_000_000,
                "scope": gcal.SCOPE,
            }
        )
        return server.CALENDAR

    def log_text(self):
        path = self.root / "action-log.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def log_events(self, name):
        return [
            json.loads(line)
            for line in self.log_text().splitlines()
            if line.strip() and json.loads(line).get("event") == name
        ]

    def start_flow(self):
        """Begin a connect in the server -> the state it issued."""
        status, _, head = HandlerStub("/oauth/google/start").send()
        self.assertEqual(status, 302)
        location = head.get("location", "")
        query = location.split("?", 1)[1]
        return dict(part.split("=", 1) for part in query.split("&"))["state"]


# --------------------------------------------------------------- the scope --


class TestTheScope(CalendarTestCase):
    """The promise in one sentence, checked against what we actually send."""

    def test_the_only_scope_is_the_read_only_one(self):
        self.assertTrue(gcal.SCOPE.endswith("/auth/calendar.readonly"))
        self.assertNotIn("calendar.events", gcal.SCOPE)
        self.assertNotIn("calendar\"", gcal.SCOPE)

    def test_the_authorization_request_asks_for_nothing_wider(self):
        self.configured()
        url = gcal.authorize_url(state="s", verifier="v", port=8787)
        self.assertEqual(url.count("scope="), 1)
        self.assertIn(
            "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar.readonly", url
        )
        for forbidden in ("auth/calendar+", "auth/calendar%20", "calendar.events", "calendar.readonly+"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, url)

    def test_it_sends_no_web_server_flow_parameters(self):
        """`access_type`, `prompt` and `include_granted_scopes` are not ours.

        Google's installed-app documentation says refresh tokens are always
        returned for that client type and that incremental authorization is not
        supported there, so sending these would be cargo cult - and
        `prompt=consent` on every connect would be a second consent screen for a
        grant we already hold.
        """
        self.configured()
        url = gcal.authorize_url(state="s", verifier="v", port=8787)
        self.assertNotIn("access_type", url)
        self.assertNotIn("prompt=", url)
        self.assertNotIn("include_granted_scopes", url)

    def test_the_redirect_is_loopback_with_no_path(self):
        """The form Google documents for this client type, and it must match."""
        self.assertEqual(gcal.redirect_uri(8787), "http://127.0.0.1:8787")
        self.configured()
        url = gcal.authorize_url(state="s", verifier="v", port=8787)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A8787", url)
        self.assertNotIn("callback", url)

    def test_a_client_id_alone_is_enough(self):
        """A Desktop client is a public client; a missing secret is not a fault."""
        self.configured(secret=False)
        self.assertTrue(gcal.configured())

    def test_the_secret_is_sent_only_when_there_is_one(self):
        fake = FakeGoogle()
        self.configured(secret=False)
        gcal.exchange_code("code-1", verifier="v", port=8787, transport=fake)
        self.assertNotIn("client_secret", fake.calls[-1]["body"])

        fake = FakeGoogle()
        self.configured(secret=True)
        gcal.exchange_code("code-1", verifier="v", port=8787, transport=fake)
        self.assertIn("client_secret=", fake.calls[-1]["body"])

    def test_what_google_granted_is_reported_as_a_fact(self):
        """The log line says read-only because the token response says so."""
        self.configured()
        stub = HandlerStub("/?code=abc&state=%s" % self.start_flow())
        original = gcal.exchange_code
        gcal.exchange_code = lambda *a, **k: {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 1.0,
            "scope": gcal.SCOPE,
        }
        try:
            stub.send()
        finally:
            gcal.exchange_code = original
        self.assertTrue(self.log_events("calendar_connected")[0]["read_only"])


# ------------------------------------------------------------ the handshake --


class TestTheHandshake(CalendarTestCase):
    def test_pkce_challenge_is_the_hash_of_the_verifier(self):
        """Not a decorative parameter: the exchange will only work if it matches."""
        state, verifier = gcal.PendingFlows().begin()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.configured()
        url = gcal.authorize_url(state=state, verifier=verifier, port=8787)
        self.assertIn("code_challenge=" + expected, url)
        self.assertIn("code_challenge_method=S256", url)

    def test_the_flow_sends_the_verifier_at_the_exchange(self):
        fake = FakeGoogle()
        self.configured()
        gcal.exchange_code("the-code", verifier="the-verifier", port=8787, transport=fake)
        body = fake.calls[-1]["body"]
        self.assertIn("code_verifier=the-verifier", body)
        self.assertIn("code=the-code", body)
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("client_id=" + CLIENT_ID, body)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A8787", body)
        self.assertEqual(fake.calls[-1]["method"], "POST")
        self.assertIn(
            "application/x-www-form-urlencoded", fake.calls[-1]["headers"]["Content-Type"]
        )

    def test_a_state_is_good_once(self):
        flows = gcal.PendingFlows()
        state, verifier = flows.begin()
        self.assertEqual(flows.consume(state), verifier)
        self.assertIsNone(flows.consume(state), "a replayed state was accepted")

    def test_an_expired_state_is_not_good(self):
        flows = gcal.PendingFlows(ttl=600)
        state, _ = flows.begin(now=1000.0)
        self.assertIsNone(flows.consume(state, now=1000.0 + 601))
        self.assertIsNone(flows.consume(state, now=1000.0 + 601))

    def test_a_state_we_never_issued_is_refused(self):
        flows = gcal.PendingFlows()
        flows.begin()
        self.assertIsNone(flows.consume("not-ours"))

    def test_the_pile_of_pending_flows_is_bounded(self):
        flows = gcal.PendingFlows(limit=4)
        for _ in range(9):
            flows.begin()
        self.assertLessEqual(len(flows._flows), 4)

    def test_two_connects_do_not_share_a_state_or_a_verifier(self):
        flows = gcal.PendingFlows()
        first = flows.begin()
        second = flows.begin()
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_the_verifier_is_long_enough_to_be_entropy(self):
        """RFC 7636 wants 43+ characters; this is 96 bytes of urlsafe base64."""
        _, verifier = gcal.PendingFlows().begin()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertRegex(verifier, r"^[A-Za-z0-9\-_]+$")


# --------------------------------------------------------------- the tokens --


class TestTheTokens(CalendarTestCase):
    def test_the_token_file_is_private(self):
        store = self.connected()
        self.assertTrue(store.path.exists())
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_token_file_is_not_connected_rather_than_an_error(self):
        import re as _re
        self.connected().path.write_text("{not json", encoding="utf-8")
        store = server.CALENDAR
        self.assertFalse(store.connected)
        self.assertEqual(store.load(), {})
        payload = gcal.section(store, today=TODAY, transport=never_call)
        self.assertFalse(payload["connected"])

    def test_expiry_is_an_absolute_time_from_expires_in(self):
        fake = FakeGoogle()
        self.configured()
        tokens = gcal.exchange_code("c", verifier="v", port=8787, transport=fake)
        self.assertIn("expires_at", tokens)
        self.assertAlmostEqual(tokens["expires_at"] - tokens["obtained_at"], 3600, delta=5)
        self.assertEqual(tokens["refresh_token"], "the-refresh-token")

    def test_a_token_body_without_expires_in_does_not_produce_a_broken_expiry(self):
        fake = FakeGoogle(token_body=json.dumps({"access_token": "a"}))
        self.configured()
        tokens = gcal.exchange_code("c", verifier="v", port=8787, transport=fake)
        self.assertGreater(tokens["expires_at"], tokens["obtained_at"])

    def test_a_body_without_an_access_token_is_refused_rather_than_stored(self):
        fake = FakeGoogle(token_body=json.dumps({"nothing": True}))
        self.configured()
        with self.assertRaises(gcal.CalendarError) as caught:
            gcal.exchange_code("c", verifier="v", port=8787, transport=fake)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_a_fresh_access_token_is_reused(self):
        store = self.connected()
        store.save({"expires_at": 9_999_999_999})
        self.assertEqual(store.access_token(transport=never_call), "live-access")

    def test_a_token_near_expiry_is_refreshed_early(self):
        """Refreshing early keeps a 401 from surfacing inside a brief."""
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 30})
        fake = FakeGoogle()
        self.assertEqual(store.access_token(transport=fake), "fresh-access")
        self.assertEqual(len([url for url in fake.urls if "/token" in url]), 1)

    def test_refreshing_keeps_the_refresh_token_google_did_not_reissue(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() - 1})
        fake = FakeGoogle()  # its refresh body carries no refresh_token
        store.access_token(transport=fake)
        self.assertEqual(store.load()["refresh_token"], "the-refresh-token")

    def test_an_invalid_grant_is_a_sentence_about_reconnecting(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() - 1})
        fake = FakeGoogle(
            token_status=400,
            refresh_body=json.dumps({"error": "invalid_grant"}),
        )
        with self.assertRaises(gcal.CalendarError) as caught:
            store.access_token(transport=fake)
        self.assertEqual(caught.exception.code, "reconnect")
        self.assertIn("Connect it again", str(caught.exception))

    def test_disconnecting_removes_the_file_and_is_idempotent(self):
        store = self.connected()
        self.assertTrue(store.clear())
        self.assertFalse(store.path.exists())
        self.assertFalse(store.clear())

    def test_a_refused_request_is_reported_not_retried(self):
        self.configured()
        fake = FakeGoogle(
            token_status=401,
            refresh_body=json.dumps({"error": "unauthorized_client"}),
        )
        with self.assertRaises(gcal.CalendarError) as caught:
            gcal.refresh_access("r", transport=fake)
        self.assertEqual(caught.exception.code, "refused")
        self.assertEqual(len(fake.calls), 1, "a refusal was retried")


# --------------------------------------------------------------- the events --


class TestTheEvents(CalendarTestCase):
    def test_a_timed_event_keeps_its_day_and_time(self):
        events = gcal.parse_events(
            [timed_event("1", "Chemistry lab", iso(NOW + timedelta(days=1, hours=5)),
                         iso(NOW + timedelta(days=1, hours=7)))],
            today=TODAY,
            now=NOW,
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["title"], "Chemistry lab")
        self.assertEqual(event["day"], (TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(event["days_away"], 1)
        self.assertFalse(event["all_day"])
        self.assertRegex(event["time"], r"^\d\d:\d\d$")

    def test_an_all_day_event_has_no_time(self):
        events = gcal.parse_events(
            [all_day_event("2", "Reading week", (TODAY + timedelta(days=3)).isoformat())],
            today=TODAY,
            now=NOW,
        )
        self.assertTrue(events[0]["all_day"])
        self.assertIsNone(events[0]["time"])
        self.assertEqual(events[0]["days_away"], 3)

    def test_an_all_day_end_date_is_exclusive_and_never_shown_as_a_day(self):
        """Google's all-day `end.date` is the day *after* the last one.

        Two days off is the classic bug here, so the assertion is about what the
        student would see: a one-day event on the 16th, not "until the 17th".
        """
        payload = gcal.compose(
            gcal.parse_events(
                [all_day_event("3", "Holiday", TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat())],
                today=TODAY,
                now=NOW,
            ),
            today=TODAY,
        )
        shown = payload["events"][0]
        self.assertEqual(shown["day"], TODAY.isoformat())
        # The day after the event is Google's exclusive end. It must not appear in
        # anything the student sees, including anything the panel is handed.
        self.assertNotIn("17", json.dumps(shown))
        self.assertNotIn("17", payload["lines"][0])

    def test_a_cancelled_event_is_not_shown(self):
        cancelled = timed_event(
            "4", "Cancelled seminar", iso(NOW + timedelta(days=1)), iso(NOW + timedelta(days=1, hours=1))
        )
        cancelled["status"] = "cancelled"
        self.assertEqual(gcal.parse_events([cancelled], today=TODAY, now=NOW), [])

    def test_an_event_with_no_title_is_still_an_event(self):
        """Usually a busy block; hiding it would make the day look emptier."""
        item = timed_event("5", "", iso(NOW + timedelta(days=2)), iso(NOW + timedelta(days=2, hours=1)))
        item.pop("summary")
        events = gcal.parse_events([item], today=TODAY, now=NOW)
        self.assertEqual(events[0]["title"], "(no title)")

    def test_a_zulu_timestamp_parses_on_every_supported_python(self):
        """3.9's fromisoformat rejects a trailing Z; 3.11 accepts it."""
        events = gcal.parse_events(
            [timed_event("6", "Lecture", "2026-10-17T14:00:00Z", "2026-10-17T15:00:00Z")],
            today=TODAY,
            now=NOW.astimezone(timezone.utc),
        )
        self.assertEqual(events[0]["day"], "2026-10-17")
        self.assertIsNotNone(events[0]["time"])

    def test_an_offset_timestamp_parses_too(self):
        events = gcal.parse_events(
            [timed_event("7", "Lecture", "2026-10-17T14:00:00+05:30", "2026-10-17T15:00:00+05:30")],
            today=TODAY,
            now=NOW,
        )
        self.assertEqual(events[0]["day"], "2026-10-17")

    def test_an_event_with_no_readable_date_is_dropped_rather_than_guessed(self):
        broken = {"id": "8", "summary": "Nowhere", "start": {"dateTime": "not a time"}, "end": {}}
        self.assertEqual(gcal.parse_events([broken], today=TODAY, now=NOW), [])

    def test_events_come_back_in_the_order_they_happen(self):
        later = timed_event("b", "Second", iso(NOW + timedelta(days=2)), iso(NOW + timedelta(days=2, hours=1)))
        sooner = timed_event("a", "First", iso(NOW + timedelta(days=1)), iso(NOW + timedelta(days=1, hours=1)))
        events = gcal.parse_events([later, sooner], today=TODAY, now=NOW)
        self.assertEqual([event["title"] for event in events], ["First", "Second"])

    def test_an_event_happening_now_is_flagged_rather_than_hidden(self):
        events = gcal.parse_events(
            [timed_event("9", "Lecture", iso(NOW - timedelta(minutes=30)), iso(NOW + timedelta(minutes=30)))],
            today=TODAY,
            now=NOW,
        )
        self.assertTrue(events[0]["ongoing"])

    def test_the_request_window_is_the_documented_parameters(self):
        fake = FakeGoogle()
        self.configured()
        gcal.fetch_events("token", now=NOW, horizon_days=7, transport=fake)
        url = fake.urls[-1]
        self.assertIn("/calendars/primary/events?", url)
        self.assertIn("singleEvents=true", url)
        self.assertIn("orderBy=startTime", url)
        self.assertIn("timeMin=2026-10-16T09%3A00%3A00Z", url)
        self.assertIn("timeMax=2026-10-23T09%3A00%3A00Z", url)
        self.assertEqual(fake.calls[-1]["headers"]["Authorization"], "Bearer token")

    def test_a_full_page_is_reported_as_having_more(self):
        self.configured()
        _, more = gcal.fetch_events("token", now=NOW, transport=FakeGoogle(more=True))
        self.assertTrue(more)

    def test_an_events_failure_keeps_its_code(self):
        self.configured()
        fake = FakeGoogle(events_status=503, events_text="upstream down")
        with self.assertRaises(gcal.CalendarError) as caught:
            gcal.fetch_events("token", now=NOW, transport=fake)
        self.assertEqual(caught.exception.code, "bad_response")
        self.assertIn("503", str(caught.exception))


# ------------------------------------------------------------- the phrasing --


class TestTheLines(CalendarTestCase):
    def lines(self, items, horizon=7):
        events = gcal.parse_events(items, today=TODAY, now=NOW)
        return gcal.compose(events, today=TODAY, horizon_days=horizon)

    def test_today_tomorrow_and_in_n_days(self):
        payload = self.lines(
            [
                timed_event("1", "Today thing", iso(NOW + timedelta(hours=4)), iso(NOW + timedelta(hours=5))),
                timed_event("2", "Tomorrow thing", iso(NOW + timedelta(days=1, hours=4)), iso(NOW + timedelta(days=1, hours=5))),
                timed_event("3", "Later thing", iso(NOW + timedelta(days=3, hours=4)), iso(NOW + timedelta(days=3, hours=5))),
            ]
        )
        self.assertEqual(len(payload["lines"]), 3)
        self.assertTrue(payload["lines"][0].startswith("today — Today thing"))
        self.assertTrue(payload["lines"][1].startswith("tomorrow — Tomorrow thing"))
        self.assertTrue(payload["lines"][2].startswith("in 3 days — Later thing"))

    def test_an_ongoing_event_leads_with_now(self):
        payload = self.lines(
            [timed_event("1", "Lecture", iso(NOW - timedelta(minutes=10)), iso(NOW + timedelta(minutes=50)))]
        )
        self.assertTrue(payload["lines"][0].startswith("now — Lecture (until "))

    def test_an_all_day_event_says_so(self):
        payload = self.lines([all_day_event("1", "Reading week", TODAY.isoformat())])
        self.assertIn("(all day)", payload["lines"][0])

    def test_a_location_is_shown_when_there_is_one(self):
        event = timed_event(
            "1", "Lab", iso(NOW + timedelta(days=1, hours=2)), iso(NOW + timedelta(days=1, hours=3)),
            location="Cavendish 2.14",
        )
        payload = self.lines([event])
        self.assertIn("Cavendish 2.14", payload["lines"][0])

    def test_events_past_the_horizon_are_left_out(self):
        payload = self.lines(
            [timed_event("1", "Far off", iso(NOW + timedelta(days=20)), iso(NOW + timedelta(days=20, hours=1)))],
            horizon=7,
        )
        self.assertEqual(payload["lines"], [])
        self.assertEqual(payload["counts"]["events"], 0)

    def test_the_list_is_capped_and_says_when_it_is(self):
        items = [
            timed_event(str(index), "Event {0}".format(index),
                        iso(NOW + timedelta(days=1, hours=index)), iso(NOW + timedelta(days=1, hours=index + 1)))
            for index in range(gcal.MAX_EVENTS + 4)
        ]
        payload = self.lines(items)
        self.assertEqual(len(payload["lines"]), gcal.MAX_EVENTS)
        self.assertTrue(payload["counts"]["more"])

    def test_the_section_declares_itself_free_and_read_only(self):
        payload = self.lines([])
        self.assertEqual(payload["cost_usd"], 0.0)
        self.assertEqual(payload["source"], "google-calendar")
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["scope"], gcal.SCOPE)

    def test_no_line_ever_contains_a_placeholder(self):
        payload = self.lines(
            [
                timed_event("1", "Thing", iso(NOW + timedelta(days=1)), iso(NOW + timedelta(days=1, hours=1))),
                all_day_event("2", "Other", TODAY.isoformat()),
            ]
        )
        for line in payload["lines"]:
            with self.subTest(line=line):
                self.assertNotIn("None", line)
                self.assertNotIn("undefined", line)
                self.assertNotIn("  ", line)

    def test_the_shape_is_stable_for_the_panel(self):
        payload = self.lines([])
        for key in (
            "connected", "read_only", "scope", "calendar", "horizon_days",
            "events", "lines", "counts", "cost_usd", "source",
        ):
            self.assertIn(key, payload)


# --------------------------------------------------------- the three silences --


class TestTheQuietPaths(CalendarTestCase):
    def test_with_no_oauth_client_nothing_is_even_requested(self):
        """A request we know cannot succeed should not be made."""
        payload = gcal.section(server.CALENDAR, today=TODAY, transport=never_call)
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["lines"], [])

    def test_configured_but_not_connected_makes_no_request_either(self):
        self.configured()
        payload = gcal.section(server.CALENDAR, today=TODAY, transport=never_call)
        self.assertFalse(payload["connected"])
        self.assertTrue(payload["configured"])

    def test_connected_reads_once_and_then_reuses_the_answer(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 3600})
        fake = FakeGoogle(events=[all_day_event("1", "Seminar", TODAY.isoformat())])
        first = gcal.section(store, today=TODAY, now=NOW, transport=fake)
        second = gcal.section(store, today=TODAY, now=NOW, transport=fake)
        self.assertEqual(len([url for url in fake.urls if "/events" in url]), 1)
        self.assertEqual(first["lines"], second["lines"])
        self.assertEqual(first["lines"][0], "today — Seminar (all day)")

    def test_unreachable_is_a_sentence_and_the_deadlines_survive(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 3600})

        def refuse(method, url, headers, body, timeout):
            raise gcal.CalendarError("could not reach Google Calendar: timed out", "unreachable")

        payload = gcal.section(store, today=TODAY, now=NOW, transport=refuse)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["error"]["code"], "unreachable")
        self.assertIn("Google Calendar", payload["error"]["message"])

    def test_a_dead_refresh_token_disconnects_so_the_fix_is_one_click(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() - 1})  # force a refresh that fails
        fake = FakeGoogle(token_status=400, refresh_body=json.dumps({"error": "invalid_grant"}))
        payload = gcal.section(store, today=TODAY, now=NOW, transport=fake)
        self.assertFalse(payload["connected"])
        self.assertTrue(payload["configured"])
        self.assertIn("Connect it again", payload["explanation"])
        self.assertFalse(store.path.exists(), "a rejected credential was kept")

    def test_the_section_is_cached_only_while_it_is_good(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 3600})
        fake = FakeGoogle(events=[all_day_event("1", "Seminar", TODAY.isoformat())])
        gcal.section(store, today=TODAY, now=NOW, transport=fake)
        # The key says `all` because the payload now covers every calendar the
        # account can read; leaving it on one calendar's id would have served a
        # cached primary-only week to someone who just added a second calendar.
        self.assertIsNotNone(gcal.cached("calendar:all:7"))
        gcal.forget()
        self.assertIsNone(gcal.cached("calendar:all:7"))


class TestMoreThanOneCalendar(CalendarTestCase):
    """Reading every calendar, not just `primary`.

    The reason this exists: a week that lives on a course calendar is invisible to
    an integration that only asks Google for `calendars/primary/events`. The page
    looks empty while the student is busy, which is the worst way for a feature to
    be wrong - it looks like good news.
    """

    PRIMARY = {"id": "primary", "summary": "Primary", "primary": True}
    WORK = {"id": "work@group.calendar.google.com", "summary": "Work"}

    def setUp(self):
        super().setUp()
        import time as _time

        self.store = self.connected()
        self.store.save({"expires_at": _time.time() + 3600})

    # -- helpers ------------------------------------------------------------

    def event(self, event_id, summary, **delta):
        start = NOW + timedelta(**delta)
        return timed_event(event_id, summary, iso(start), iso(start + timedelta(hours=1)))

    def read(self, fake, **kwargs):
        return gcal.section(
            self.store, today=TODAY, now=NOW, transport=fake, use_cache=False, **kwargs
        )

    def event_urls(self, fake):
        return [url for url in fake.urls if "/events" in url]

    # -- the behaviour ------------------------------------------------------

    def test_it_reads_every_calendar_the_account_can_see(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={
                "primary": [self.event("home", "Seminar", hours=1)],
                "work@group.calendar.google.com": [self.event("w1", "Standup", hours=2)],
            },
        )
        payload = self.read(fake)

        self.assertEqual([event["title"] for event in payload["events"]], ["Seminar", "Standup"])
        self.assertEqual(len(self.event_urls(fake)), 2)
        self.assertTrue(any("/calendars/primary/events" in url for url in fake.urls))
        self.assertTrue(
            any("work%40group.calendar.google.com" in url for url in fake.urls),
            "the second calendar's id was not escaped into its URL",
        )
        self.assertEqual([cal["name"] for cal in payload["calendars"]], ["Primary", "Work"])

    def test_a_line_names_its_calendar_only_when_more_than_one_contributed(self):
        two = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={
                "primary": [self.event("home", "Seminar", hours=1)],
                "work@group.calendar.google.com": [self.event("w1", "Standup", hours=2)],
            },
        )
        lines = self.read(two)["lines"]
        self.assertTrue(any(line.endswith(" · Work") for line in lines), lines)
        self.assertTrue(any(line.endswith(" · Primary") for line in lines), lines)

        one = FakeGoogle(events=[self.event("home", "Seminar", hours=1)])
        lines = self.read(one)["lines"]
        self.assertEqual(lines, ["today — Seminar (" + (NOW + timedelta(hours=1)).strftime("%H:%M") + ")"])
        self.assertNotIn(" · ", lines[0], "a single calendar was labelled anyway")

    def test_events_are_sorted_together_rather_than_calendar_by_calendar(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={
                "primary": [self.event("later", "Essay", days=2)],
                "work@group.calendar.google.com": [self.event("sooner", "Standup", hours=1)],
            },
        )
        titles = [event["title"] for event in self.read(fake)["events"]]
        self.assertEqual(titles, ["Standup", "Essay"])

    def test_hidden_and_deleted_calendars_are_not_read(self):
        fake = FakeGoogle(
            calendars=[
                self.PRIMARY,
                {"id": "holidays@x", "summary": "Holidays", "hidden": True},
                {"id": "gone@x", "summary": "Old calendar", "deleted": True},
            ]
        )
        self.read(fake)
        read_ids = [
            url.split("/calendars/")[1].split("/events")[0] for url in self.event_urls(fake)
        ]
        self.assertEqual(read_ids, ["primary"], "a hidden or deleted calendar was read")

    def test_googles_generated_calendars_are_skipped(self):
        """Holidays and weather are generated, not kept by anyone.

        Found in the first live run: `en.usa#holiday@group.v.calendar.google.com`
        was among the account's calendars, and in December its all-day entries
        would fill the ten-line cap with Christmas instead of classes.
        """
        fake = FakeGoogle(
            calendars=[
                self.PRIMARY,
                {"id": "en.usa#holiday@group.v.calendar.google.com", "summary": "Holidays in United States"},
                {"id": "en.usa#weather@group.v.calendar.google.com", "summary": "Weather"},
                {"id": "classroom1001823676541424300@group.calendar.google.com", "summary": "SAT Course"},
            ]
        )
        payload = self.read(fake)
        read_ids = [
            url.split("/calendars/")[1].split("/events")[0] for url in self.event_urls(fake)
        ]
        self.assertNotIn("en.usa#holiday@group.v.calendar.google.com", [
            urllib.parse.unquote(calendar_id) for calendar_id in read_ids
        ])
        self.assertEqual(
            [cal["name"] for cal in payload["calendars"]], ["Primary", "SAT Course"]
        )

    def test_the_subscription_test_is_by_id_not_by_localised_name(self):
        self.assertTrue(gcal.is_subscription_calendar("en.usa#holiday@group.v.calendar.google.com"))
        self.assertTrue(gcal.is_subscription_calendar("de.german#weather@group.v.calendar.google.com"))
        self.assertFalse(gcal.is_subscription_calendar("classroom1001823676541424300@group.calendar.google.com"))
        self.assertFalse(gcal.is_subscription_calendar("yojithm22@gmail.com"))

    def test_the_same_event_on_two_calendars_is_shown_once(self):
        shared = self.event("shared-id", "Chemistry lab", hours=1)
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={
                "primary": [shared],
                "work@group.calendar.google.com": [shared],
            },
        )
        payload = self.read(fake)
        self.assertEqual([event["title"] for event in payload["events"]], ["Chemistry lab"])

    def test_the_total_cap_applies_across_calendars_not_per_calendar(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={
                "primary": [self.event("p{0}".format(i), "P{0}".format(i), hours=i + 1) for i in range(8)],
                "work@group.calendar.google.com": [
                    self.event("w{0}".format(i), "W{0}".format(i), hours=i + 1) for i in range(8)
                ],
            },
        )
        payload = self.read(fake)
        self.assertEqual(len(payload["lines"]), gcal.MAX_EVENTS)
        self.assertTrue(payload["counts"]["more"], "sixteen events were hidden as if there were ten")

    def test_one_broken_calendar_does_not_take_the_week_with_it(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            events_by_calendar={"primary": [self.event("home", "Seminar", hours=1)]},
            failing_calendars={"work@group.calendar.google.com"},
        )
        payload = self.read(fake)

        self.assertEqual([event["title"] for event in payload["events"]], ["Seminar"])
        self.assertNotIn("error", payload)
        self.assertEqual(payload["calendar_errors"][0]["calendar"], "Work")
        self.assertEqual(payload["calendar_errors"][0]["code"], "refused")

    def test_every_calendar_failing_is_an_error_not_an_empty_week(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY, self.WORK],
            failing_calendars={"primary", "work@group.calendar.google.com"},
        )
        payload = self.read(fake)

        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["error"]["code"], "refused")
        self.assertIn("Google refused", payload["error"]["message"])

    def test_a_failed_calendar_list_still_reads_the_primary(self):
        fake = FakeGoogle(
            calendar_list_status=403,
            events=[self.event("home", "Seminar", hours=1)],
        )
        payload = self.read(fake)

        self.assertNotIn("error", payload)
        self.assertEqual(len(payload["lines"]), 1)
        self.assertEqual([cal["id"] for cal in payload["calendars"]], ["primary"])

    def test_it_stops_after_a_few_calendars(self):
        fake = FakeGoogle(
            calendars=[self.PRIMARY]
            + [{"id": "c{0}@x".format(i), "summary": "C{0}".format(i)} for i in range(12)]
        )
        self.read(fake)
        self.assertLessEqual(len(self.event_urls(fake)), gcal.MAX_CALENDARS)

    def test_the_list_request_asks_only_for_calendars_it_can_read(self):
        fake = FakeGoogle()
        self.read(fake)
        list_url = [url for url in fake.urls if "/calendarList" in url][0]
        self.assertIn("minAccessRole=reader", list_url)
        self.assertNotIn("calendar+events", list_url)


# --------------------------------------------------------------- the server --


class HandlerStub(server.Handler):
    """The real handler, without a socket.

    Different from the other suites' stub in one way that matters here: this one
    returns the response **headers** as well as the body, because two of these
    routes answer with a redirect and one answers with HTML rather than JSON.
    """

    def __init__(self, path="/calendar", body=None, command="GET", authorization=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.command = command
        self.path = path
        self.request_version = "HTTP/1.0"
        self.requestline = "{0} {1} HTTP/1.0".format(command, path)
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = email.message_from_string(
            "Content-Length: {0}\r\n".format(len(raw))
            + ("Authorization: {0}\r\n".format(authorization) if authorization else "")
        )
        self.client_address = ("127.0.0.1", 0)
        self.server = None

    def log_message(self, *args):
        pass

    def send(self):
        getattr(self, "do_" + self.command)()
        text = self.wfile.getvalue().decode("utf-8")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(": ")
            headers[key.lower()] = value
        return status, body, headers


class TestTheRoutes(CalendarTestCase):
    def test_calendar_reports_the_off_state_without_a_client_id(self):
        status, body, headers = HandlerStub("/calendar").send()
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_health_carries_the_two_booleans_and_the_scope(self):
        status, body, _ = HandlerStub("/health").send()
        self.assertEqual(status, 200)
        calendar = json.loads(body)["calendar"]
        self.assertEqual(calendar["configured"], False)
        self.assertEqual(calendar["connected"], False)
        self.assertTrue(calendar["read_only"])
        self.assertEqual(calendar["scope"], gcal.SCOPE)

    def test_starting_a_connect_without_a_client_id_says_what_to_set(self):
        status, body, _ = HandlerStub("/oauth/google/start").send()
        self.assertEqual(status, 400)
        self.assertIn("GOOGLE_CLIENT_ID", json.loads(body)["error"])

    def test_a_client_id_is_enough_to_start_and_it_redirects_to_google(self):
        self.configured(secret=False)
        status, body, headers = HandlerStub("/oauth/google/start").send()
        self.assertEqual(status, 302)
        self.assertTrue(headers["location"].startswith(gcal.AUTHORIZE_URL))
        self.assertIn("response_type=code", headers["location"])
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertNotIn("access_token", headers["location"], "a token in a URL is a token in a log")

    def test_the_state_issued_at_start_is_the_one_the_callback_needs(self):
        self.configured()
        state = self.start_flow()

        def unreachable(*args, **kwargs):
            raise gcal.CalendarError("could not reach Google Calendar: no route", "unreachable")

        original = gcal.exchange_code
        gcal.exchange_code = unreachable
        try:
            status, body, _ = HandlerStub("/?code=abc&state={0}".format(state)).send()
        finally:
            gcal.exchange_code = original
        # 400 because the exchange failed; what matters is *which* 400. The state
        # was accepted - the log names the network, not the state - and nothing was
        # stored from a handshake that did not finish.
        self.assertEqual(status, 400)
        self.assertEqual(self.log_events("calendar_callback")[0]["outcome"], "unreachable")
        self.assertNotIn("did not come from this server", body)
        self.assertFalse(server.CALENDAR.path.exists())

    def test_a_callback_with_a_state_we_never_issued_does_nothing(self):
        self.configured()
        status, body, _ = HandlerStub("/?code=abc&state=guessed").send()
        self.assertEqual(status, 400)
        self.assertIn("did not come from this server", body)
        self.assertFalse(server.CALENDAR.path.exists(), "a token was written anyway")
        self.assertEqual(self.log_events("calendar_callback")[0]["outcome"], "bad_state")

    def test_google_refusing_the_grant_is_reported_in_a_page(self):
        self.configured()
        state = self.start_flow()
        status, body, _ = HandlerStub(
            "/?error=access_denied&state={0}".format(state)
        ).send()
        self.assertEqual(status, 400)
        self.assertIn("access_denied", body)
        self.assertEqual(self.log_events("calendar_callback")[0]["outcome"], "refused")

    def test_a_good_callback_stores_the_token_privately(self):
        self.configured()
        state = self.start_flow()
        original = gcal.exchange_code
        gcal.exchange_code = lambda *a, **k: {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 1.0,
            "scope": gcal.SCOPE,
        }
        try:
            status, body, _ = HandlerStub("/?code=abc&state={0}".format(state)).send()
        finally:
            gcal.exchange_code = original
        self.assertEqual(status, 200)
        self.assertIn("read-only", body)
        self.assertTrue(server.CALENDAR.path.exists())
        self.assertEqual(server.CALENDAR.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(server.CALENDAR.path.read_text())["refresh_token"], "r")

    def test_a_code_and_state_are_the_callback_and_nothing_else_is(self):
        """`/` is also the panel. The two are told apart by the query, only."""
        self.configured()
        self.assertFalse(server.Handler._is_google_callback(HandlerStub("/")))
        self.assertFalse(server.Handler._is_google_callback(HandlerStub("/?demo=1")))
        self.assertTrue(server.Handler._is_google_callback(HandlerStub("/?code=x&state=y")))
        self.assertTrue(server.Handler._is_google_callback(HandlerStub("/?error=access_denied")))
        self.assertFalse(server.Handler._is_google_callback(HandlerStub("/demo")))

    def test_the_environment_never_becomes_a_callback(self):
        self.configured()
        status, body, _ = HandlerStub("/").send()
        self.assertEqual(status, 404)  # no panel build in the test's temp tree
        self.assertNotIn("Connected", body)

    def test_disconnecting_forgets_the_token_and_answers_with_the_state(self):
        self.connected()
        status, body, _ = HandlerStub("/calendar", command="DELETE").send()
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["removed"])
        self.assertFalse(payload["connected"])
        self.assertFalse(server.CALENDAR.path.exists())
        # Idempotent, because the likely cause of a second click is a second click.
        status, body, _ = HandlerStub("/calendar", command="DELETE").send()
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["removed"])

    def test_the_log_holds_counts_and_never_an_event_title(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 3600})
        fake = FakeGoogle(
            events=[
                all_day_event("1", "Dentist appointment", TODAY.isoformat()),
                timed_event("2", "Meeting with Sam", iso(NOW + timedelta(days=1, hours=3)),
                            iso(NOW + timedelta(days=1, hours=4)), location="Sam's office"),
            ]
        )
        original = gcal._call
        gcal._call = lambda method, url, **kwargs: fake(method, url, kwargs.get("headers"), kwargs.get("body"), kwargs.get("timeout"))
        try:
            HandlerStub("/calendar").send()
        finally:
            gcal._call = original

        text = self.log_text()
        self.assertIn("calendar_read", text)
        self.assertNotIn("Dentist", text)
        self.assertNotIn("Sam", text)
        self.assertNotIn("live-access", text)
        self.assertNotIn("the-refresh-token", text)
        entry = self.log_events("calendar_read")[0]
        self.assertEqual(entry["events"], 2)
        self.assertEqual(entry["all_day"], 1)

    def test_an_unreachable_google_is_logged_as_a_code_and_a_quiet_section(self):
        import time as _time
        store = self.connected()
        store.save({"expires_at": _time.time() + 3600})

        def refuse(method, url, headers, body, timeout):
            raise gcal.CalendarError("could not reach Google Calendar: nope", "unreachable")

        original = gcal._call
        gcal._call = lambda method, url, **kwargs: refuse(
            method, url, kwargs.get("headers"), kwargs.get("body"), kwargs.get("timeout")
        )
        try:
            status, body, _ = HandlerStub("/calendar").send()
        finally:
            gcal._call = original
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], "unreachable")
        self.assertEqual(self.log_events("calendar_error")[0]["code"], "unreachable")


class TestTheAgentCanReadTheCalendar(CalendarTestCase):
    """The wiring that was missing when the student asked and got "I don't know".

    The brief could see the calendar from Step 12; the agent could not, because
    there was no tool for it. These assert the bridge exists and goes through the
    same read - the same token store, the same demo refusal, and the window the
    question actually asked for.
    """

    def setUp(self):
        super().setUp()
        self.connected()
        self._original = gcal.section
        self.seen = []
        outer = self

        def fake_section(store, *, today, horizon_days=7, transport=None, **kwargs):
            outer.seen.append(horizon_days)
            return {
                "connected": True,
                "read_only": True,
                "calendars": [{"id": "primary", "name": "Primary"}],
                "horizon_days": horizon_days,
                "events": [
                    {
                        "title": "Chemistry lab",
                        "day": today.isoformat(),
                        "time": "14:00",
                        "all_day": False,
                        "days_away": 0,
                    }
                ],
                "lines": ["today — Chemistry lab (14:00)"],
                "counts": {"events": 1, "today": 1, "all_day": 0, "more": False},
                "cost_usd": 0.0,
                "source": "google-calendar",
            }

        gcal.section = fake_section
        self.addCleanup(setattr, gcal, "section", self._original)

    def test_the_agent_tool_reads_the_calendar_and_keeps_the_events(self):
        result = server._run_tool_for_turn("read_calendar", {})
        self.assertTrue(result["ok"], result)
        self.assertIn("Chemistry lab", result["text"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(self.seen, [server.BRIEF_HORIZON_DAYS])

    def test_the_question_decides_the_window(self):
        server._run_tool_for_turn("read_calendar", {"days": 14})
        self.assertEqual(self.seen, [14])

    def test_the_demo_cannot_read_the_operators_calendar_through_the_tool(self):
        server.DEMO_MODE = True
        result = server._run_tool_for_turn("read_calendar", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_connected")
        self.assertIn("demo", result["text"].lower())
        self.assertEqual(self.seen, [], "the demo read the calendar anyway")


class TestDemoModeIsNotTheOperatorsCalendar(CalendarTestCase):
    """The Step 10 lesson, applied to the most personal data here.

    A public demo serves the operator's own backend. A calendar on that backend
    would show a visitor the operator's dentist appointments, so the demo reports
    a calendar that does not exist rather than one that is not theirs - and it does
    not read the token file at all.
    """

    def test_the_demo_reports_no_calendar_even_when_one_is_connected(self):
        self.connected()
        server.DEMO_MODE = True
        payload = server.calendar_payload()
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["configured"])
        self.assertIn("demo", payload["explanation"])

    def test_the_demo_route_returns_the_same_thing(self):
        self.connected()
        server.DEMO_MODE = True
        status, body, _ = HandlerStub("/calendar").send()
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["connected"])

    def test_the_health_booleans_do_not_leak_a_connected_calendar_to_a_demo(self):
        self.connected()
        server.DEMO_MODE = True
        _, body, _ = HandlerStub("/health").send()
        calendar = json.loads(body)["calendar"]
        self.assertFalse(calendar["connected"])
        self.assertEqual(calendar["off"], "demo")

    def test_a_visitor_cannot_start_a_connect_on_the_demo(self):
        self.configured()
        server.DEMO_MODE = True
        status, body, _ = HandlerStub("/oauth/google/start").send()
        self.assertEqual(status, 400)
        self.assertIn("demo", json.loads(body)["error"])

    def test_a_callback_arriving_at_the_demo_does_not_exchange_anything(self):
        self.configured()
        server.DEMO_MODE = True
        calls = []
        original = gcal.exchange_code
        gcal.exchange_code = lambda *a, **k: calls.append(a)
        try:
            status, body, _ = HandlerStub("/?code=abc&state=whatever").send()
        finally:
            gcal.exchange_code = original
        self.assertEqual(status, 400)
        self.assertEqual(calls, [])
        self.assertEqual(self.log_events("calendar_callback")[0]["outcome"], "demo")


class TestTheBriefNextToIt(CalendarTestCase):
    """The calendar is the second thing in the brief and must not break the first."""

    def test_a_broken_calendar_leaves_the_deadlines_alone(self):
        """Not a calendar test - an ordering test, in the shape of one.

        The brief's own payload is composed from files and cannot fail; the
        calendar is fetched by a separate route. What this pins is that the
        deadlines half still works while the calendar half is unreachable.
        """
        brief = compose_brief(
            facts=[{"id": "f1", "text": "lab report 3 draft is due October 20"}],
            today=TODAY,
        )
        self.assertEqual(brief["counts"]["dated"], 1)
        self.assertIn("2026-10-20", brief["headline"])

    def test_the_calendar_is_asked_with_the_briefs_own_horizon(self):
        """Two windows for one student's week would be two different weeks.

        The brief's horizon is configurable (`CADENCE_BRIEF_DAYS`), so the assertion
        is not the number 7 - it is that the calendar is handed the brief's number
        rather than a second one that would drift from it.
        """
        seen = {}
        original = gcal.section
        gcal.section = lambda store, **kwargs: seen.update(kwargs) or gcal.not_connected_payload()
        try:
            server.calendar_payload()
        finally:
            gcal.section = original
        self.assertEqual(seen["horizon_days"], server.BRIEF_HORIZON_DAYS)
        self.assertEqual(server.DEFAULT_HORIZON_DAYS, gcal.DEFAULT_HORIZON_DAYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
