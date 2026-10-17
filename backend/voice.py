"""The backend's voice: ElevenLabs, called from here so the key stays here.

Step 13. The panel already speaks through Chrome's own `speechSynthesis`, which
is free, local, and sounds like a 2009 GPS. Swapping in a hosted voice is a small
change in one place - `speak()` in the extension - and four decisions that are not
small at all, each against an alternative that fails quietly:

1. **The key never reaches the browser.** The extension is a bundle anyone can
   read; an `ELEVENLABS_API_KEY` in it is a key published. So synthesis happens
   here, and the panel asks this backend for audio bytes the same way it asks for
   a reply. The token the panel already carries is enough - there is no second
   credential for the student to paste into anything.

2. **Speech costs money per character, so it is counted.** A reply is one or two
   sentences, which is a fraction of a cent, but "a fraction of a cent" repeated
   is a bill. Every synthesis returns its own measured cost and the server books
   it in the same table model calls use, so the panel's cost line means "what
   this process spent", not "what the models spent". Speech is the only part of
   this project that a hostile caller could bill us for directly, which is why
   the text is capped (`MAX_SYNTH_CHARS`) and the bytes are cached.

3. **The cap is on characters, not seconds.** Characters are what ElevenLabs
   bills and what we can measure before spending. A long reply is cut at a
   sentence boundary rather than mid-word, and reports that it was cut (`meta`
   carries `truncated`), so the panel can say so instead of silently speaking
   less than it shows.

4. **A cache, because "Speak again" is a button.** The same words at the same
   voice and model produce the same audio; paying a second time to hear it again
   would be paying for a bug. The cache is bounded by bytes, because audio is
   large and this process is supposed to be able to run for weeks.

Privacy, stated rather than buried: Chrome's voice is local and this one is not.
Synthesising a reply sends its words to ElevenLabs. A reply can echo what the
student told us ("your organic chemistry midterm is on November 12"), so enabling
this is a real change in who sees what. It is off until `ELEVENLABS_API_KEY` is
set, the panel falls back to Chrome's voice when it is off or failing, and the
README says all of this next to the variable rather than here alone.

What this module deliberately does not do: pick a voice per student, stream audio
as it arrives, or save audio to disk. The first is a settings problem nobody has
asked for, the second is the next optimisation once the simple path is proven (see
`README.md`), and the third would turn a cache into a recording.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import quote

# ------------------------------------------------------------------ config ---

API_KEY_ENV = "ELEVENLABS_API_KEY"
VOICE_ID_ENV = "ELEVENLABS_VOICE_ID"
MODEL_ID_ENV = "ELEVENLABS_MODEL_ID"

# Tests point this at a stub, the same way `CADENCE_GOOGLE_TOKEN` points the
# calendar's token store at a temp file. It is not a student-facing setting.
BASE_URL_ENV = "CADENCE_ELEVENLABS_BASE"
DEFAULT_BASE_URL = "https://api.elevenlabs.io"

# Adam, a *premade* voice. A named default rather than an empty string: an unset
# voice id must still produce a voice, and "no voice configured" is a worse first
# run than "a voice you may want to change".
#
# It was Rachel, the id in ElevenLabs' own docs, until a real free-plan key was
# tried against it: Rachel is a *library* voice now, and a free account gets
# HTTP 402 `paid_plan_required` for every one of those. A default that fails on
# the plan most people try first is not a default. Premade voices such as Adam,
# Sarah, Daniel and George answered 200 on the same key, so the default is one of
# those. Verified live on 2026-10-17; the ids are not secrets.
DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"

# Flash is the low-latency model the docs recommend for exactly this case: a short
# reply, wanted out loud now. Turbo is deprecated in favour of it. Overridable
# because voice quality is a matter of taste and the id is not a secret.
DEFAULT_MODEL_ID = "eleven_flash_v2_5"

# The documented default, and small enough that a study reply arrives fast.
OUTPUT_FORMAT = "mp3_44100_128"

# Long enough for a paragraph, short enough that a runaway reply cannot become a
# bill. Forty seconds of speech is already more than anyone listens to.
MAX_SYNTH_CHARS = 1500

# The published API price for the Flash/Turbo family, per thousand characters at
# the time of writing. An estimate used for the cost line, never a bill: the
# provider's own `character-cost` header wins when it sends one.
USD_PER_1K_CHARS = 0.04

REQUEST_TIMEOUT = 20.0

# Bounded by bytes, not entries: sixteen cached replies at 128 kbps are megabytes,
# and this process is meant to run for weeks.
MAX_CACHE_ENTRIES = 24
MAX_CACHE_BYTES = 4 * 1024 * 1024

# One call's HTTP seam, identical in shape to `backend/tools.py` and `gcal.py` so
# the tests exercise the real URL building and parsing. It returns bytes rather
# than text because a successful call *is* audio.
Transport = Callable[[str, str, dict, Optional[bytes], float], Tuple[int, bytes, dict]]


class VoiceError(RuntimeError):
    """Something to tell the student, with a code the server maps to a status.

    The codes are deliberately the small set of things that mean different
    things to a person: a missing key, a rejected key, an empty account, a rate
    limit, and "ElevenLabs is not answering". A single "speech failed" would
    make all five look like the same bug.
    """

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


def api_key() -> str:
    return (os.environ.get(API_KEY_ENV) or "").strip()


def configured() -> bool:
    return bool(api_key())


def voice_id() -> str:
    return (os.environ.get(VOICE_ID_ENV) or "").strip() or DEFAULT_VOICE_ID


def model_id() -> str:
    return (os.environ.get(MODEL_ID_ENV) or "").strip() or DEFAULT_MODEL_ID


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL


# ---------------------------------------------------------------- characters --


def clean(text: str) -> str:
    """The text as one line, the way a voice should read it.

    Whitespace collapsed rather than stripped only at the ends: a reply that
    arrives with a newline in the middle gets a pause from some engines and a
    glitch from others, and neither is a sentence boundary we meant.
    """
    return " ".join(str(text or "").split())


def truncate(text: str) -> Tuple[str, bool]:
    """Cut to the cap at a sentence boundary. -> (text, was_truncated).

    Cutting mid-word is the one outcome to avoid: the voice cannot say the second
    half and the student hears a sentence that stops existing. If there is no
    sentence end in the second half of the budget we hard-cut, because a single
    1500-character sentence is already not being listened to.
    """
    if len(text) <= MAX_SYNTH_CHARS:
        return text, False
    window = text[:MAX_SYNTH_CHARS]
    cut = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    if cut >= MAX_SYNTH_CHARS // 2:
        return window[: cut + 1].strip(), True
    return window.rstrip(), True


def estimate_usd(chars: int) -> float:
    """What these characters should cost. An estimate, rounded like one."""
    try:
        count = max(0, int(chars))
    except (TypeError, ValueError):
        return 0.0
    return round(count / 1000.0 * USD_PER_1K_CHARS, 6)


# --------------------------------------------------------------------- cache --


class _Cache:
    """The last few replies we spoke, by (model, voice, text).

    Deliberately memory-only: a file would survive a restart (nice) and become a
    recording of what the student was told (not nice). It is bounded twice,
    entries and bytes, because the second bound is the one that matters when the
    audio is large and the first one alone could still fill memory.
    """

    def __init__(self, max_entries=MAX_CACHE_ENTRIES, max_bytes=MAX_CACHE_BYTES):
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        self._items: Dict[tuple, bytes] = {}
        self._order: list = []
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> Optional[Tuple[bytes, dict]]:
        """-> (audio, the meta it was synthesised with), or None.

        The meta is cached with the audio and not rebuilt on a hit, because a hit
        has to report the *same* character count the synthesis did. Rebuilding it
        from the text gave 41 where the provider had billed 46 - a small lie in a
        field whose whole purpose is being checkable.
        """
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            # Move to the end: least recently *used* goes first, because "Speak
            # again" is the case this exists for.
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            return entry

    def put(self, key: tuple, audio: bytes, meta: Optional[dict] = None) -> None:
        if not audio or len(audio) > self.max_bytes:
            return
        with self._lock:
            if key in self._items:
                self.bytes -= len(self._items[key][0])
                if key in self._order:
                    self._order.remove(key)
            self._items[key] = (audio, dict(meta or {}))
            self._order.append(key)
            self.bytes += len(audio)
            while len(self._order) > self.max_entries or self.bytes > self.max_bytes:
                oldest = self._order.pop(0)
                dropped = self._items.pop(oldest, None)
                if dropped:
                    self.bytes -= len(dropped[0])

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._order.clear()
            self.bytes = 0
            self.hits = 0
            self.misses = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._items),
                "bytes": self.bytes,
                "hits": self.hits,
                "misses": self.misses,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
            }


CACHE = _Cache()


# ---------------------------------------------------------------------- http --


def _call(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
    timeout: float = REQUEST_TIMEOUT,
    transport: Optional[Transport] = None,
) -> Tuple[int, bytes, dict]:
    """One HTTP call. -> (status, bytes, headers). Never raises for a status.

    A status *is* ElevenLabs answering, so it is data the caller maps to a
    sentence. Only a network failure raises, and it raises the same error type
    everything else in this module does.
    """
    headers = headers or {}
    try:
        if transport is not None:
            return transport(method, url, headers, body, timeout)
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}
    except urllib.error.URLError as exc:
        raise VoiceError(
            "could not reach ElevenLabs: {0}".format(exc.reason), "unreachable"
        ) from None
    except OSError as exc:
        # A reset or a timeout mid-read is not a URLError.
        raise VoiceError(
            "could not reach ElevenLabs: {0}".format(exc), "unreachable"
        ) from None


def _detail(payload: bytes) -> dict:
    """The `detail` object from an error body, or an empty dict."""
    try:
        parsed = json.loads(payload.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, dict):
            return detail
    return {}


def _error_for(status: int, payload: bytes) -> VoiceError:
    """Map a status to something a person can act on.

    The distinction that matters most is 402 against 429: the first means the
    account is out of credits and nothing will work until that changes, the
    second means slow down and it will. Calling both "rate limited" would send a
    student to wait for a storm that is never going to pass.
    """
    detail = _detail(payload)
    code = str(detail.get("code") or "")
    message = str(detail.get("message") or "").strip()

    # 402 means two different things and they need different sentences:
    # `paid_plan_required` is this *voice* being outside the plan (a free account
    # cannot use library voices), which a different voice id fixes; running out of
    # characters is only fixed by waiting or paying. Found by trying a real key.
    if status == 402 and code == "paid_plan_required":
        return VoiceError(
            "the ElevenLabs plan does not allow the voice {0} (library voices "
            "need a paid plan). Set ELEVENLABS_VOICE_ID in .env to a premade "
            "voice - {1} is the default - and this reply will be read by it "
            "instead of Chrome's own voice.".format(voice_id(), DEFAULT_VOICE_ID),
            "plan",
        )
    if status == 402 or code == "insufficient_credits":
        return VoiceError(
            "the ElevenLabs account is out of credits, so replies are being read "
            "in Chrome's own voice until that is topped up. The character count "
            "on your plan at elevenlabs.io shows where it went.",
            "quota",
        )
    if status in (401, 403):
        return VoiceError(
            "ElevenLabs rejected the API key (HTTP {0}). Check ELEVENLABS_API_KEY "
            "in .env, and that the key is allowed to use text to speech.".format(status),
            "auth",
        )
    if status == 429:
        return VoiceError(
            "ElevenLabs is rate limiting this account right now, so this reply is "
            "being read in Chrome's own voice. Trying again in a moment usually "
            "works.",
            "rate_limited",
        )
    if status >= 500:
        return VoiceError(
            "ElevenLabs had a problem on its side (HTTP {0}){1}. This reply is "
            "being read in Chrome's own voice.".format(
                status, ": " + message if message else ""
            ),
            "upstream",
        )
    return VoiceError(
        "ElevenLabs would not synthesise that (HTTP {0}){1}.".format(
            status, ": " + message if message else ""
        ),
        "upstream",
    )


# ------------------------------------------------------------------ synthesis -


def describe(demo: bool = False) -> dict:
    """The `/health` block. Says which voice will actually be used, and why not.

    `available` is the one the panel reads: it is what decides between asking this
    backend for audio and using Chrome's voice directly. It is false in demo mode
    even with a key set, because a stranger's reply would be billed to the
    operator - the same reasoning that keeps the demo's calendar off.
    """
    return {
        "provider": "elevenlabs",
        "configured": configured(),
        "available": configured() and not demo,
        "model": model_id(),
        "voice_id": voice_id(),
        "output_format": OUTPUT_FORMAT,
        "max_chars": MAX_SYNTH_CHARS,
        "cost_per_1k_chars_usd": USD_PER_1K_CHARS,
        "off": "demo" if demo else (None if configured() else "no-key"),
        # Said in the payload rather than only in the docs, because this is the
        # one part of Cadence whose work happens somewhere else.
        "note": (
            "reply text is sent to ElevenLabs for synthesis; with no key the "
            "panel uses Chrome's local voice instead"
        ),
        "cache": CACHE.snapshot(),
    }


def synthesize(
    text: str,
    *,
    transport: Optional[Transport] = None,
    demo: bool = False,
) -> Tuple[bytes, dict]:
    """Speak this text. -> (mp3 bytes, meta). Raises VoiceError with a code.

    `meta` carries what the caller needs to be honest: how many characters were
    spoken, what it cost, whether it was cut, how long it took, and whether it
    came from the cache (in which case it cost nothing again).
    """
    spoken = clean(text)
    if not spoken:
        raise VoiceError("there was nothing to say", "empty")
    if demo:
        raise VoiceError(
            "this server is the demo, so it does not spend the operator's "
            "ElevenLabs credits. Replies are read in Chrome's own voice here; "
            "run Cadence locally for the better one.",
            "demo",
        )
    if not configured():
        raise VoiceError(
            "no ElevenLabs key is configured (set ELEVENLABS_API_KEY in .env). "
            "Replies are being read in Chrome's own voice.",
            "no_key",
        )

    spoken, truncated = truncate(spoken)
    key = (model_id(), voice_id(), spoken)
    cached = CACHE.get(key)
    if cached is not None:
        audio, previous = cached
        # Everything as it was reported the first time - same characters, same
        # truncation - with only the two things a hit changes: no cost, no wait.
        return audio, {
            **previous,
            "cost_usd": 0.0,
            "ms": 0,
            "cached": True,
        }

    url = "{0}/v1/text-to-speech/{1}?output_format={2}".format(
        base_url().rstrip("/"), quote(voice_id(), safe=""), OUTPUT_FORMAT
    )
    body = json.dumps(
        {
            "text": spoken,
            "model_id": model_id(),
            # The documented defaults, written out because a voice configured by
            # silence is one nobody can reason about later. `speed` stays at 1:
            # the panel is a study companion, not an audiobook.
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": 1.0,
            },
        }
    ).encode("utf-8")

    started = time.monotonic()
    status, payload, headers = _call(
        "POST",
        url,
        headers={
            "xi-api-key": api_key(),
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        body=body,
        transport=transport,
    )
    ms = int(round((time.monotonic() - started) * 1000))

    if status >= 400:
        raise _error_for(status, payload)
    if not payload:
        raise VoiceError(
            "ElevenLabs answered with no audio for that reply.", "upstream"
        )

    # ElevenLabs reports what it actually charged in `character-cost`. Prefer it
    # over our own arithmetic: the cost line should be their number, not ours.
    billed = headers.get("character-cost") or headers.get("Character-Cost") or ""
    try:
        chars = int(str(billed).strip()) if str(billed).strip() else len(spoken)
    except ValueError:
        chars = len(spoken)

    meta = {
        "chars": chars,
        "truncated": truncated,
        "cost_usd": estimate_usd(chars),
        "ms": ms,
        "cached": False,
        "model": model_id(),
        "voice_id": voice_id(),
        "audio_bytes": len(payload),
    }
    CACHE.put(key, payload, meta)
    return payload, meta
