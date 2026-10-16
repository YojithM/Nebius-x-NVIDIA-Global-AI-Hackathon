"""Quizzes and what they taught us: plain Markdown on disk, one file per quiz.

This is the store behind the `quiz` skill and the Progress tab. It is the same
bet as memory, for the same reason: **the student can open it, read it, and
delete it.** A study record is more sensitive than a note - it is a list of the
things you got wrong - so it lives in `progress/*.md` where anyone can see it,
and the Progress tab is a view over those files rather than a separate database.

Five decisions, each against an alternative that fails visibly.

**1. One file per quiz, named for when it happened.**

    progress/2026-10-14-organic-chemistry-8f2a1c3d.md

A student can find the quiz they took on Tuesday without knowing our id scheme,
and deleting one is `rm`, not a row lookup.

**2. The questions are the file's body, one numbered item each.**

    1. What is the rate-determining step of an SN1 reaction?
       - expected: loss of the leaving group to form a carbocation
       - answer: losing the chloride
       - verdict: correct

The file reads like a worked quiz because it *is* one. `expected` is kept on
disk: mid-quiz the server grades against it, and after a restart the open session
has to be able to continue - a quiz that becomes ungradeable because we restarted
would be a strange way to lose a student's work. Yes, the student can peek at the
answer. It is their quiz.

**3. A verdict is a closed set, and `unclear` is one of the values.**

    unanswered | correct | incorrect | skipped | unclear

`unclear` exists because grading can legitimately fail (a model reply we cannot
parse). Scoring that as wrong would punish the student for our bug, and scoring
it as right would inflate their progress. So it is neither, it is visible in the
file, and it does not count toward the score.

**4. Writes are atomic**, exactly as in `memory.py`: temp file, then `os.replace`.
A crash mid-write leaves the previous version rather than half a question.

**5. Only one quiz can be open at a time.** Two live sessions would make "the
next thing the student says" ambiguous, and ambiguity is the one thing this
feature cannot afford. `start()` closes whatever was open first, marking the
unanswered questions as left behind rather than inventing verdicts for them.

What this module deliberately does NOT do: decide whether a turn *is* an answer.
That is the request handler's job (`backend/server.py`), for the same reason the
memory gate lives there - the policy belongs next to the routing decision it has
to agree with.
"""

import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from frontmatter import FrontmatterError, parse

# Verdicts a question can carry. `scored` below is derived from this set, so a new
# value cannot silently start counting toward the score.
VERDICTS = {"unanswered", "correct", "incorrect", "skipped", "unclear"}
SCORED_VERDICTS = {"correct", "incorrect"}

MAX_TOPIC_CHARS = 80
DEFAULT_QUESTION_COUNT = 5
MAX_QUESTION_COUNT = 10

NOT_SESSIONS = {"readme", "index"}

_QUESTION_RE = re.compile(r"^(?P<number>\d{1,3})\.\s+(?P<text>.+?)\s*$")
_ITEM_RE = re.compile(
    r"^\s+[-*+]\s*(?P<key>expected|answer|verdict)\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
# Anything that cannot sit in a filename. Topics here are the student's words
# ("organic chemistry"), not slugs, so this maps rather than rejects.
_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, fallback: str = "quiz") -> str:
    """A filename-safe slug for a topic the student said out loud."""
    slug = _UNSAFE.sub("-", str(text or "").strip().lower()).strip("-")
    return (slug or fallback)[:48]


def clean_topic(topic, fallback: str = "this topic") -> str:
    """The topic as a short human string. Never raises: it becomes a title."""
    text = " ".join(str(topic or "").split())[:MAX_TOPIC_CHARS].strip(" .,;:-")
    return text or fallback


