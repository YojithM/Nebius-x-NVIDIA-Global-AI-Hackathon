"""Pending approvals: what the assistant asked permission for, and what it may now do.

Step 10's permissioning half. The gate itself has existed since Step 3 - a skill
that declares `requires_confirmation` is never run on the model's say-so, and
that is enforced in `router._finish`. What was missing was the other half of a
question: the panel said *"awaiting your go-ahead"* and there was no way to give
one, so a Tier-2 action dead-ended.

Four decisions:

1. **The ask is stored, not re-derived.** A "yes" runs the *same* decision that
   was shown, not a fresh route of whatever the student says next. Re-routing on
   approval would mean "yes" is routed as an utterance, and a model gets to
   decide again what it already asked about - which is how a confirmation turns
   into a second, different action.
2. **Approvals are single-use.** `take()` pops in the same lock as the lookup,
   so a double-click cannot run the action twice, and an approval cannot be
   replayed later.
3. **They expire.** Five minutes, because what the student agreed to was a
   sentence they said five minutes ago. An approval that outlives the moment is
   a standing permission nobody granted.
4. **Nothing survives a restart, and that is correct.** A held approval is a
   conversation, not a setting. If the backend restarts mid-question, the honest
   answer is "ask me again" rather than acting on a stale yes.
"""

import threading
import time

# How long a question stays answerable.
DEFAULT_TTL_SECONDS = 300

# How many questions one session may have outstanding. Small on purpose: a queue
# of pending actions is a queue of things the student cannot see.
MAX_PENDING_PER_SESSION = 8


class Approvals:
    """Questions we are waiting on an answer to, keyed by session."""

    def __init__(self, ttl_seconds=DEFAULT_TTL_SECONDS, max_per_session=MAX_PENDING_PER_SESSION):
        self.ttl = float(ttl_seconds)
        self.max_per_session = int(max_per_session)
        self._lock = threading.Lock()
        self._pending = {}  # session -> {approval_id: (stored_at, payload)}
        self._counter = 0

    def _expired(self, stored_at) -> bool:
        return (time.time() - stored_at) > self.ttl

    def put(self, session_id, payload) -> str:
        """Store one pending action. -> its approval id.

        Oldest-first eviction, like the spend table: an unbounded store here
        would be a memory leak with a security shape.
        """
        with self._lock:
            self._counter += 1
            approval_id = "ap{:04d}".format(self._counter)
            bucket = self._pending.setdefault(str(session_id or "unknown"), {})
            for key, (stored_at, _) in list(bucket.items()):
                if self._expired(stored_at):
                    bucket.pop(key, None)
            while len(bucket) >= self.max_per_session:
                oldest = min(bucket.items(), key=lambda item: item[1][0])[0]
                bucket.pop(oldest, None)
            bucket[approval_id] = (time.time(), payload)
            return approval_id

    def take(self, session_id, approval_id):
        """Consume one approval. -> its payload, or None if it is gone or stale."""
        bucket = self._pending.get(str(session_id or "unknown")) or {}
        with self._lock:
            entry = bucket.pop(str(approval_id or ""), None)
        if entry is None:
            return None
        stored_at, payload = entry
        if self._expired(stored_at):
            return None
        return payload

    def drop(self, session_id) -> int:
        """Forget everything one session had pending. -> how many questions went."""
        with self._lock:
            return len(self._pending.pop(str(session_id or "unknown"), {}) or {})

    def count(self, session_id=None) -> int:
        with self._lock:
            if session_id is None:
                return sum(len(bucket) for bucket in self._pending.values())
            return len(self._pending.get(str(session_id or "unknown"), {}))

    def reset(self) -> None:
        with self._lock:
            self._pending = {}
            self._counter = 0
