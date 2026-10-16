"""The scheduler: what is due, and what to say about it.

Step 8, and it has two jobs.

**The morning brief.** One short answer to "what should I be doing today",
composed *in code* from the student's own files: the deadlines they asked us to
remember, the topics they told us are hard, and whether a quiz is open. No model
call, for three reasons that all matter:

1. It costs nothing. A brief that spends money every morning is a brief that
   quietly empties a $25 account.
2. It cannot invent a deadline. Every date in the output was in a sentence the
   student wrote, or it is not in the output at all.
3. It is the same answer twice. A model asked the same thing twice will phrase it
   two ways, and "did my deadline move?" is not a question this should raise.

**The nudges.** The subset of those deadlines that are close enough to act on:
overdue, due today, or inside the horizon (a week, by default).

Everything in this module is a pure function of the arguments it is given - no
files, no clock, no network - because that is what makes "which deadline is
next?" testable without waiting for November. The server owns the I/O and passes
in `today`.

The date rules are deliberately conservative, and the honest gap is stated rather
than papered over: a fact with no date we can read is reported as *undated* and
never guessed at. A made-up deadline is worse than a missing one, because the
student would plan around it.
"""

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------- dates ------
# What we accept, and nothing cleverer than this:
#
#   2026-11-12          an exact date, the form the assistant writes itself
#   November 12         a month name and a day, with or without a year
#   Nov 12 / Nov. 12    the three-letter form
#   12 November         day-first, because half the world writes it that way
#
# A bare "the 12th" is NOT accepted: with no month it is not a date, and the
# fact is reported as undated rather than assigned to whichever month is next.

MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

_MONTH_BY_WORD = {}
for _number, _name in enumerate(MONTHS, start=1):
    _MONTH_BY_WORD[_name] = _number
    _MONTH_BY_WORD[_name[:3]] = _number

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_FIRST = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\b")

# How far ahead a deadline counts as a nudge.
DEFAULT_HORIZON_DAYS = 7

# Fact text is bounded at write time too (MAX_FACT_CHARS), but a line that long is
# unreadable in a brief, so these keep the output scannable.
MAX_DEADLINE_CHARS = 120
MAX_WEAK_TOPICS = 3


