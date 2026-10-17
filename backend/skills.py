"""The skills registry: SKILL.md files in, validated Skill objects out.

Skills are **data, not code**. A skill declares its own trigger conditions, its
arguments, and the prompt it runs - so adding one means dropping in a Markdown
file, with no Python and no deploy. That is what makes Step 9 (the agent writing
its own skills after you confirm a task worked) possible without the agent
touching the backend.

Format: YAML frontmatter for the machine-readable routing fields, plus three
body sections the registry reads - `## When to Use`, `## Input` (a JSON schema
in a fenced block) and `## Prompt` (the template). Everything else in the body
is documentation for humans and for the agent that authored it.

Optional frontmatter beyond the routing fields: `tier` (which model runs it),
`action` (a handler from a reviewed allowlist, for skills with side effects),
`requires_confirmation`, `context` (which extra material the skill may be shown
- the invoked page text, the previous reply, or the student's memory), and
`verify_source_numbers` (true for a skill that rewrites the student's own words:
the reply's quoted rewrites are checked against the document, and a number in one
that the document does not contain is called out in the reply).

The frontmatter is deliberately Hermes-compatible: same file name, same `name`
and `description` keys, tags under `metadata.hermes.tags`. So a skill written
here stays readable by Hermes, and vice versa.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from frontmatter import FrontmatterError, parse

FIELD_TYPES = {"string", "integer", "number", "boolean"}

# What a skill can *do*, as opposed to what it can *say*.
#
# Most skills are pure prompts: render the template, call the tier, return the
# reply. A few need a side effect - `remember` writes to memory - and that is the
# one thing a Markdown file must not be able to do on its own, because a skill
# file can be authored by the model in Step 9. So the file *selects* a handler
# from this reviewed allowlist and can never supply code. An unknown value is a
# load error, not a silently ignored one.
SKILL_ACTIONS = {"prompt", "remember", "quiz"}

# Which actions are handed the declared context. Prompt skills always are; `quiz`
# is an action that is, because it writes its questions from the page or from
# memory. The list is explicit rather than "any action that asks" so that a skill
# file cannot claim to read the page while never being given it.
CONTEXT_ACTIONS = {"prompt", "quiz"}

# What a skill may ask to be shown, beyond the student's utterance.
#
# Declared rather than assumed, for the same reason as everything else here: a
# skill gets exactly what it says it needs and nothing more, so the page the
# student is reading cannot leak into a turn that never asked for it. Step 5's
# `explain`, `rephrase` and `plan` are the first skills to want any of it.
#
#   page     - the text of the page the student EXPLICITLY asked us to read.
#              Only ever attached when they invoked it; see `context_block` in
#              backend/server.py, which is where that promise is enforced.
#   previous - the assistant's own last reply, so "say that more simply" has a
#              referent.
#   memory   - the facts the student asked us to keep.
#   quoted   - a passage the student handed us in the request itself (they
#              quoted it, or the router extracted it). It travels as a labelled
#              data block rather than inside the instruction line, because a
#              passage interpolated into "restate this in simpler words: {text}"
#              is indistinguishable from the instruction when it is missing - the
#              model then restates the instruction. Data in data blocks.
CONTEXT_SOURCES = {"page", "previous", "memory", "quoted"}

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
_FENCE = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)```", re.S)


class SkillError(ValueError):
    """A skill file exists but cannot be loaded with confidence."""


def matches_type(value, ftype: str) -> bool:
    """Does `value` satisfy a declared field type?

    Shared with the router so skill arguments are type-checked in exactly one
    place, whether the value came from a schema default or from the model.
    """
    if isinstance(value, bool):
        # bool is a subclass of int in Python - catch it before the numeric
        # branches or `True` would pass as an integer.
        return ftype == "boolean"
    if ftype == "string":
        return isinstance(value, str)
    if ftype == "integer":
        return isinstance(value, int)
    if ftype == "number":
        return isinstance(value, (int, float))
    return False


def _label(path) -> str:
    """A useful name for error messages.

    `path.name` alone is worthless here: every skill file is called SKILL.md, so
    an error reading "SKILL.md: missing ## Prompt" tells you nothing when there
    are many skills. Include the containing folder, which is the skill's name.
    """
    path = Path(path)
    return f"{path.parent.name}/{path.name}"


@dataclass(frozen=True)
class SkillField:
    name: str
    type: str
    required: bool
    description: str
    has_default: bool = False
    default: object = None
    # A closed vocabulary, when the argument is a choice rather than free text.
    # This exists because a field *description* is never sent to the router - the
    # routing card carries only the shape - so prose like "prefer one of:
    # deadlines, classes, ..." was invisible to the model that had to comply with
    # it, and it invented a new topic on every utterance instead. A declared enum
    # reaches the router, and the router's validator enforces it.
    choices: tuple = ()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when_to_use: str
    not_for: str
    tier: str
    requires_confirmation: bool
    action: str
    tools: List[str]
    tags: List[str]
    context: tuple
    input_schema: Dict[str, SkillField]
    prompt_template: str
    body: str
    path: Path
    # Ask the server to check the reply's quoted rewrites against the document they
    # came from, and to say so when a number in one of them is not in the source.
    # Off by default: only a skill that rewrites the student's own words needs it.
    verify_source_numbers: bool = False
    schema_warnings: List[str] = field(default_factory=list)

    @property
    def required_fields(self) -> List[str]:
        return [f.name for f in self.input_schema.values() if f.required]

    def routing_card(self) -> dict:
        """The compact form handed to the router.

        Kept deliberately small: this goes into the prompt on every single
        utterance, so every word here is paid for on every request.
        """
        args = {}
        for name, f in self.input_schema.items():
            # A closed vocabulary is rendered inline so the router can see its
            # options without the card carrying every field description.
            shape = (
                "one of: " + " | ".join(str(c) for c in f.choices)
                if f.choices
                else f.type
            )
            args[name] = f"{shape}{' (required)' if f.required else ''}"
        card = {"name": self.name, "when": self.when_to_use}
        # The negative case matters as much as the positive one. Without it the
        # router over-applies whatever skills happen to exist - a single skill
        # in the library becomes the answer to everything.
        if self.not_for:
            card["not_for"] = self.not_for
        # Which skills can work from the page is a routing signal, so it goes in
        # the card - and only when a skill declares it, keeping the card (paid
        # for on every request) as small as it can be.
        if self.context:
            card["context"] = list(self.context)
        card["args"] = args
        return card

    def resolve_args(self, args: dict) -> dict:
        """Apply schema defaults, then overlay what the router supplied."""
        resolved = {
            f.name: f.default for f in self.input_schema.values() if f.has_default
        }
        resolved.update({k: v for k, v in (args or {}).items() if v is not None})
        return resolved

    def render_prompt(self, args: dict) -> str:
        """Fill the prompt template. Unknown or missing optionals don't crash."""

        class _Defaults(dict):
            def __missing__(self, key):
                return "(not specified)"

        values = _Defaults(self.resolve_args(args))
        try:
            return self.prompt_template.format_map(values).strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise SkillError(
                f"{_label(self.path)}: prompt template has an unusable placeholder "
                f"({type(exc).__name__}: {exc})"
            ) from None


