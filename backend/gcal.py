"""Google Calendar, read-only, for a backend that runs on your own machine.

The plan said "remind people about what is in their calendar". The safe form of
that sentence is *read* their calendar, and everything in this module follows from
one decision: **Cadence may look at your calendar and may never change it.**

Why that is the whole design
----------------------------
An assistant that can write to your calendar can double-book you, move an exam,
or cancel something you were relying on - and a reminder that is wrong is a
worse failure than a reminder that is missing. So the scope asked for is
`calendar.readonly` and it is the only scope requested anywhere in this project.
Even if a future bug rerouted a write through this module, Google would refuse
it, which makes "I never touched your calendar" a property of the credential
rather than a promise in a comment.

What that costs is stated rather than hidden: Cadence cannot add a study block to
your calendar for you, and it cannot mark anything done. Both would be a write.

Where the credentials live
--------------------------
Three secrets, in three places, and none of them reach the extension:

- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` come from `.env`, the same file
  as the Nebius key. The secret is optional for a Desktop client (Google treats
  those as *public* clients that cannot keep a secret), which is why the code
  sends it when it is there and does not require it.
- The **refresh token** is the long-lived one, and it lives in
  `data/google-token.json` - gitignored, written `0600`, never logged, never sent
  to the panel, and never put in a prompt. It is the file to delete if you want
  Cadence disconnected by hand.
- The **access token** is short-lived, cached in that same file so a restart does
  not force a round trip, and refreshed early rather than on failure.

The OAuth flow, and the two things about it that are not obvious
----------------------------------------------------------------
This is Google's **desktop / installed-app** flow against a loopback redirect,
because the backend is a process on `127.0.0.1` rather than a website:

1. **The redirect URI is `http://127.0.0.1:<port>` with no path.** That is the
   form Google documents for loopback, and the redirect URI must match exactly
   between the authorization request and the code exchange. So the callback is
   not a pretty `/oauth/google/callback` route: it is a request to `/`, carrying
   `code` and `state`, and the server treats it as the callback only when those
   are present. One rule, written down and tested, instead of a redirect URI we
   hoped Google would accept.
2. **PKCE, not a client secret.** Desktop clients are public clients, so the
   secret is not a secret; the protection Google actually documents for this
   client type is a per-attempt `code_verifier` whose SHA-256 travels with the
   authorization request and is presented at the exchange. The verifier is kept
   in memory next to the state, for ten minutes, and is spent once.

Two parameters that look mandatory and are not, for this client type:
`access_type=offline` and `prompt=consent`. Both belong to the *web server* flow;
Google's installed-app documentation says refresh tokens are always returned and
that incremental authorization is not supported there. Sending them would be
cargo cult, so they are not sent, and this paragraph is why.

Nothing here runs on a clock of its own
---------------------------------------
No polling thread, no background sync. Events are read when the brief is
requested and cached for a minute, so a quiet laptop makes no requests at all.
That is the opposite of how calendar integrations usually work, and it is
deliberate: a study assistant should cost nothing while you are not looking at it,
and a background poller is a second thing that can fail while you sleep.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- endpoints --

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"

# The one scope. Narrow on purpose, and the only one this project ever asks for.
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# What Google shows on the consent screen, quoted in the panel so a student is not
# asked to approve something they cannot name.
SCOPE_SUMMARY = "see the events on your calendars"

CALENDAR_ID = "primary"

CLIENT_ID_ENV = "GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET"
# The token file's location is overridable so tests can point it at a temp file
# instead of the real one. Same reason `NEMOTRON_CONFIG_PATH` exists.
TOKEN_PATH_ENV = "CADENCE_GOOGLE_TOKEN"
DEFAULT_TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "google-token.json"

# A connect attempt that sits unused for ten minutes is forgotten: an authorization
# code is short-lived anyway, and a state that never expires is a state that is
# still valid by the time someone finds it in a browser history.
STATE_TTL_SECONDS = 600
MAX_PENDING_FLOWS = 32

REQUEST_TIMEOUT = 8
# How long a successful read is reused. The panel asks for the brief on every open
# and after every turn; without this, one study session would be dozens of calls
# to a rate-limited API for a calendar that rarely changes.
CACHE_SECONDS = 60
# Refresh this long before expiry rather than waiting for a 401 to tell us.
EARLY_REFRESH_SECONDS = 60

# How many events we ask Google for in one page, and how many we keep. The page is
# larger than the list because a page of ten could be ten events we filter out;
# `orderBy=startTime` with `singleEvents=true` means one page *is* the soonest
# events, so a fuller page is a strictly better view of the near future.
MAX_FETCH = 50
MAX_EVENTS = 10
MAX_TITLE_CHARS = 120
MAX_LOCATION_CHARS = 80
MAX_CALENDAR_NAME_CHARS = 40
DEFAULT_HORIZON_DAYS = 7

# Google's own subscription calendars are generated, not owned by anyone: the
# holiday calendar and the weather one both end in this shape. They were visible
# in the first live run (`en.usa#holiday@group.v.calendar.google.com`), and at the
# ten-line cap a December brief would have shown Christmas Eve and Boxing Day
# instead of the classes happening around them. A holiday is context, not a
# commitment; the commitments are the calendars a person keeps.
SUBSCRIPTION_MARKERS = (
    "#holiday@group.v.calendar.google.com",
    "#weather@group.v.calendar.google.com",
)

# A brief that reads every calendar in an account has to stop somewhere. Eight is
# enough for a student (their own, a course calendar, a shared project, a work
# one) and small enough that a stray subscription cannot turn one page load into
# fifty API calls. The primary calendar is kept first, so the cap never drops it.
MAX_CALENDARS = 8
# Four at a time: enough that five calendars do not cost five round trips in a
# row, few enough that a slow one cannot hold the whole brief open.
CALENDAR_WORKERS = 4


class CalendarError(RuntimeError):
    """A calendar operation that did not work.

    `code` is the coarse bucket the action log records - "unreachable",
    "not_connected", "reconnect", "refused", "rate_limited", "bad_response".
    The message is for the student: it says what happened and what to do about it,
    because "calendar error" is not something anyone can act on.
    """

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ config --


def client_id() -> str:
    return (os.environ.get(CLIENT_ID_ENV) or "").strip()


def client_secret() -> str:
    """The client secret, when there is one. Optional for a desktop client."""
    return (os.environ.get(CLIENT_SECRET_ENV) or "").strip()


def configured() -> bool:
    """Is this server able to start a connect flow at all?

    The client ID is the whole answer: a desktop client's secret is optional
    (Google calls these *public* clients), so requiring one here would rule out a
    perfectly correct setup and produce a "broken" message for a working config.
    """
    return bool(client_id())


def token_path() -> Path:
    return Path(os.environ.get(TOKEN_PATH_ENV) or DEFAULT_TOKEN_PATH)


def redirect_uri(port: int) -> str:
    """The loopback redirect, in the form Google documents: no path, fixed port.

    The backend is one process on a known port, so a fixed port is right here -
    it is the same port the panel already talks to, and the port the student
    pastes into their OAuth client if the browser asks. `127.0.0.1` rather than
    `localhost` because Google documents the IP form for loopback.
    """
    return "http://127.0.0.1:{0}".format(int(port))


# ------------------------------------------------------------------- http ---
#
# One call path for the three things this module does over the network, with the
# same seam `backend/tools.py` uses: `transport` replaces the network entirely, so
# the tests exercise the real URL-building and parsing instead of a reimplementation
# of it. Inside the same guard as urllib, on purpose - a stand-in that fails
# differently from the thing it replaces hides bugs.

Transport = Callable[[str, str, dict, Optional[str], float], Tuple[int, str]]


def _call(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    body: Optional[str] = None,
    timeout: float = REQUEST_TIMEOUT,
    transport: Optional[Transport] = None,
) -> Tuple[int, str]:
    """One HTTP call. -> (status, text). Network problems raise CalendarError."""
    headers = headers or {}
    try:
        if transport is not None:
            return transport(method, url, headers, body, timeout)
        data = body.encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # A status *is* Google answering us, so it is data rather than a crash.
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except urllib.error.URLError as exc:
        raise CalendarError(
            "could not reach Google Calendar: {0}".format(exc.reason), "unreachable"
        ) from None
    except OSError as exc:
        # A reset or a timeout mid-read is not a URLError.
        raise CalendarError(
            "could not reach Google Calendar: {0}".format(exc), "unreachable"
        ) from None


def _json_or_error(status: int, text: str, *, what: str) -> dict:
    """Parse a 2xx body, or turn anything else into a sentence worth reading."""
    if status >= 400:
        raise CalendarError(*_error_for(status, text, what=what))
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        raise CalendarError(
            "Google sent something I could not read while {0}.".format(what),
            "bad_response",
        ) from None
    if not isinstance(payload, dict):
        raise CalendarError(
            "Google sent something I could not read while {0}.".format(what),
            "bad_response",
        )
    return payload


def _error_for(status: int, text: str, *, what: str) -> Tuple[str, str]:
    """(a sentence, a log code) for a failure, from Google's own error body."""
    code = ""
    try:
        code = str((json.loads(text or "{}") or {}).get("error") or "")
    except ValueError:
        code = ""

    if code == "invalid_grant":
        # The one error a student can do something about, and it is the most
        # common one for a personal integration: testing-mode consent screens
        # expire their refresh tokens after a week.
        return (
            "Google no longer accepts my permission to read your calendar "
            "(invalid_grant). Connect it again and it will work.",
            "reconnect",
        )
    if code == "invalid_client":
        return (
            "Google rejected the client ID in .env. Check GOOGLE_CLIENT_ID against "
            "your OAuth client.",
            "refused",
        )
    if status in (401, 403):
        return (
            "Google refused that request (HTTP {0}) while {1}. If it keeps "
            "happening, connect the calendar again.".format(status, what),
            "refused",
        )
    if status == 429:
        return (
            "Google is rate limiting this project (HTTP 429). Try again in a "
            "minute; nothing is lost.",
            "rate_limited",
        )
    detail = (text or "").strip().replace("\n", " ")[:160]
    return (
        "Google returned HTTP {0} while {1}: {2}".format(status, what, detail),
        "bad_response",
    )


