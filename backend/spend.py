"""What this backend has spent, and what one visitor is allowed to spend.

Step 10. The cost display and the public demo's request cap live in one module
because they are the same question asked twice: how much has this process cost,
and how much may this visitor add to it.

Four decisions, each against an alternative that fails quietly:

1. **Money is counted in integer micro-dollars.** A float accumulator looks
   fine and drifts: adding $0.000986 a thousand times does not give the same
   number twice. One turn is a micro-dollar-scale event, so micro-dollars is
   the natural unit - an int sum cannot drift, and the dollar figure is derived
   once, at the edge in `snapshot()`.

2. **The cap counts requests, not tokens or dollars.** It is the number a
   visitor can be told ("4 of 20 left") and the number an operator can reason
   about. A token cap would be an implementation detail leaking into somebody
   else's afternoon.

3. **A session is a client address, not a cookie.** The thing being bounded is
   the operator's credit, and an identifier the client supplies is an identifier
   the client can reset: a reload or a cleared cookie would hand out a fresh
   budget. So the counter is keyed by the address the request came from, which
   also means it survives the panel being closed and reopened. A visitor who
   changes address does get a new budget - that is stated rather than hidden,
   and it is why this bounds casual use rather than an adversary. For an
   adversary there is `CADENCE_TOKEN`, and for a truly public demo the honest
   answer is a spending limit on the account itself.

4. **Charging is one step, never check-then-use.** `take()` increments only
   when it also says yes, under one lock. A cap with a race in it is a cap that
   reports 20 and spends 21.

Step 11 adds the fifth thing this module owns, and it is the oldest promise in the
file: `warn_over_usd_per_request` was declared in `config/models.json` in Step 0 and
read by nothing for eleven steps. A ceiling nobody checks is worse than no ceiling,
because it reads like a safeguard. `per_request_warning()` is that check - a pure
function of the config and one turn's measured cost, so this module decides whether
there is anything to say and the server decides how to say it.
"""

import threading
import time

MICRO = 1_000_000

DEFAULT_SESSION_CAP = 20
DEFAULT_TOTAL_CREDIT_USD = 25.0

# What one turn is allowed to cost before the student is told about it. The same
# number as `budgets.warn_over_usd_per_request` in the config; kept here so a config
# with the key deleted still warns rather than falling silent.
DEFAULT_WARN_OVER_USD_PER_REQUEST = 0.05

# Tracked sessions hold two ints and two floats each, so this is a memory bound
# rather than a policy: it exists so a public demo cannot grow a dictionary
# until the process dies. Oldest-touched goes first.
MAX_TRACKED_SESSIONS = 500