def _as_date(year: int, month: int, day: int) -> Optional[date]:
    """Build a date, or None when the numbers are not a real one (Feb 30)."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_from(word: str) -> Optional[int]:
    return _MONTH_BY_WORD.get(word.strip(".").lower())


def dates_in(text: str, today: date) -> List[date]:
    """Every date this sentence states, resolved against `today`.

    A date written without a year means the *next* time that day comes round:
    a midterm in November, mentioned in December, is next November. That is the
    only reading that keeps a deadline in the future rather than silently in the
    past, and it is a documented guess rather than a hidden one.
    """
    found: List[date] = []

    for year, month, day in _ISO.findall(text):
        resolved = _as_date(int(year), int(month), int(day))
        if resolved:
            found.append(resolved)

    for word, day in _MONTH_FIRST.findall(text):
        month = _month_from(word)
        if month:
            resolved = _resolve_without_year(month, int(day), today)
            if resolved:
                found.append(resolved)

    for day, word in _DAY_FIRST.findall(text):
        month = _month_from(word)
        if month:
            resolved = _resolve_without_year(month, int(day), today)
            if resolved:
                found.append(resolved)

    # "November 12" matched twice (month-first and, harmlessly, nothing else) -
    # dedupe while keeping the order the sentence used.
    seen = set()
    unique = []
    for value in found:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _resolve_without_year(month: int, day: int, today: date) -> Optional[date]:
    """This year if that day is still to come, otherwise next year."""
    this_year = _as_date(today.year, month, day)
    if this_year and this_year >= today:
        return this_year
    return _as_date(today.year + 1, month, day)


def due_date(text: str, today: date) -> Optional[date]:
    """The date this fact is *about*.

    Facts can name more than one day ("labs are Tuesdays, the report is due
    2026-11-05"). The next one that has not passed is what a student means, so
    that is the rule: the earliest date on or after today, or - when every date
    in the sentence has been and gone - the most recent one, which is the thing
    that is now overdue.
    """
    dates = dates_in(text, today)
    if not dates:
        return None
    upcoming = [value for value in dates if value >= today]
    return min(upcoming) if upcoming else max(dates)


# ----------------------------------------------------------- deadlines ------

@dataclass(frozen=True)
class Deadline:
    """One dated thing, and how close it is."""

    id: str
    text: str
    due: str          # YYYY-MM-DD
    days_left: int    # 0 is today, negative is overdue

    @property
    def overdue(self) -> bool:
        return self.days_left < 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "due": self.due,
            "days_left": self.days_left,
            "overdue": self.overdue,
        }


def _trim(text: str, limit: int = MAX_DEADLINE_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def deadlines(
    facts: Sequence[dict], today: date
) -> Tuple[List[Deadline], List[dict]]:
    """Split remembered facts into dated deadlines and undated notes.

    Both halves are returned. Dropping the undated ones would hide a note the
    student wrote from the one screen they look at every morning, and inventing a
    date for them is exactly the failure this module exists to avoid.
    """
    dated: List[Deadline] = []
    undated: List[dict] = []

    for fact in facts:
        text = _trim(str(fact.get("text") or ""))
        if not text:
            continue
        when = due_date(text, today)
        if when is None:
            undated.append({"id": fact.get("id"), "text": text})
            continue
        dated.append(
            Deadline(
                id=str(fact.get("id") or ""),
                text=text,
                due=when.isoformat(),
                days_left=(when - today).days,
            )
        )

    # Soonest first, which puts the most overdue at the top: the thing that has
    # already slipped is the thing to say first.
    dated.sort(key=lambda item: (item.days_left, item.due, item.text))
    return dated, undated


def _phrase(deadline: Deadline) -> str:
    if deadline.days_left < 0:
        count = -deadline.days_left
        return f"overdue by {count} day{'s' if count != 1 else ''}"
    if deadline.days_left == 0:
        return "due today"
    if deadline.days_left == 1:
        return "due tomorrow"
    return f"in {deadline.days_left} days"


def nudges(
    dated: Iterable[Deadline], horizon_days: int = DEFAULT_HORIZON_DAYS
) -> List[str]:
    """The lines worth acting on today: overdue, or inside the horizon."""
    return [
        f"{_phrase(deadline)} — {deadline.text}"
        for deadline in dated
        if deadline.days_left <= horizon_days
    ]


def headline(dated: Sequence[Deadline], horizon_days: int) -> str:
    """One sentence for the top of the brief.

    Written here rather than by a model so it can only ever say what is in the
    list below it - and so "nothing is due" is a fact rather than a cheerful
    guess.
    """
    if not dated:
        return "Nothing due that I know about."

    first = dated[0]
    if first.overdue:
        count = -first.days_left
        more = len(dated) - 1
        tail = f" ({more} more dated {'item' if more == 1 else 'items'})" if more else ""
        return f"Overdue: {count} day{'s' if count != 1 else ''} past {first.due}{tail}."

    if first.days_left == 0:
        return "Something is due today."
    if first.days_left == 1:
        return "Something is due tomorrow."
    if first.days_left <= horizon_days:
        return f"Next deadline is in {first.days_left} days, on {first.due}."
    return f"Nothing due in the next {horizon_days} days. Next is {first.due}."


def compose_brief(
    facts: Sequence[dict],
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    weak_topics: Optional[Sequence[str]] = None,
    open_quiz: Optional[dict] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """The morning brief, as JSON-ready data.

    Field names are the API's contract; the panel renders them and nothing else,
    so a new line here means a deliberate change on both sides.

    `cost_usd` is present and always 0.0 on purpose: every other reply from this
    backend reports what it cost, and the honest value for this one is nothing.
    """
    dated, undated = deadlines(facts, today)
    soon = [deadline for deadline in dated if deadline.days_left <= horizon_days]
    overdue = [deadline for deadline in dated if deadline.overdue]

    return {
        "date": today.isoformat(),
        "headline": headline(dated, horizon_days),
        "horizon_days": horizon_days,
        "deadlines": [deadline.as_dict() for deadline in dated],
        "nudges": nudges(dated, horizon_days),
        "undated": undated,
        "weak_topics": [str(topic) for topic in (weak_topics or [])][:MAX_WEAK_TOPICS],
        "open_quiz": open_quiz,
        "counts": {
            "dated": len(dated),
            "overdue": len(overdue),
            "due_soon": len(soon),
            "undated": len(undated),
        },
        "cost_usd": 0.0,
        "source": "code",
        "generated_at": generated_at or time.strftime("%H:%M:%S"),
    }


# ------------------------------------------------------ the daily tick ------

def brief_due(hour_now: int, today: str, last_logged: str, hour: int) -> bool:
    """Whether the server should log today's brief now.

    Two conditions, and the second is the one that stops a loop from writing the
    same line sixty times an hour: it is past the configured hour, and no brief
    has been logged today.

    `last_logged` lives in the process, so a restart after the hour logs the brief
    again. That is a duplicate line in the action log rather than anything a
    student sees, and it is a better trade than a state file whose absence or
    corruption would silently disable the feature.
    """
    return hour_now >= hour and last_logged != today