# -------------------------------------------------------- connect attempts ---


class PendingFlows:
    """The connect attempts this process has started.

    One dict entry per attempt, holding the PKCE verifier the state belongs to.
    Three rules, each of which is a way this goes wrong:

    - **Single use.** `consume` pops; a state that arrives twice is a replay, and
      the second one gets nothing.
    - **It expires.** Ten minutes, because an unused state is not a thing to keep
      forever.
    - **It is bounded.** A student clicking Connect in a loop cannot grow this
      dictionary without limit; the oldest attempt is dropped first.
    """

    def __init__(self, ttl: int = STATE_TTL_SECONDS, limit: int = MAX_PENDING_FLOWS):
        self.ttl = ttl
        self.limit = limit
        self._lock = threading.Lock()
        self._flows: Dict[str, Tuple[str, float]] = {}

    def begin(self, now: Optional[float] = None) -> Tuple[str, str]:
        """Start an attempt. -> (state, code_verifier)."""
        state = secrets.token_urlsafe(24)
        verifier = _pkce_verifier()
        moment = time.time() if now is None else now
        with self._lock:
            if len(self._flows) >= self.limit:
                oldest = min(self._flows.items(), key=lambda item: item[1][1])[0]
                self._flows.pop(oldest, None)
            self._flows[state] = (verifier, moment + self.ttl)
        return state, verifier

    def consume(self, state: str, now: Optional[float] = None) -> Optional[str]:
        """Spend a state. -> its verifier, or None when it is not ours."""
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._flows.pop((state or "").strip(), None)
        if entry is None:
            return None
        verifier, expires_at = entry
        return verifier if moment <= expires_at else None

    def clear(self) -> None:
        with self._lock:
            self._flows.clear()


