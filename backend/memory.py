"""Persistent memory: plain Markdown on disk, one file per topic.

Five design rules, each chosen against an alternative that fails in a way the
student would notice.

**1. One file per topic, plain Markdown.** The README promises that memory is
"yours to read, edit, and delete". A SQLite table cannot be opened in a text
editor, so that promise decides the format: `memory/deadlines.md`,
`memory/classes.md`, and so on.

**2. A fact is an ordinary bullet, with its id in a trailing HTML comment.**

    - organic chemistry midterm is 2026-11-12 <!-- id: 3f9c1a2b added: 2026-10-14 -->

The comment is invisible when the file is rendered, so the file reads like notes
rather than a database. Deleting a line deletes the fact, exactly as a person
expects. Keeping the id in the file (rather than deriving it from position)
means deleting fact 2 cannot silently renumber fact 3 into its place.

**3. A bullet with no id still works.** Hand-edited facts are the common case for
a file the student owns, so a bullet with no comment is accepted and given a
*derived* id (`auto-<hash of text>`). That id is stable across restarts, so it
can still be deleted from the UI. The tradeoff is deliberate and documented: edit
the words and the derived id changes, so a stale UI delete is a no-op rather than
removing the wrong fact.

**4. Writes are atomic.** Write a temp file, then `os.replace`. A crash mid-write
leaves the previous memory intact instead of a half-written file, and a student
reading the file over their shoulder never sees a truncated one.

**5. The topic name is validated, never trusted.** Topics become filenames, so
`../../etc/passwd` must be impossible rather than merely unlikely. A strict slug
pattern is the only thing standing between a model-supplied string and the
filesystem, so it is enforced here and again at the API boundary.

What this module deliberately does NOT do: cap the number of facts. Silently
dropping something the student asked us to remember is worse than a long file.
Only the *prompt block* is capped, because that is the part that costs tokens.
"""

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from frontmatter import FrontmatterError, parse

# Topics become filenames. Lowercase, snake_case, and bounded - no dots, no
# slashes, nothing that can climb out of the memory directory.
TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_FACT_CHARS = 500

# Filenames that are documentation, not memory. A directory we invite the student
# to open in an editor will eventually contain a README, and treating one as a
# topic would show up as a bogus entry in their memory - or worse, as a load error
# telling them a file they wrote on purpose is broken.
NOT_TOPICS = {"readme", "index"}

_BULLET_RE = re.compile(r"^[-*+]\s+(?P<body>.+?)\s*$")
_COMMENT_RE = re.compile(r"<!--(?P<inner>.*?)-->")
_ID_RE = re.compile(r"\bid:\s*(?P<id>[A-Za-z0-9_-]+)")
_ADDED_RE = re.compile(r"\badded:\s*(?P<added>[0-9T:\-]+)")


class MemoryError_(ValueError):
    """A memory file cannot be read, or a write was refused."""


def validate_topic(topic: str) -> str:
    """Return the clean topic slug, or raise. The only gate to the filesystem."""
    if not isinstance(topic, str):
        raise MemoryError_(f"topic must be a string, got {type(topic).__name__}")
    slug = topic.strip().lower()
    if not slug:
        raise MemoryError_("topic must not be empty")
    if not TOPIC_RE.match(slug):
        raise MemoryError_(
            f"topic {topic!r} is not usable as a filename - use lowercase "
            "letters, digits and underscores, up to 32 characters"
        )
    return slug


# The topics the *model* may choose from. Free-form slugs remain legal for files a
# student writes by hand - this constrains only what gets invented automatically,
# because "one new file per utterance" is how a memory feature becomes a junk
# drawer. A topic outside this set is filed under `general` rather than rejected:
# the fact matters more than the label.
CANONICAL_TOPICS = (
    "deadlines",
    "classes",
    "weak_topics",
    "preferences",
    "people",
    "general",
)


def canonical_topic(topic) -> str:
    """Coerce any model-chosen topic into the known set, or `general`.

    Never raises. This runs on a value a language model produced, and the useful
    outcome for a bad label is "stored somewhere sensible", not "not stored".
    """
    try:
        slug = validate_topic(topic)
    except MemoryError_:
        return "general"
    return slug if slug in CANONICAL_TOPICS else "general"