def _section(body: str, title: str) -> str:
    """Return the text under a '## title' heading, up to the next heading."""
    wanted = title.strip().lower()
    for match in _HEADING.finditer(body):
        if match.group(2).strip().lower() != wanted:
            continue
        level = len(match.group(1))
        for nxt in _HEADING.finditer(body, match.end()):
            if len(nxt.group(1)) <= level:
                return body[match.end():nxt.start()]
        return body[match.end():]
    return ""


def _first_fence(text: str):
    match = _FENCE.search(text)
    if not match:
        return None, None
    return match.group(1).lower(), match.group(2)


def _parse_schema(raw: str, path: Path) -> Dict[str, SkillField]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError(f"{_label(path)}: ## Input block is not valid JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise SkillError(f"{_label(path)}: ## Input must be a JSON object of fields")

    fields: Dict[str, SkillField] = {}
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise SkillError(f"{_label(path)}: input field {name!r} must be an object")
        ftype = str(spec.get("type", "string")).lower()
        if ftype not in FIELD_TYPES:
            raise SkillError(
                f"{_label(path)}: input field {name!r} has unsupported type {ftype!r} "
                f"(allowed: {sorted(FIELD_TYPES)})"
            )
        has_default = "default" in spec
        default = spec.get("default")
        required = bool(spec.get("required", False))

        choices = spec.get("enum")
        if choices is not None:
            if not isinstance(choices, list) or not choices:
                raise SkillError(
                    f"{_label(path)}: input field {name!r} has an 'enum' that is not a "
                    "non-empty list"
                )
            for choice in choices:
                if not matches_type(choice, ftype):
                    raise SkillError(
                        f"{_label(path)}: input field {name!r} has enum value {choice!r} "
                        f"which is not a {ftype}"
                    )
            if has_default and default not in choices:
                raise SkillError(
                    f"{_label(path)}: input field {name!r} defaults to {default!r}, "
                    f"which is not in its enum {choices}"
                )
        else:
            choices = ()

        if has_default and required:
            raise SkillError(
                f"{_label(path)}: input field {name!r} is marked required but also has a "
                "default - those contradict each other"
            )
        if has_default and not matches_type(default, ftype):
            raise SkillError(
                f"{_label(path)}: input field {name!r} has a default that is not a "
                f"{ftype}: {default!r}"
            )

        fields[name] = SkillField(
            name=name,
            type=ftype,
            required=required,
            description=str(spec.get("description", "")),
            has_default=has_default,
            default=default,
            choices=tuple(choices),
        )
    return fields


def load_skill(path: Path, tiers: Optional[List[str]] = None) -> Skill:
    """Load and fully validate one SKILL.md."""
    try:
        meta, body = parse(path.read_text(encoding="utf-8"))
    except FrontmatterError as exc:
        raise SkillError(f"{_label(path)}: {exc}") from None

    def need(key):
        value = meta.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SkillError(f"{_label(path)}: frontmatter is missing required key {key!r}")
        return value

    name = str(need("name")).strip()
    if path.parent.name != name:
        raise SkillError(
            f"{_label(path)}: folder is {path.parent.name!r} but frontmatter name is "
            f"{name!r} - they must match, or the registry and the filesystem drift"
        )

    tier = str(meta.get("tier") or "super").strip()
    if tiers and tier not in tiers:
        raise SkillError(
            f"{_label(path)}: tier {tier!r} is not a configured tier ({sorted(tiers)})"
        )

    confirmation = meta.get("requires_confirmation", False)
    if not isinstance(confirmation, bool):
        raise SkillError(
            f"{_label(path)}: requires_confirmation must be true or false, "
            f"got {confirmation!r}"
        )

    verify_numbers = meta.get("verify_source_numbers", False)
    if not isinstance(verify_numbers, bool):
        raise SkillError(
            f"{_label(path)}: verify_source_numbers must be true or false, "
            f"got {verify_numbers!r}"
        )

    action = str(meta.get("action") or "prompt").strip().lower()
    if action not in SKILL_ACTIONS:
        raise SkillError(
            f"{_label(path)}: action {action!r} is not one of {sorted(SKILL_ACTIONS)}"
        )

    tools = meta.get("tools") or []
    if not isinstance(tools, list):
        raise SkillError(f"{_label(path)}: tools must be a list, got {type(tools).__name__}")

    hermes = (meta.get("metadata") or {}).get("hermes") or {}
    tags = hermes.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]

    # Declared context sources. A typo here would silently mean "this skill gets
    # nothing", which looks identical to a skill that works, so it is a load
    # error instead.
    declared_context = meta.get("context") or []
    if isinstance(declared_context, str):
        declared_context = [declared_context]
    if not isinstance(declared_context, list):
        raise SkillError(
            f"{_label(path)}: context must be a list, got "
            f"{type(declared_context).__name__}"
        )
    context: List[str] = []
    for source in declared_context:
        # Named `source_name`, not `name`: this function already has a `name`
        # (the skill's), and shadowing it here silently renamed every skill after
        # its first context source. The registry then reported a duplicate
        # called "page", which is the kind of bug a test catches once and a
        # reader never forgets.
        source_name = str(source).strip().lower()
        if source_name not in CONTEXT_SOURCES:
            raise SkillError(
                f"{_label(path)}: context source {source_name!r} is not one of "
                f"{sorted(CONTEXT_SOURCES)}"
            )
        if source_name not in context:
            context.append(source_name)
    if context and action not in CONTEXT_ACTIONS:
        # Only the actions in CONTEXT_ACTIONS are handed context. One declaring it
        # would be a silent no-op, and a file that looks like it reads the page
        # while never reading it is worse than one that refuses to load.
        raise SkillError(
            f"{_label(path)}: declares context {context} but its action is "
            f"{action!r}, which is not given context (only {sorted(CONTEXT_ACTIONS)} are)"
        )

    # A prompt is required only when the skill is actually a prompt. Demanding
    # one from a skill whose action is implemented in code would force it to
    # carry a template nobody ever renders - a lie sitting in the file.
    prompt = ""
    if action == "prompt":
        prompt_text = _section(body, "Prompt")
        lang, prompt = _first_fence(prompt_text)
        if prompt is None or not prompt.strip():
            raise SkillError(
                f"{_label(path)}: needs a '## Prompt' section containing a fenced code block"
            )

    schema_text = _section(body, "Input")
    _, schema_block = _first_fence(schema_text)
    if schema_block is None:
        raise SkillError(
            f"{_label(path)}: needs a '## Input' section with a fenced JSON block "
            "(use an empty {} for a skill that takes no arguments)"
        )

    return Skill(
        name=name,
        description=str(need("description")).strip(),
        when_to_use=str(need("when_to_use")).strip(),
        not_for=str(meta.get("not_for") or "").strip(),
        tier=tier,
        requires_confirmation=confirmation,
        action=action,
        tools=[str(t) for t in tools],
        tags=[str(t) for t in tags],
        context=tuple(context),
        verify_source_numbers=verify_numbers,
        input_schema=_parse_schema(schema_block, path),
        prompt_template=prompt,
        body=body,
        path=path,
    )