def _pkce_verifier() -> str:
    """A fresh PKCE code verifier: 96 bytes of urlsafe entropy, unpadded."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(*, state: str, verifier: str, port: int) -> str:
    """Where to send the browser to ask for permission.

    Deliberately absent: `access_type`, `prompt`, `include_granted_scopes`. All
    three belong to the web-server flow; for an installed app Google documents
    that a refresh token is always returned, and that incremental authorization is
    not supported. See the module docstring.
    """
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id(),
            "redirect_uri": redirect_uri(port),
            "scope": SCOPE,
            "state": state,
            "code_challenge": _challenge_for(verifier),
            "code_challenge_method": "S256",
        }
    )
    return AUTHORIZE_URL + "?" + query


def exchange_code(
    code: str,
    *,
    verifier: str,
    port: int,
    transport: Optional[Transport] = None,
) -> dict:
    """Trade the one-time code for tokens. -> the token payload, normalised."""
    form = {
        "client_id": client_id(),
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(port),
    }
    secret = client_secret()
    if secret:
        # Sent when it exists, never required: a Desktop client is a public client.
        form["client_secret"] = secret

    status, text = _call(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(form),
        transport=transport,
    )
    payload = _json_or_error(status, text, what="finishing the sign-in")
    return _normalise_tokens(payload, what="the sign-in")


def refresh_access(refresh_token: str, *, transport: Optional[Transport] = None) -> dict:
    """Get a new access token from the stored refresh token.

    Google's refresh response usually carries no new `refresh_token`, and the rule
    is to keep the one you have rather than to expect a replacement - so the caller
    merges this over what it already stored.
    """
    form = {
        "client_id": client_id(),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    secret = client_secret()
    if secret:
        form["client_secret"] = secret

    status, text = _call(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(form),
        transport=transport,
    )
    payload = _json_or_error(status, text, what="renewing permission to read your calendar")
    return _normalise_tokens(payload, what="the refresh")


def _normalise_tokens(payload: dict, *, what: str) -> dict:
    """Google's token body -> ours, with an absolute expiry instead of `expires_in`.

    An absolute time is what the rest of the code needs, and converting it once
    here means no other function has to know when the tokens arrived.
    """
    access = str(payload.get("access_token") or "")
    if not access:
        raise CalendarError(
            "Google accepted the request but sent no access token while {0}.".format(what),
            "bad_response",
        )
    expires_in = payload.get("expires_in")
    try:
        lifetime = int(expires_in)
    except (TypeError, ValueError):
        # Google documents `expires_in`; a body without it is a body we do not
        # guess at. A conservative hour is the documented example's scale.
        lifetime = 3600
    now = time.time()
    normalised = {
        "access_token": access,
        "expires_at": now + max(0, lifetime),
        "obtained_at": now,
    }
    if payload.get("refresh_token"):
        normalised["refresh_token"] = str(payload["refresh_token"])
    if payload.get("scope"):
        normalised["scope"] = str(payload["scope"])
    return normalised


# ------------------------------------------------------------- token store ---


class TokenStore:
    """The refresh token on disk, and the access token beside it.

    The file is the *only* copy of the long-lived credential this project holds, so
    it is written 0600, inside a directory that `.gitignore` already excludes, and
    it is never read into a log line, a prompt or a panel payload.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or token_path()

    def load(self) -> dict:
        """The stored tokens, or {} when there are none to trust.

        A file that will not parse is treated as *not connected* rather than as an
        error: the remedy is the same as a missing file (connect again), and a
        corrupt cache should not be able to break the brief forever.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @property
    def connected(self) -> bool:
        return bool(self.load().get("refresh_token"))

    def save(self, tokens: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            merged = dict(self.load())
            merged.update(tokens)
            text = json.dumps(merged, indent=2, sort_keys=True)
            # Create with the mode already set, then write, then set it again:
            # `open(..., "w")` on an existing file keeps its old mode, and a token
            # file that was briefly world-readable is a token file that leaked.
            with open(self.path, "w", encoding="utf-8") as handle:
                os.chmod(self.path, 0o600)
                handle.write(text)
            os.chmod(self.path, 0o600)

    def clear(self) -> bool:
        """Forget everything. -> whether there was anything to forget."""
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise CalendarError(
                    "could not delete the stored Google token: {0}".format(exc),
                    "failed",
                ) from None
            return True

    def access_token(
        self, *, transport: Optional[Transport] = None, now: Optional[float] = None
    ) -> str:
        """A usable access token, refreshing if it is close to expiring.

        Refreshing *early* rather than after a 401 keeps the failure modes apart:
        an expired token would otherwise surface as "Google refused that request"
        somewhere in the middle of a brief, which points at the wrong thing.
        """
        tokens = self.load()
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            raise CalendarError(
                "Google Calendar is not connected yet.", "not_connected"
            )

        moment = time.time() if now is None else now
        access = str(tokens.get("access_token") or "")
        expires_at = float(tokens.get("expires_at") or 0)
        if access and expires_at - EARLY_REFRESH_SECONDS > moment:
            return access

        fresh = refresh_access(refresh_token, transport=transport)
        # The refresh token is kept, not replaced: Google's refresh response does
        # not normally carry a new one, and writing None over a working credential
        # is how a connection gets silently lost.
        self.save({key: value for key, value in fresh.items() if key != "refresh_token"})
        return fresh["access_token"]


# --------------------------------------------------------------- the events ---


def _rfc3339(moment: datetime) -> str:
    """An RFC3339 timestamp with an offset, which is what Google requires.

    `timeMin` and `timeMax` are documented as requiring an offset *or* a `Z`, so
    this always emits UTC with the `Z`. Python 3.9 cannot parse a `Z` back, which
    is why the reader below rewrites it before `fromisoformat`.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_moment(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        # 3.9's fromisoformat rejects "Z"; 3.11 accepts it. Normalise rather than
        # depend on the Python version CI happens to run.
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A `dateTime` without an offset means the calendar's own time zone. Left
        # naive it would be read as local and then shifted twice, so it is taken
        # as local - our best guess, and one that is never off by a day.
        parsed = parsed.astimezone()
    return parsed


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def is_subscription_calendar(calendar_id: str) -> bool:
    """Is this one of Google's generated calendars rather than a person's?

    Checked by id suffix rather than by name, because the name is localised
    ("Holidays in United States", "Feiertage in Deutschland") and the id is not.
    """
    lowered = str(calendar_id or "").lower()
    return any(marker in lowered for marker in SUBSCRIPTION_MARKERS)


def list_calendars(
    access_token: str,
    *,
    transport: Optional[Transport] = None,
) -> List[dict]:
    """Every calendar this account can read, primary first. -> [{"id", "name"}].

    The brief used to read `calendars/primary` and nothing else, which is a
    defensible first version and a wrong answer for anyone whose week lives on a
    second calendar: a course calendar, a shared project, the calendar a school
    account shares with them. Google has no "events from all calendars" endpoint,
    so the list has to be walked one calendar at a time - which is why this costs
    one extra request and is capped.

    Hidden and deleted calendars are skipped: Google keeps both in this list, and
    `hidden` is the student having unchecked it, which is a preference rather than
    an accident. Google's own generated subscription calendars are skipped too -
    see `SUBSCRIPTION_MARKERS`. A failure here returns just the primary calendar,
    because reading one calendar beats reading none - and if the failure is a dead
    token, the events call right after it reports that properly.
    """
    query = urllib.parse.urlencode(
        {
            # Google's own ceiling; the cap that matters is applied below.
            "maxResults": "250",
            "minAccessRole": "reader",
            "fields": "items(id,summary,primary,hidden,deleted,accessRole)",
        }
    )
    url = "{0}/users/me/calendarList?{1}".format(API_BASE, query)
    try:
        status, text = _call(
            "GET",
            url,
            headers={"Authorization": "Bearer {0}".format(access_token)},
            transport=transport,
        )
        payload = _json_or_error(status, text, what="listing your calendars")
    except CalendarError:
        return [{"id": CALENDAR_ID, "name": "Primary"}]

    items = payload.get("items")
    if not isinstance(items, list):
        items = []

    calendars: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("deleted") or item.get("hidden"):
            continue
        calendar_id = str(item.get("id") or "").strip()
        if not calendar_id or is_subscription_calendar(calendar_id):
            continue
        calendars.append(
            {
                "id": calendar_id,
                "name": _clip(str(item.get("summary") or ""), MAX_CALENDAR_NAME_CHARS)
                or calendar_id,
                "primary": bool(item.get("primary")),
            }
        )

    if not calendars:
        return [{"id": CALENDAR_ID, "name": "Primary"}]

    # Primary first, then the rest by name: the order is what decides which
    # calendar wins a duplicate and which one falls off the cap, so it cannot be
    # whatever order Google happened to return.
    calendars.sort(key=lambda cal: (not cal.get("primary"), cal["name"].lower()))
    capped = calendars[:MAX_CALENDARS]
    return [{"id": cal["id"], "name": cal["name"]} for cal in capped]


def fetch_events(
    access_token: str,
    *,
    calendar_id: str = CALENDAR_ID,
    now: Optional[datetime] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    max_results: int = MAX_FETCH,
    transport: Optional[Transport] = None,
) -> Tuple[List[dict], bool]:
    """One page of one calendar's events. -> (items, more_beyond_this_page).

    The window is **now → now + horizon**, and the two bounds are not symmetric:
    Google documents `timeMin` as an exclusive lower bound on an event's *end*
    time, so an event happening right now is still in the window - which is what a
    student means by "what is coming up". An event that finished an hour ago is
    not, and one starting after the horizon is deliberately left out rather than
    fetched and thrown away.

    `more` is honest about paging: with `singleEvents=true` and `orderBy=startTime`
    the first page is the soonest events, so what we drop is the far end of the
    list - but if Google says there is another page, the brief should not imply it
    has seen everything.
    """
    moment = now or datetime.now(timezone.utc)
    query = urllib.parse.urlencode(
        {
            "timeMin": _rfc3339(moment),
            "timeMax": _rfc3339(moment + timedelta(days=max(0, int(horizon_days)))),
            # Recurring events are expanded into instances, which is the only form
            # in which "the next lecture" is a thing that exists.
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max(1, int(max_results))),
        }
    )
    url = "{0}/calendars/{1}/events?{2}".format(
        API_BASE, urllib.parse.quote(str(calendar_id or CALENDAR_ID), safe=""), query
    )
    status, text = _call(
        "GET",
        url,
        headers={"Authorization": "Bearer {0}".format(access_token)},
        transport=transport,
    )
    payload = _json_or_error(status, text, what="reading your calendar")
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    return items, bool(payload.get("nextPageToken"))