def _micro_from_usd(value) -> int:
    """Dollars to micro-dollars, refusing to guess at nonsense.

    A negative cost is a bug rather than a credit, so it clamps to zero: the
    one thing this must never do is quietly *reduce* what we think we spent.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    return int(round(amount * MICRO))


def _usd_from_micro(micro: int) -> float:
    return round(micro / MICRO, 6)


def warn_ceiling_usd(config) -> float:
    """The per-turn ceiling we flag. -> dollars, or 0.0 when it is switched off.

    Three cases, and the middle one is the reason this is a function:

    - a usable positive number is the ceiling;
    - an explicit 0 (or a negative) turns the warning off, deliberately;
    - anything else - the key missing, null, a string, a bool - falls back to the
      default. A misspelled config line must not be able to turn a safeguard off
      silently, which is exactly how this key spent eleven steps doing nothing.
    """
    raw = ((config or {}).get("budgets") or {}).get("warn_over_usd_per_request")
    if isinstance(raw, bool):  # bool is an int in Python; true is not a ceiling
        return DEFAULT_WARN_OVER_USD_PER_REQUEST
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WARN_OVER_USD_PER_REQUEST
    return value if value > 0 else 0.0


def per_request_warning(config, cost_usd):
    """Is this turn over the ceiling? -> the warning, or None.

    Strictly *over*: a turn that lands exactly on the ceiling is a turn that obeyed
    it. The cost is rounded through the same micro-dollar arithmetic the display
    uses, so the number in the sentence and the number in the cost line are the same
    number rather than two roundings of the same spend.
    """
    ceiling = warn_ceiling_usd(config)
    if ceiling <= 0:
        return None
    cost = _usd_from_micro(_micro_from_usd(cost_usd))
    if cost <= ceiling:
        return None
    return {"cost_usd": cost, "ceiling_usd": ceiling, "over_by_usd": round(cost - ceiling, 6)}


class Spend:
    """A running total for the process, and a per-visitor request budget."""

    def __init__(self, config=None, cap=None, credit_usd=None):
        budgets = (config or {}).get("budgets") or {}
        if cap is None:
            cap = budgets.get("max_requests_per_session_demo", DEFAULT_SESSION_CAP)
        if credit_usd is None:
            credit_usd = budgets.get("total_credit_usd", DEFAULT_TOTAL_CREDIT_USD)
        self.cap = max(0, int(cap))
        self.credit_micro = _micro_from_usd(
            0 if credit_usd is None else credit_usd
        ) or _micro_from_usd(DEFAULT_TOTAL_CREDIT_USD)
        self._lock = threading.Lock()
        self.reset()

    # ------------------------------------------------------------ recording --

    def record_call(self, tier, cost_usd) -> None:
        """One model call, on one tier. The router and the executor both land here.

        Deliberately does **not** add to the process total. A turn's total already
        contains its calls, so adding both would count a turn twice - which this
        module did in its first wiring, and the test for it now exists because of
        that. The total is the sum of completed turns; this table answers a
        different question (what did each tier cost us).
        """
        micro = _micro_from_usd(cost_usd)
        with self._lock:
            self.calls += 1
            self.called_micro += micro
            if tier:
                row = self.by_tier.setdefault(str(tier), {"calls": 0, "micro": 0})
                row["calls"] += 1
                row["micro"] += micro

    def record_turn(self, cost_usd, steps=1) -> None:
        """One completed turn, counted once. `cost_usd` is its measured total."""
        micro = _micro_from_usd(cost_usd)
        with self._lock:
            self.turns += 1
            self.micro += micro
            self.steps += max(0, int(steps or 0))
            self.last_turn = time.time()

    # ------------------------------------------------------------- sessions --

    def take(self, session_id, cap=None) -> dict:
        """Charge one request to a visitor. -> dict(allowed, used, remaining, cap).

        The increment happens inside the same lock as the check, so two turns
        arriving together cannot both see the last slot as free.
        """
        limit = self.cap if cap is None else max(0, int(cap))
        key = str(session_id or "unknown")
        with self._lock:
            row = self._sessions.get(key)
            if row is None:
                self._evict()
                row = {"used": 0, "first": time.time(), "last": time.time()}
                self._sessions[key] = row
            row["last"] = time.time()
            if row["used"] >= limit:
                return {
                    "allowed": False,
                    "used": row["used"],
                    "remaining": 0,
                    "cap": limit,
                }
            row["used"] += 1
            return {
                "allowed": True,
                "used": row["used"],
                "remaining": max(0, limit - row["used"]),
                "cap": limit,
            }

    def session(self, session_id, cap=None) -> dict:
        """What this visitor has used, without charging anything. Safe to call."""
        limit = self.cap if cap is None else max(0, int(cap))
        key = str(session_id or "unknown")
        with self._lock:
            row = self._sessions.get(key) or {"used": 0}
            used = int(row.get("used") or 0)
        return {
            "allowed": used < limit,
            "used": used,
            "remaining": max(0, limit - used),
            "cap": limit,
        }

    def _evict(self):
        """Drop the least recently seen sessions. Called holding the lock."""
        if len(self._sessions) < MAX_TRACKED_SESSIONS:
            return
        ordered = sorted(self._sessions.items(), key=lambda item: item[1]["last"])
        for key, _ in ordered[: max(1, len(ordered) // 10)]:
            self._sessions.pop(key, None)

    # ------------------------------------------------------------ reporting --

    def snapshot(self) -> dict:
        """The whole picture: process totals, credit left, and the demo cap."""
        with self._lock:
            micro = self.micro
            spent = _usd_from_micro(micro)
            return {
                "turns": self.turns,
                "calls": self.calls,
                "steps": self.steps,
                "cost_usd": spent,
                "credit_usd": _usd_from_micro(self.credit_micro),
                "remaining_credit_usd": _usd_from_micro(
                    max(0, self.credit_micro - micro)
                ),
                "percent_of_credit": (
                    round(100.0 * micro / self.credit_micro, 4)
                    if self.credit_micro
                    else 0.0
                ),
                # The same money seen from the tiers' side. It sums to the total
                # above for turns that completed; a call whose turn then failed is
                # in `calls` and here, but never in the total, because the total is
                # what we can stand behind and a crashed turn's cost is an estimate.
                "calls_cost_usd": _usd_from_micro(self.called_micro),
                "by_tier": {
                    tier: {
                        "calls": row["calls"],
                        "cost_usd": _usd_from_micro(row["micro"]),
                    }
                    for tier, row in sorted(self.by_tier.items())
                },
                "session_cap": self.cap,
                "sessions_seen": len(self._sessions),
                "started_at": self.started_at,
                "last_turn_at": (
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.last_turn))
                    if self.last_turn
                    else None
                ),
            }

    def reset(self) -> None:
        with self._lock:
            self.turns = 0
            self.calls = 0
            self.steps = 0
            self.micro = 0
            self.called_micro = 0
            self.by_tier = {}
            self.last_turn = 0.0
            self._sessions = {}
            self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