class SkillRegistry:
    """All loadable skills, keyed by name."""

    def __init__(self, skills_dir: Path, tiers: Optional[List[str]] = None):
        self.skills_dir = Path(skills_dir)
        self.tiers = tiers
        self.skills: Dict[str, Skill] = {}
        self.errors: List[str] = []

    def load(self) -> "SkillRegistry":
        """Load every SKILL.md. One bad file does not take down the rest."""
        self.skills, self.errors = {}, []
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                skill = load_skill(path, self.tiers)
            except SkillError as exc:
                # Surface the problem, keep serving. A broken skill must not
                # stop the student from talking to the assistant.
                self.errors.append(str(exc))
                continue
            if skill.name in self.skills:
                self.errors.append(
                    f"duplicate skill name {skill.name!r}: {path} "
                    f"(already loaded from {self.skills[skill.name].path})"
                )
                continue
            self.skills[skill.name] = skill
        return self

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def names(self) -> List[str]:
        return sorted(self.skills)

    def routing_cards(self) -> List[dict]:
        return [self.skills[n].routing_card() for n in self.names()]

    def describe(self) -> List[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "when_to_use": s.when_to_use,
                "not_for": s.not_for,
                "tier": s.tier,
                "requires_confirmation": s.requires_confirmation,
                "action": s.action,
                "args": {
                    f.name: {
                        "type": f.type,
                        "required": f.required,
                        "description": f.description,
                        **({"enum": list(f.choices)} if f.choices else {}),
                    }
                    for f in s.input_schema.values()
                },
                "tools": s.tools,
                "tags": s.tags,
                "context": list(s.context),
                "path": str(s.path),
            }
            for s in (self.skills[n] for n in self.names())
        ]