def fetch_from_calendars(
    access_token: str,
    calendars: Sequence[dict],
    *,
    today: date,
    now: Optional[datetime] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    transport: Optional[Transport] = None,
) -> Tuple[List[dict], bool, List[dict]]:
    """Merge every calendar into one ordered list. -> (events, more, failures).

    One request per calendar, four at a time. Two decisions worth naming:

    * **A failing calendar does not empty the brief.** A shared calendar that was
      unshared, or one Google answers 403 for, is reported as a failure and
      skipped - the student still sees the rest of their week. Only `reconnect`
      (a dead token) aborts, because then every calendar would fail the same way.
    * **Duplicates are dropped by event id.** An event on a shared calendar can be
      visible from both sides, and "Chemistry lab" twice in one brief reads like a
      bug. The first calendar in the list wins, which is why the list is ordered
      primary-first rather than in Google's return order.

    Each event carries its calendar's display name, so a line can say where it
    came from when more than one calendar contributed.
    """
    moment = now

    def one(calendar: dict):
        try:
            items, more = fetch_events(
                access_token,
                calendar_id=calendar["id"],
                now=moment,
                horizon_days=horizon_days,
                transport=transport,
            )
            return calendar, items, more, None
        except CalendarError as exc:
            if exc.code == "reconnect":
                raise
            return calendar, [], False, {
                "calendar": calendar["name"],
                "code": exc.code,
                "message": str(exc),
            }

    ordered = list(calendars) or [{"id": CALENDAR_ID, "name": "Primary"}]
    results = []
    with ThreadPoolExecutor(max_workers=min(CALENDAR_WORKERS, len(ordered))) as pool:
        # `map` keeps the input order, which is what the duplicate rule depends on.
        for result in pool.map(one, ordered):
            results.append(result)

    events: List[dict] = []
    failures: List[dict] = []
    more = False
    seen = set()
    for calendar, items, page_more, failure in results:
        if failure:
            failures.append(failure)
            continue
        more = more or page_more
        for event in parse_events(items, today=today, now=moment):
            if event["id"]:
                if event["id"] in seen:
                    continue
                seen.add(event["id"])
            event["calendar"] = calendar["name"]
            events.append(event)

    events.sort(key=lambda event: (event["_start"], event["title"]))
    return events, more, failures