def clamp_count(value, default: int = DEFAULT_QUESTION_COUNT) -> int:
    """How many questions to ask. Bounded: ten is already a long sitting."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    if count < 1:
        return default
    return min(count, MAX_QUESTION_COUNT)


@dataclass
class Question:
    text: str
    expected: str = ""
    answer: str = ""
    verdict: str = "unanswered"

    @property
    def scored(self) -> bool:
        return self.verdict in SCORED_VERDICTS

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "expected": self.expected,
            "answer": self.answer,
            "verdict": self.verdict,
        }


@dataclass
class Session:
    id: str
    topic: str
    source: str
    started: str
    status: str
    questions: List[Question]
    path: Path = field(default=None)

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def correct(self) -> int:
        return sum(1 for q in self.questions if q.verdict == "correct")

    @property
    def wrong(self) -> int:
        return sum(1 for q in self.questions if q.verdict == "incorrect")

    @property
    def graded(self) -> int:
        return sum(1 for q in self.questions if q.scored)

    @property
    def ungraded(self) -> int:
        return self.total - self.graded

    @property
    def finished(self) -> bool:
        return self.status != "open"

    @property
    def index(self) -> Optional[int]:
        """The next question to ask, or None when there is nothing left."""
        for position, question in enumerate(self.questions):
            if question.verdict == "unanswered":
                return position
        return None

    def next_question(self) -> Optional[Question]:
        position = self.index
        return None if position is None else self.questions[position]

    def score_text(self) -> str:
        if self.graded == 0:
            return "no answers yet"
        return f"{self.correct} of {self.graded}"

    def as_dict(self, include_questions: bool = True) -> dict:
        position = self.index
        data = {
            "id": self.id,
            "topic": self.topic,
            "source": self.source,
            "started": self.started,
            "status": self.status,
            "total": self.total,
            # Which question is next, 1-based, so the UI reads "question 2 of 5"
            # without knowing anything about our indices. None when none is left.
            "question": None if position is None else position + 1,
            "correct": self.correct,
            "wrong": self.wrong,
            "graded": self.graded,
            "unofficial": self.ungraded,
            "score": self.score_text(),
            "path": str(self.path) if self.path else "",
        }
        if include_questions:
            data["questions"] = [q.as_dict() for q in self.questions]
        return data


class QuizError(ValueError):
    """A progress file cannot be read, or a write was refused."""


class QuizStore:
    """Every quiz on disk, plus the one that is currently open."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.sessions: Dict[str, Session] = {}
        self.errors: List[str] = []
        self._mtimes: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- loading --

    def load(self) -> "QuizStore":
        """Read every *.md file. One unreadable file does not hide the rest."""
        self.sessions, self.errors, self._mtimes = {}, [], {}
        if not self.root.is_dir():
            return self
        for path in sorted(self.root.glob("*.md")):
            if path.stem.lower() in NOT_SESSIONS:
                continue
            try:
                session = self._read(path)
            except (QuizError, FrontmatterError, OSError) as exc:
                # Same reasoning as memory: a half-edited file must not stop the
                # student from being quizzed, it just gets reported.
                self.errors.append(f"{path.name}: {exc}")
                continue
            if session.id in self.sessions:
                self.errors.append(f"{path.name}: duplicate quiz id {session.id!r}")
                continue
            self.sessions[session.id] = session
            self._mtimes[session.id] = self._mtime(path)
        return self

    @staticmethod
    def _mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _read(self, path: Path) -> Session:
        text = path.read_text(encoding="utf-8")
        meta, body = parse(text)

        session_id = str(meta.get("session") or path.stem).strip()
        if not session_id:
            raise QuizError("no session id in the frontmatter or the filename")

        questions: List[Question] = []
        current: Optional[Question] = None
        for line in body.splitlines():
            question_match = _QUESTION_RE.match(line)
            if question_match:
                current = Question(text=question_match.group("text"))
                questions.append(current)
                continue
            item_match = _ITEM_RE.match(line)
            if item_match and current is not None:
                key = item_match.group("key").lower()
                value = item_match.group("value").strip()
                if key == "verdict":
                    # A verdict we do not know how to score is not a verdict.
                    current.verdict = value if value in VERDICTS else "unclear"
                elif key == "expected":
                    current.expected = value
                else:
                    current.answer = value

        if not questions:
            # A "quiz" with no questions is not a quiz, and treating it as an
            # empty open session would let it silently swallow the next answer.
            raise QuizError("no questions found in the file")

        raw_status = str(meta.get("status") or "open").strip().lower()
        return Session(
            id=session_id,
            topic=clean_topic(meta.get("topic"), fallback="a quiz"),
            source=str(meta.get("source") or "topic"),
            started=str(meta.get("started") or ""),
            status=raw_status if raw_status in {"open", "finished", "abandoned"} else "open",
            questions=questions,
            path=path,
        )

    def _refresh(self, session_id: str) -> None:
        """Re-read a session whose file changed underneath us (a hand-edit)."""
        known = self.sessions.get(session_id)
        if known is None or known.path is None:
            return
        if self._mtime(known.path) == self._mtimes.get(session_id, -1):
            return
        try:
            refreshed = self._read(known.path)
        except (QuizError, FrontmatterError, OSError) as exc:
            self.errors.append(f"{known.path.name}: {exc}")
            return
        self.sessions[session_id] = refreshed
        self._mtimes[session_id] = self._mtime(known.path)

    # ------------------------------------------------------------ querying --

    def active(self) -> Optional[Session]:
        """The one open quiz, or None. A file that vanished is simply not open."""
        for session_id in list(self.sessions):
            self._refresh(session_id)
        open_sessions = [s for s in self.sessions.values() if s.status == "open"]
        if not open_sessions:
            return None
        # Newest start wins if a hand-edit ever leaves two open at once.
        return max(open_sessions, key=lambda s: (s.started, s.id))

    def get(self, session_id: str) -> Optional[Session]:
        self._refresh(session_id)
        return self.sessions.get(session_id)

    def ordered(self) -> List[Session]:
        """Newest first, which is the order the Progress tab wants."""
        for session_id in list(self.sessions):
            self._refresh(session_id)
        return sorted(
            self.sessions.values(), key=lambda s: (s.started, s.id), reverse=True
        )

    def weakness(self, limit: int = 5) -> List[dict]:
        """Per-topic accuracy, worst first. The evidence behind "weakest"."""
        by_topic: Dict[str, dict] = {}
        for session in self.ordered():
            bucket = by_topic.setdefault(
                session.topic.lower(),
                {"topic": session.topic, "asked": 0, "correct": 0, "quizzes": 0},
            )
            bucket["asked"] += session.graded
            bucket["correct"] += session.correct
            bucket["quizzes"] += 1
        rows = []
        for bucket in by_topic.values():
            if bucket["asked"] == 0:
                continue
            bucket["accuracy"] = round(bucket["correct"] / bucket["asked"], 2)
            rows.append(bucket)
        # Worst accuracy first; on a tie the topic with more evidence is worse
        # news, so it outranks the one we have barely seen.
        rows.sort(key=lambda row: (row["accuracy"], -row["asked"], row["topic"]))
        return rows[:limit]

    def describe(self, limit: int = 20, include_questions: bool = False) -> dict:
        """Everything the Progress tab renders (and the /progress endpoint)."""
        sessions = self.ordered()
        active = self.active()
        totals = {
            "quizzes": len(sessions),
            "questions": sum(s.total for s in sessions),
            "answered": sum(s.graded for s in sessions),
            "correct": sum(s.correct for s in sessions),
            "finished": sum(1 for s in sessions if s.finished),
        }
        return {
            "active": active.as_dict() if active else None,
            "sessions": [s.as_dict(include_questions) for s in sessions[:limit]],
            "total_sessions": len(sessions),
            "weakest": self.weakness(),
            "totals": totals,
            "errors": list(self.errors),
            "directory": str(self.root),
        }

    # ------------------------------------------------------------- writing --

    def start(self, topic: str, source: str, questions: List[Question],
              started: str = "") -> Session:
        """Open a new quiz, closing any quiz that was still open.

        The previous session is not deleted and its answers are not touched: the
        questions nobody reached stay `unanswered`, which is the truth. Marking
        them wrong would be inventing data about the student.
        """
        if not questions:
            raise QuizError("a quiz needs at least one question")
        topic_text = clean_topic(topic)
        with self._lock:
            previous = self.active()
            if previous is not None:
                previous.status = "abandoned"
                self._write(previous)

            session_id = uuid.uuid4().hex[:8]
            while session_id in self.sessions:
                session_id = uuid.uuid4().hex[:8]
            name = f"{started or 'quiz'}-{slugify(topic_text)}-{session_id}.md"
            session = Session(
                id=session_id,
                topic=topic_text,
                source=str(source or "topic"),
                started=str(started or ""),
                status="open",
                questions=list(questions),
                path=self.root / name,
            )
            self._write(session)
            self.sessions[session_id] = session
            self._mtimes[session_id] = self._mtime(session.path)
            return session

    def record(self, session: Session, answer: str, verdict: str) -> Question:
        """Attach one answer and its verdict to the open question."""
        question = session.next_question()
        if question is None:
            raise QuizError("there is no question to answer")
        if verdict not in VERDICTS:
            verdict = "unclear"
        with self._lock:
            question.answer = " ".join(str(answer or "").split())[:600]
            question.verdict = verdict
            self._write(session)
            self._mtimes[session.id] = self._mtime(session.path)
        return question

    def skip(self, session: Session) -> Question:
        """Move past a question without scoring it wrong - they did not try."""
        return self.record(session, "", "skipped")

    def finish(self, session: Session, status: str = "finished") -> Session:
        with self._lock:
            session.status = status if status in {"finished", "abandoned"} else "finished"
            self._write(session)
            self._mtimes[session.id] = self._mtime(session.path)
        return session

    def delete(self, session_id: str) -> bool:
        """Remove one quiz entirely. False when it was already gone."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise QuizError("a session id is required to delete")
        session_id = session_id.strip()
        with self._lock:
            session = self.sessions.pop(session_id, None)
            self._mtimes.pop(session_id, None)
            if session is None or session.path is None:
                return False
            try:
                session.path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                self.errors.append(f"{session.path.name}: {exc}")
                raise QuizError(f"could not delete {session.path.name}: {exc}") from None
            return True

    def clear(self) -> int:
        """Delete every quiz. Returns how many files went away."""
        removed = 0
        for session_id in [s.id for s in self.ordered()]:
            if self.delete(session_id):
                removed += 1
        return removed

    def _write(self, session: Session) -> None:
        """Render a session to Markdown atomically."""
        self.root.mkdir(parents=True, exist_ok=True)
        lines = [
            "---",
            f"session: {session.id}",
            f"topic: {session.topic}",
            f"source: {session.source}",
            f"started: {session.started}",
            f"status: {session.status}",
            f"correct: {session.correct}",
            f"answered: {session.graded}",
            f"of: {session.total}",
            "---",
            "",
            f"# Quiz — {session.topic}",
            "",
            f"Asked for by the student via {session.source}. Answers appear as they are given.",
            "",
        ]
        for number, question in enumerate(session.questions, start=1):
            lines.append(f"{number}. {question.text}")
            if question.expected:
                lines.append(f"   - expected: {question.expected}")
            if question.answer:
                lines.append(f"   - answer: {question.answer}")
            lines.append(f"   - verdict: {question.verdict}")
        lines.append("")

        target = session.path
        temp = target.with_suffix(".md.tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        temp.replace(target)
