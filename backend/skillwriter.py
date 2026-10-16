"""Writing a skill file the agent drafted: the one write the loop is allowed to ask for.

Step 9's label promised that the agent would write its own skills once the student
confirmed a task worked, and left the machinery for it and no write path. This is
that write path. The interesting half is not `os.replace` - it is deciding what a
*model-authored* SKILL.md is allowed to be, because a skill file is executed in the
only sense this project has: it is routed to, it is rendered into a prompt, and its
frontmatter can select a handler that writes to the student's files.

Six rules, each one a way this goes wrong:

1. **Nothing is written until the student has seen the exact text.** The tool that
   drafts the file is read-only; the only caller of `save()` is the server's
   approval path, and what it writes is the bytes that were shown. A "yes" that
   saved a *different* file from the one on screen would make the question a
   formality.
2. **A file written this way may only say things.** `action` must be `prompt`. The
   reviewed allowlist in `skills.py` (`prompt`, `remember`, `quiz`) decides what a
   skill file may *select*, and that is the right rule for files a person wrote and
   reviewed. A file drafted by the model in the same turn and approved from a
   preview is not that: the student sees frontmatter they did not parse. So the
   tasks with side effects stay hand-written, and a drafted file that tries to
   select one is refused with the reason. This is the whole safety property, and
   the same shape as `calculate` being a whitelist evaluator rather than a sandbox:
   we do not remove the capability, we decline to be the thing that grants it.
3. **The name is an identifier, not a path.** `[a-z][a-z0-9_]{0,60}`, so `../`,
   `/`, `.` and a leading digit cannot even be expressed - the traversal is refused
   by the grammar rather than by a check against a list of dangerous strings.
4. **The file has to load before it exists.** It is validated by the registry's own
   `load_skill` against a copy in a temp directory, so the folder-name rule, the
   frontmatter requirements, the argument schema and the tier check are the *same*
   rules the server will apply at startup. A file that would appear as a registry
   error after a restart is refused while it is still a draft, which is the only
   moment the model can do something about it.
5. **A skill is never overwritten.** Existing means refused, including a skill this
   module wrote earlier. Editing a skill is a different intent from adding one, and
   silently replacing routing rules the student relies on is not an edit anyone
   asked for.
6. **The write is atomic.** Temp file and `os.replace`, so a crash mid-write cannot
   leave a half-file that the next startup reports as a broken skill.
"""

import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from skills import Skill, SkillError, load_skill

# One folder name, and nothing else: lowercase, starting with a letter. A name is
# interpolated into a path, so the grammar is the guard.
SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,60}$")

# A skill file is a page of Markdown. Past this it is a model generating at
# length, and the student is the one who has to read it before saying yes.
MAX_SKILL_CHARS = 12000

# The action a drafted file is allowed to select. Named here, never passed in.
DRAFTED_ACTIONS = ("prompt",)


class SkillWriteError(ValueError):
    """A skill file cannot be written, with the reason a person can act on.

    The message goes to the student when a draft fails the server's own check, and
    to the *model* when the tool refuses one - which is why it says what is wrong
    with the file rather than just that it is wrong.

    `code` is a coarse bucket for the action log: "bad_name", "exists", "invalid",
    "action", "too_long", "empty".
    """

    def __init__(self, message, code="refused"):
        super().__init__(message)
        self.code = code


def skill_target(root, name) -> Path:
    """Where a skill called `name` lives. Assumes the name already passed its check."""
    return Path(root) / name / "SKILL.md"


def check(root, name, text, tiers=None) -> Skill:
    """Would this file load as a skill? -> the loaded Skill. Writes nothing.

    Raises SkillWriteError, so a caller that wants to ask the student for approval
    can only do so about a file that is already known to load. Called twice in a
    save - once when the draft is made, once when the file is actually written -
    because between the two the student may have taken minutes to answer, and the
    second call is the one that has to be true.
    """
    name = (name or "").strip()
    text = text if isinstance(text, str) else ""

    if not SKILL_NAME.match(name):
        raise SkillWriteError(
            "the name {0!r} is not usable as a skill name: it has to start with a "
            "letter and contain only lowercase letters, digits and underscores, at "
            "most 61 characters. Nothing else - no slashes, no dots, no spaces - so "
            "a name can only ever mean one folder under skills/.".format(name),
            "bad_name",
        )
    if not text.strip():
        raise SkillWriteError("the skill file is empty.", "empty")
    if len(text) > MAX_SKILL_CHARS:
        raise SkillWriteError(
            "the file is {0} characters, and a skill file is capped at {1} - shorten "
            "it rather than saving something the student has to wade through.".format(
                len(text), MAX_SKILL_CHARS
            ),
            "too_long",
        )

    target = skill_target(root, name)
    if target.exists():
        raise SkillWriteError(
            "a skill called {0!r} already exists, and this refuses to overwrite one - "
            "tell the student it is already there, or propose a different name.".format(
                name
            ),
            "exists",
        )

    # Validated as a real file in a throwaway directory, because that is the only
    # way to run the registry's own rules: `load_skill` reads from disk and checks
    # the folder name against the frontmatter name.
    with tempfile.TemporaryDirectory(prefix="cadence-skill-") as tmp:
        candidate = skill_target(tmp, name)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(text, encoding="utf-8")
        try:
            skill = load_skill(candidate, tiers)
        except SkillError as exc:
            # The validator's own words. `_label` prints "name/SKILL.md" rather than
            # the temp path, so the message is about the file, not about us.
            raise SkillWriteError(str(exc), "invalid") from None

    if skill.action not in DRAFTED_ACTIONS:
        raise SkillWriteError(
            "the file selects action: {0}. A skill written this way may only say "
            "things - action: prompt - because the student approves it from a preview "
            "of the text, not by reading frontmatter. A task with a side effect "
            "({1}) has to be hand-written, and this returns it to you so you can tell "
            "the student that.".format(skill.action, ", ".join(sorted(DRAFTED_ACTIONS))),
            "action",
        )
    return skill


def save(root, name, text, tiers=None):
    """Check, then write one skill file. -> (path, Skill).

    The check runs again here rather than trusting the one that produced the
    draft: the student answered a question that was asked minutes ago, and the
    library it is being added to may have changed since.
    """
    skill = check(root, name, text, tiers)
    target = skill_target(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written beside its final name and moved into place: `rglob("SKILL.md")` never
    # sees the half-file, so a crash here cannot become a registry error.
    staging = target.with_name(target.name + ".tmp")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, target)
    # The Skill from `check` describes the copy that was validated, in a directory
    # that no longer exists. Handing back its path would have a caller print a temp
    # folder as where the student's skill lives.
    return target, replace(skill, path=target)


def relative_path(name) -> str:
    """How a student refers to the file: relative to the repo, not to our root."""
    return "skills/{0}/SKILL.md".format(name)