def parse_events(
    items: Sequence[dict],
    *,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
) -> List[dict]:
    """Google's event objects -> ours, dropping the ones we will not show.

    Dropped: cancelled events (Google keeps them in the list and expects clients to
    filter), and anything without a date we can read. Everything else is kept,
    including a missing title - an event with no summary is usually a busy block,
    and hiding it would quietly make the day look emptier than it is.
    """
    moment = now or datetime.now().astimezone()
    day = today or moment.date()
    parsed: List[dict] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() == "cancelled":
            continue

        start = item.get("start") or {}
        end = item.get("end") or {}
        if not isinstance(start, dict):
            continue

        all_day = bool(start.get("date")) and not start.get("dateTime")
        if all_day:
            try:
                start_day = date.fromisoformat(str(start["date"]))
            except (TypeError, ValueError):
                continue
            start_at = datetime.combine(start_day, datetime.min.time()).astimezone()
            end_at = None
            try:
                # Google's all-day `end.date` is *exclusive*, so the last day the
                # event covers is the day before it. Keeping the exclusive value
                # and never showing it is simpler than subtracting a day.
                end_at = datetime.combine(
                    date.fromisoformat(str(end["date"])), datetime.min.time()
                ).astimezone()
            except (TypeError, ValueError, KeyError):
                end_at = None
        else:
            start_at = _parse_moment(str(start.get("dateTime") or ""))
            if start_at is None:
                continue
            end_at = _parse_moment(str(end.get("dateTime") or ""))
            start_at = start_at.astimezone()
            end_at = end_at.astimezone() if end_at else None

        local_start = start_at.astimezone()
        local_end = end_at.astimezone() if end_at else None
        ongoing = bool(local_end and local_start <= moment < local_end)
        parsed.append(
            {
                "id": str(item.get("id") or ""),
                "title": _clip(str(item.get("summary") or ""), MAX_TITLE_CHARS)
                or "(no title)",
                "location": _clip(str(item.get("location") or ""), MAX_LOCATION_CHARS),
                "day": local_start.date().isoformat(),
                "days_away": (local_start.date() - day).days,
                "time": None if all_day else local_start.strftime("%H:%M"),
                "end_time": None if all_day else (local_end.strftime("%H:%M") if local_end else None),
                "all_day": all_day,
                "ongoing": ongoing,
                "_start": local_start,
            }
        )

    parsed.sort(key=lambda event: (event["_start"], event["title"]))
    return parsed