def derive_id(text: str) -> str:
    """A stable id for a bullet that has none (i.e. a hand-written one)."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"auto-{digest}"


def _title_for(topic: str) -> str:
    return topic.replace("_", " ").strip().capitalize()


def _resolve_topic(path: Path, meta: dict) -> str:
    """Decide which topic a memory file represents.

    The filename wins when it is already a usable slug, because that is where the
    file lives and therefore where a write has to land. A file called
    `deadlines.md` whose frontmatter says `topic: Deadlines` is **not** an error -
    it is a hand-edit, and the student's intent is unmistakable. Refusing to read
    it would throw away their memory to punish a capital letter.

    Only when neither the filename nor the frontmatter yields a usable slug do we
    give up on the file, and then we say exactly why.
    """
    for candidate in (path.stem, meta.get("topic")):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            return validate_topic(candidate)
        except MemoryError_:
            continue
    raise MemoryError_(
        f"neither the filename ({path.name}) nor its frontmatter 'topic' is a usable "
        "topic name - use lowercase letters, digits and underscores"
    )


@dataclass(frozen=True)
class Fact:
    id: str
    topic: str
    text: str
    added: str
    # True when the id was derived from the text rather than written into the
    # file - useful in tests and in the log, never shown in the UI.
    derived: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "topic": self.topic, "text": self.text, "added": self.added}


@dataclass
class Topic:
    topic: str
    title: str
    description: str
    updated: str
    path: Path
    facts: List[Fact]

    @property
    def count(self) -> int:
        return len(self.facts)

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "title": self.title,
            "description": self.description,
            "updated": self.updated,
            "count": self.count,
            "path": str(self.path),
        }


class MemoryStore:
    """All of the student's memory, loaded from `root/*.md`."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.topics: Dict[str, Topic] = {}
        self.errors: List[str] = []
        self._mtimes: Dict[str, int] = {}
        # One lock for the whole store. Writes are rare (only an explicit
        # "remember ..."), so per-topic locking would be complexity without a
        # measurable gain.
        self._lock = threading.Lock()

    # ------------------------------------------------------------- loading --

    def load(self) -> "MemoryStore":
        """Read every *.md file. One unreadable file does not hide the rest."""
        self.topics, self.errors, self._mtimes = {}, [], {}
        if not self.root.is_dir():
            return self
        for path in sorted(self.root.glob("*.md")):
            if path.stem.lower() in NOT_TOPICS:
                continue
            try:
                topic = self._read_topic(path)
            except (MemoryError_, FrontmatterError, OSError) as exc:
                # Surface the problem and keep going: a memory file the student
                # half-edited must not stop them from talking to the assistant.
                self.errors.append(f"{path.name}: {exc}")
                continue
            if topic.topic in self.topics:
                self.errors.append(
                    f"{path.name}: duplicate topic {topic.topic!r} "
                    f"(already read from {self.topics[topic.topic].path.name})"
                )
                continue
            self.topics[topic.topic] = topic
            self._mtimes[topic.topic] = self._mtime(path)
        return self

    @staticmethod
    def _mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _read_topic(self, path: Path) -> Topic:
        """Parse one memory file into a Topic."""
        text = path.read_text(encoding="utf-8")
        meta, body = parse(text)

        topic = _resolve_topic(path, meta)

        facts: List[Fact] = []
        seen: Dict[str, int] = {}
        in_fence = False
        for line in body.splitlines():
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                # A bullet inside a code block is an example, not a memory.
                continue
            match = _BULLET_RE.match(line.strip())
            if not match:
                continue
            fact_text, fact_id, added = _split_fact(match.group("body"))
            if not fact_text:
                continue
            derived = fact_id is None
            fact_id = fact_id or derive_id(fact_text)
            # Duplicate ids in a hand-edited file would make delete ambiguous.
            if fact_id in seen:
                seen[fact_id] += 1
                fact_id = f"{fact_id}-{seen[fact_id]}"
            else:
                seen[fact_id] = 0
            facts.append(
                Fact(id=fact_id, topic=topic, text=fact_text, added=added or "", derived=derived)
            )

        return Topic(
            topic=topic,
            title=str(meta.get("title") or _title_for(topic)),
            description=str(meta.get("description") or "").strip(),
            updated=str(meta.get("updated") or ""),
            path=path,
            facts=facts,
        )

    def _refresh(self, topic: str) -> None:
        """Re-read a topic whose file changed on disk.

        This is what makes the "edit the Markdown yourself" promise real rather
        than aspirational: a hand-edit shows up on the next read, without a
        restart and without us holding a stale copy.
        """
        known = self.topics.get(topic)
        if known is None:
            return
        if self._mtime(known.path) == self._mtimes.get(topic, -1):
            return
        try:
            refreshed = self._read_topic(known.path)
        except (MemoryError_, FrontmatterError, OSError) as exc:
            self.errors.append(f"{known.path.name}: {exc}")
            return
        self.topics[topic] = refreshed
        self._mtimes[topic] = self._mtime(known.path)

    # ------------------------------------------------------------ querying --

    def topic_names(self) -> List[str]:
        return sorted(self.topics)

    def get(self, topic: str) -> Optional[Topic]:
        name = validate_topic(topic)
        self._refresh(name)
        return self.topics.get(name)

    def facts(self, topic: Optional[str] = None) -> List[Fact]:
        """Every fact, or one topic's, in file order."""
        if topic is not None:
            found = self.get(topic)
            return list(found.facts) if found else []
        out: List[Fact] = []
        for name in self.topic_names():
            self._refresh(name)
            out.extend(self.topics[name].facts)
        return out

    def count(self) -> int:
        return sum(t.count for t in self.topics.values())

    def describe(self) -> dict:
        for name in self.topic_names():
            self._refresh(name)
        listed = [self.topics[n] for n in self.topic_names()]
        return {
            "topics": [t.as_dict() for t in listed],
            "facts": [f.as_dict() for t in listed for f in t.facts],
            "total": sum(t.count for t in listed),
            "errors": list(self.errors),
            "directory": str(self.root),
        }

    # -------------------------------------------------------------- writing --

    def add(self, topic: str, text: str, added: str = "") -> Fact:
        """Append one fact, creating the topic file if this is a new topic."""
        slug = validate_topic(topic)
        fact_text = " ".join(str(text).split())
        if not fact_text:
            raise MemoryError_("a memory with no text is not worth storing")
        if len(fact_text) > MAX_FACT_CHARS:
            raise MemoryError_(
                f"memory text is {len(fact_text)} characters; the limit is {MAX_FACT_CHARS}"
            )

        with self._lock:
            self._refresh(slug)
            existing = self.topics.get(slug)
            known_ids = {f.id for f in existing.facts} if existing else set()

            fact_id = uuid.uuid4().hex[:8]
            while fact_id in known_ids:
                fact_id = uuid.uuid4().hex[:8]

            fact = Fact(id=fact_id, topic=slug, text=fact_text, added=added)

            if existing is None:
                topic_obj = Topic(
                    topic=slug,
                    title=_title_for(slug),
                    description="",
                    updated=added,
                    path=self.root / f"{slug}.md",
                    facts=[fact],
                )
            else:
                topic_obj = Topic(
                    topic=existing.topic,
                    title=existing.title,
                    description=existing.description,
                    updated=added or existing.updated,
                    path=existing.path,
                    facts=list(existing.facts) + [fact],
                )

            self._write(topic_obj)
            self.topics[slug] = topic_obj
            self._mtimes[slug] = self._mtime(topic_obj.path)
            return fact

    def delete(self, topic: str, fact_id: str) -> bool:
        """Remove exactly one fact. Returns False if it was already gone.

        False is not an error: the UI may be showing a fact the student already
        deleted in their editor, and re-deleting should settle rather than shout.
        """
        slug = validate_topic(topic)
        if not isinstance(fact_id, str) or not fact_id.strip():
            raise MemoryError_("a fact id is required to delete")
        with self._lock:
            self._refresh(slug)
            existing = self.topics.get(slug)
            if existing is None:
                return False
            remaining = [f for f in existing.facts if f.id != fact_id.strip()]
            if len(remaining) == len(existing.facts):
                return False
            topic_obj = Topic(
                topic=existing.topic,
                title=existing.title,
                description=existing.description,
                updated=existing.updated,
                path=existing.path,
                facts=remaining,
            )
            self._write(topic_obj)
            self.topics[slug] = topic_obj
            self._mtimes[slug] = self._mtime(topic_obj.path)
            return True

    def _write(self, topic: Topic) -> None:
        """Render a topic to Markdown atomically."""
        self.root.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"topic: {topic.topic}", f"title: {topic.title}"]
        if topic.description:
            lines.append(f'description: "{topic.description}"')
        if topic.updated:
            lines.append(f"updated: {topic.updated}")
        lines += ["---", "", f"# {topic.title}", ""]
        for fact in topic.facts:
            comment = f"<!-- id: {fact.id}"
            if fact.added:
                comment += f" added: {fact.added}"
            comment += " -->"
            lines.append(f"- {fact.text} {comment}")
        lines.append("")

        target = topic.path
        temp = target.with_suffix(".md.tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        # Atomic: a reader never sees a partially written file.
        temp.replace(target)

    # --------------------------------------------------------------- prompt --

    def prompt_block(self, max_facts: int = 12, max_chars: int = 700) -> str:
        """A compact block of facts for a model prompt.

        Capped, because this is the part that costs tokens on every request. The
        cap is on what we *send*, never on what we store - dropping a fact the
        student asked us to remember would be a silent, invisible failure.
        """
        facts = self.facts()
        if not facts:
            return ""
        chunks, used = [], 0
        for fact in facts[:max_facts]:
            piece = f"- {fact.topic}: {fact.text}"
            if used + len(piece) > max_chars:
                break
            chunks.append(piece)
            used += len(piece)
        if not chunks:
            return ""
        omitted = len(facts) - len(chunks)
        block = (
            "Things this student has told you to remember. Use them when relevant, "
            "and never contradict them:\n" + "\n".join(chunks)
        )
        if omitted > 0:
            block += f"\n({omitted} more not shown)"
        return block


def _split_fact(body: str):
    """Split a bullet body into (text, id_or_None, added_or_empty)."""
    fact_id = None
    added = ""
    for comment in _COMMENT_RE.finditer(body):
        inner = comment.group("inner")
        id_match = _ID_RE.search(inner)
        if id_match:
            fact_id = id_match.group("id")
        added_match = _ADDED_RE.search(inner)
        if added_match:
            added = added_match.group("added")
    text = _COMMENT_RE.sub("", body).strip()
    return text, fact_id, added