# ----------------------------------------------------------- the brief part ---


def _phrase(event: dict, show_source: bool = False) -> str:
    """One line about one event, in the brief's voice.

    Composed here rather than by a model, for the same reason the deadline lines
    are: this text has to be true, and "in 3 days" is arithmetic.

    `show_source` appends the calendar a line came from, and is only set when more
    than one calendar contributed - with a single calendar the suffix would be
    noise repeated down the list.
    """
    away = int(event.get("days_away") or 0)
    source = " · {0}".format(event["calendar"]) if show_source and event.get("calendar") else ""
    if event.get("ongoing"):
        detail = " (until {0})".format(event["end_time"]) if event.get("end_time") else ""
        return "now — {0}{1}{2}".format(event["title"], detail, source)
    if away <= 0:
        lead = "today"
    elif away == 1:
        lead = "tomorrow"
    else:
        lead = "in {0} days".format(away)

    bits = []
    if event.get("all_day"):
        bits.append("all day")
    elif event.get("time"):
        bits.append(event["time"])
    if event.get("location"):
        bits.append(event["location"])
    detail = " ({0})".format(", ".join(bits)) if bits else ""
    return "{0} — {1}{2}{3}".format(lead, event["title"], detail, source)


def compose(
    events: Sequence[dict],
    *,
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    more: bool = False,
    calendars: Optional[Sequence[dict]] = None,
) -> dict:
    """The calendar half of the brief, as JSON-ready data.

    A pure function of its arguments - no clock, no network, no files - so "what
    does next Tuesday look like" is testable without waiting for Tuesday. The
    server owns the I/O and passes `today` in, exactly like the scheduler.
    """
    soon = [event for event in events if abs(int(event.get("days_away") or 0)) <= horizon_days]
    shown = soon[:MAX_EVENTS]
    sources = [
        {"id": str(cal.get("id") or ""), "name": str(cal.get("name") or "")}
        for cal in (calendars or [])
        if isinstance(cal, dict)
    ]
    show_source = len(sources) > 1
    return {
        "connected": True,
        "read_only": True,
        "scope": SCOPE,
        "scope_summary": SCOPE_SUMMARY,
        # Kept for the panel and the tests that predate reading more than one;
        # `calendars` is the answer to "where did these lines come from".
        "calendar": CALENDAR_ID,
        "calendars": sources,
        "horizon_days": int(horizon_days),
        "events": [
            {key: value for key, value in event.items() if not key.startswith("_")}
            for event in shown
        ],
        "lines": [_phrase(event, show_source=show_source) for event in shown],
        "counts": {
            "events": len(shown),
            "today": len([event for event in shown if int(event.get("days_away") or 0) <= 0]),
            "all_day": len([event for event in shown if event.get("all_day")]),
            # Not "there are more events in your life" - rather "Google had
            # another page, so treat this list as the start of one".
            "more": bool(more) or len(soon) > len(shown),
        },
        "cost_usd": 0.0,
        "source": "google-calendar",
        "generated_at": time.strftime("%H:%M:%S"),
    }


def not_connected_payload(*, configured_now: Optional[bool] = None, explanation: str = "") -> dict:
    """What the brief shows when there is no calendar to read.

    `configured` is the difference between two very different screens: a student
    who has not connected yet (show the button) and a server that has no OAuth
    client at all (show instructions, because the button cannot work). They are
    separate booleans so the panel does not have to guess.
    """
    return {
        "connected": False,
        "configured": configured() if configured_now is None else bool(configured_now),
        "read_only": True,
        "scope": SCOPE,
        "scope_summary": SCOPE_SUMMARY,
        "calendar": CALENDAR_ID,
        "events": [],
        "lines": [],
        "counts": {"events": 0, "today": 0, "all_day": 0, "more": False},
        "cost_usd": 0.0,
        "source": "google-calendar",
        "explanation": explanation,
    }


_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, dict]] = {}


def cached(key: str, ttl: int = CACHE_SECONDS, now: Optional[float] = None):
    """Read the short-lived cache. -> the payload, or None when it is stale."""
    moment = time.time() if now is None else now
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if not entry:
        return None
    at, payload = entry
    return payload if moment - at < ttl else None


def remember(key: str, payload: dict, now: Optional[float] = None) -> dict:
    moment = time.time() if now is None else now
    with _CACHE_LOCK:
        _CACHE[key] = (moment, payload)
    return payload


def forget(key: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if key is None:
            _CACHE.clear()
        else:
            _CACHE.pop(key, None)


def section(
    store: "TokenStore",
    *,
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    transport: Optional[Transport] = None,
    now: Optional[datetime] = None,
    use_cache: bool = True,
) -> dict:
    """The calendar section of the brief: read live, with three quiet paths.

    1. **No OAuth client configured** -> no network call at all, and a payload that
       says so, because a request we know cannot succeed should not be made.
    2. **Not connected** -> no network call either. This is the common case for
       anyone who has not asked for a calendar, and it must cost nothing.
    3. **Connected** -> a list request and one events request per calendar, four
       at a time, cached for a minute. A calendar that fails is skipped and
       reported; only an all-calendars failure or a dead token becomes the
       payload's `error`.

    A dead refresh token (`invalid_grant`) clears the stored tokens and answers
    `reconnect`: keeping a credential Google has already rejected would turn a
    one-click fix into a permanently broken section.
    """
    if not configured():
        return not_connected_payload(configured_now=False)

    tokens = store.load()
    if not tokens.get("refresh_token"):
        return not_connected_payload(configured_now=True)

    key = "calendar:all:{0}".format(int(horizon_days))
    if use_cache:
        hit = cached(key)
        if hit is not None:
            return hit

    try:
        access = store.access_token(transport=transport)
        # One list request, then one per calendar. The list is allowed to fail
        # quietly back to the primary calendar; the events are not, because that
        # is where a dead token surfaces.
        calendars = list_calendars(access, transport=transport)
        events, more, failures = fetch_from_calendars(
            access,
            calendars,
            today=today,
            now=now,
            horizon_days=horizon_days,
            transport=transport,
        )
        # Every calendar failing is not a partial week, it is no calendar at all -
        # and an empty list would read as "your week is free", which is the one
        # wrong answer worth going out of the way to avoid.
        if failures and len(failures) >= len(calendars):
            raise CalendarError(failures[0]["message"], failures[0]["code"])
    except CalendarError as exc:
        if exc.code == "reconnect":
            store.clear()
            forget(key)
            return not_connected_payload(
                configured_now=True,
                explanation=str(exc),
            )
        return {
            "connected": True,
            "read_only": True,
            "scope": SCOPE,
            "scope_summary": SCOPE_SUMMARY,
            "calendar": CALENDAR_ID,
            "horizon_days": int(horizon_days),
            "events": [],
            "lines": [],
            "counts": {"events": 0, "today": 0, "all_day": 0, "more": False},
            "cost_usd": 0.0,
            "source": "google-calendar",
            "error": {"code": exc.code, "message": str(exc)},
            "generated_at": time.strftime("%H:%M:%S"),
        }

    payload = compose(
        events,
        today=today,
        horizon_days=horizon_days,
        more=more,
        calendars=calendars,
    )
    if failures:
        # Said out loud rather than swallowed: a week that is missing a shared
        # calendar's events looks the same as a free week, and one of those is a
        # reason to check something.
        payload["calendar_errors"] = failures
    return remember(key, payload) if use_cache else payload
