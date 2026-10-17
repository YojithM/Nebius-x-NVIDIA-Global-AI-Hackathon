"""The router: one utterance in, one skill decision out.

Contract (from the spec, unchanged):

    {"skill": "<name or 'general'>", "args": {...}, "needs_confirmation": bool}

Three design rules here, and each one exists because the alternative fails in a
way a student would notice:

1. **Validate, never trust.** The model's JSON is checked against the registry
   before anything runs. A skill name that isn't in the library is rejected, and
   so is an argument the skill never declared - the spec's "never invent skill
   names or arguments outside the schema", enforced in code rather than hoped for.

2. **One retry, then fall back to general.** A model that emits a stray character
   should cost the student a few hundred milliseconds, not an error message. The
   retry tells it exactly what was wrong; if the second attempt is also bad we
   route to `general` and answer anyway.

3. **The confirmation flag is enforced server-side.** `needs_confirmation` is
   ORed with the skill's own `requires_confirmation`, so the model can escalate a
   Tier-2 action but can never silently de-escalate one. Safety flags that live
   only in a prompt are not safety flags.
"""

import json
from dataclasses import dataclass
from typing import Optional, Tuple

from nemotron import cost_usd, reply_text
from skills import Skill, SkillRegistry, matches_type

ROUTER_TIER = "nano"
ROUTER_MAX_TOKENS = 400
ROUTER_TEMPERATURE = 0  # routing is a classification task; we want the same answer twice

ROUTER_SYSTEM = """You are a router. Choose exactly ONE way to handle a student's spoken request.

Reply with ONLY a JSON object. No prose, no explanation, no code fences.

{"skill": "<name>", "args": {}, "needs_confirmation": false}

"general" is a real answer, not a failure. Choose it unless the request clearly matches a skill's "when" description. Choose "general" for:
- a question with a factual answer ("what is a ketone", "define entropy")
- anything no skill covers
- small talk, or a request that is ambiguous
- a request to do work the student should do themselves

The "general" path can search the web and do arithmetic when it decides a tool is worth a step, so send anything that leans on a current fact or a calculation there rather than guessing at it yourself. You have no tools: never say you looked something up.

The "general" path can also draft a new skill file for our own library, and no skill can - you cannot, and neither can the skill whose task it describes. So a request to create, write, draft, add or "save" a NEW skill is ALWAYS "general", even when it describes work a skill already covers: "make yourself a skill for lab reports" is not a request about a lab report, and answering it with a study plan is answering a question nobody asked. The skills listed below already exist; a request that names a new one is general.

A request for something that changes over time - a version, a price, a date, a score, a schedule, a current release - is ALWAYS "general", even when a skill's description sounds close. Only "general" can look it up, so routing it to a skill means answering it from memory.

A question that asks only *when* something already on file is due - "when is my next deadline", "what's due this week", "how long until the midterm" - is a LOOKUP, not a plan. The dates are in the memory field, so "general" can read them out. Never send one to a skill that builds a schedule: a plan allocates a span of time, and a date question contains no span to allocate. If the student also says how much time they have and asks for the work to be fitted into it ("plan my week, I have six hours"), that IS a plan, and the plan skill is right.

Rules:
1. "skill" must be exactly one of the skill names provided, or "general".
2. Never invent a skill name. Never invent an argument name. Use only the arguments the chosen skill declares.
3. Include every required argument. Include an optional argument only when the student clearly supplied it, and never fill one with the request itself ("rephrase this paragraph" is not the passage to rephrase).
4. "needs_confirmation" must be a boolean. Set it true only when the request would change the student's tabs, files, or schedule. Most requests do not.
5. If a skill lists "not_for" and the request matches it, do not choose that skill.
6. If you are not confident a skill matches, choose "general". Picking a skill that does not fit is worse than picking none.

A "memory" field may be present: things this student asked you to remember. Use it to resolve what they mean ("when is it?" after a midterm was mentioned), but it is NOT a reason to pick a skill. If they ask you to remember something NEW, that is the remember skill.

A "context" field on a skill card lists extra material that skill can work from: "page" (the text of the page they are looking at), "previous" (your own last reply), "memory". You are never shown page text, so never say you read the page. When the input includes "page_read", the student asked us to read the page for this request, so prefer a skill that declares "page" over one that does not."""


@dataclass
class Decision:
    skill: str
    args: dict
    needs_confirmation: bool
    source: str  # "model" | "fallback"
    attempts: int
    tier: str
    model: str
    latency_ms: int
    cost_usd: float
    raw: str = ""
    validation_error: str = ""


class RouteError(ValueError):
    """Raised only for programmer error, never for a bad model response."""


def read_reply(response) -> Tuple[str, Optional[str]]:
    """Return (content, truncation_note).

    Distinguishing "the model was cut off" from "the model wrote nonsense"
    matters: they need opposite retries. A cut-off reply needs a bigger budget,
    not a stronger telling-off.
    """
    choices = response.get("choices") or []
    finish_reason = choices[0].get("finish_reason") if choices else None
    content = reply_text(response)
    if not content and finish_reason == "length":
        return "", "the model ran out of tokens before producing JSON"
    return content, None


def build_user_message(
    transcript: str,
    page_title: Optional[str],
    registry: SkillRegistry,
    memory: str = "",
    page_read: bool = False,
) -> str:
    """The router's input: the utterance, the page, the library, and what we know.

    Memory is here because it changes what a sentence *means*. "Tell me more
    about that" is unroutable on its own and obvious once we know the student is
    looking at their chemistry syllabus and has a midterm on the 12th.

    `page_read` is a flag, never the text: the router decides *who* handles the
    request, and the page itself goes to the skill, which is the only place it is
    needed. Sending a whole page through the router would pay for those tokens
    twice and put untrusted text in front of the component that must stay
    predictable.
    """
    payload = {
        "transcript": transcript,
        "page_title": page_title or None,
        "skills": registry.routing_cards(),
    }
    if memory:
        payload["memory"] = memory
    if page_read:
        payload["page_read"] = True
    return json.dumps(payload, ensure_ascii=False)


def extract_json_object(text: str) -> Optional[dict]:
    """Pull a JSON object out of a model reply.

    Handles the two things models actually do: wrap the object in a code fence,
    or put a sentence around it. Scanning for balanced braces (rather than
    regexing to the last '}') keeps a trailing explanation from corrupting the
    parse, and it respects braces inside strings - including escaped quotes.
    """
    if not text:
        return None

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else ""
        if "```" in candidate:
            candidate = candidate[: candidate.index("```")]
        candidate = candidate.strip()

    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def validate_decision(payload: dict, registry: SkillRegistry) -> Tuple[Optional[dict], Optional[str]]:
    """Check a routed payload against the library. Returns (clean, error)."""
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"

    if "skill" not in payload:
        return None, "missing required key 'skill'"

    skill_name = payload["skill"]
    if not isinstance(skill_name, str) or not skill_name.strip():
        return None, "'skill' must be a non-empty string"
    skill_name = skill_name.strip()

    known = registry.names()
    if skill_name != "general" and skill_name not in known:
        return None, (
            f"unknown skill {skill_name!r}; it is not in the library "
            f"(available: {', '.join(known) if known else 'none'})"
        )

    args = payload.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "'args' must be an object"

    confirmation = payload.get("needs_confirmation", False)
    if not isinstance(confirmation, bool):
        return None, (
            f"'needs_confirmation' must be true or false, not {confirmation!r} "
            "(do not send the strings \"true\"/\"false\")"
        )

    if skill_name == "general":
        # The general path takes free-form context, but only flat scalars, so a
        # runaway model cannot smuggle a nested structure into our logs.
        for key, value in args.items():
            if not isinstance(value, (str, int, float, bool)):
                return None, f"general args may only contain simple values, got {key!r}"
        clean_args = args
    else:
        skill: Skill = registry.get(skill_name)
        declared = skill.input_schema
        unknown = sorted(set(args) - set(declared))
        if unknown:
            return None, (
                f"skill {skill_name!r} has no argument(s): {', '.join(unknown)} "
                f"(it accepts: {', '.join(sorted(declared)) or 'no arguments'})"
            )
        for name, field_spec in declared.items():
            if field_spec.required and name not in args:
                return None, f"skill {skill_name!r} requires argument {name!r}"
            if name in args and not matches_type(args[name], field_spec.type):
                return None, (
                    f"argument {name!r} should be a {field_spec.type}, "
                    f"got {type(args[name]).__name__}"
                )
            # A closed vocabulary is enforced, not requested. The router has the
            # enum in front of it, so a value outside it is a mistake we can name
            # - and naming it is what makes the corrective retry work.
            if name in args and field_spec.choices and args[name] not in field_spec.choices:
                return None, (
                    f"argument {name!r} must be one of "
                    f"{', '.join(str(c) for c in field_spec.choices)}, got {args[name]!r}"
                )
        clean_args = args

    return (
        {
            "skill": skill_name,
            "args": clean_args,
            "needs_confirmation": confirmation,
        },
        None,
    )


def route(
    client,
    config: dict,
    registry: SkillRegistry,
    transcript: str,
    page_title: Optional[str] = None,
    memory: str = "",
    page_read: bool = False,
) -> Decision:
    """Route one utterance. Always returns a Decision - never raises for bad JSON."""
    tier_cfg = config["tiers"][ROUTER_TIER]
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM},
        {
            "role": "user",
            "content": build_user_message(
                transcript, page_title, registry, memory, page_read
            ),
        },
    ]

    total_cost = 0.0
    total_latency = 0
    attempts = 0
    last_error = ""
    last_raw = ""

    for attempt in (1, 2):
        attempts = attempt
        # The retry gets a bigger budget. The most common first-attempt failure
        # is the model spending its entire allowance on reasoning instead of
        # emitting JSON, and retrying at the same size just repeats the failure.
        budget = ROUTER_MAX_TOKENS if attempt == 1 else ROUTER_MAX_TOKENS * 3
        response, latency = client.chat(
            ROUTER_TIER,
            messages,
            temperature=ROUTER_TEMPERATURE,
            max_tokens=budget,
        )
        total_latency += int(latency * 1000)
        usage = response.get("usage") or {}
        total_cost += cost_usd(config, ROUTER_TIER, usage)

        raw, truncated = read_reply(response)
        last_raw = raw
        if truncated:
            last_error = truncated
        else:
            payload = extract_json_object(raw)
            if payload is None:
                last_error = "the reply was not a JSON object"
            else:
                clean, error = validate_decision(payload, registry)
                if error is None:
                    return _finish(
                        clean, registry, "model", attempts, total_latency,
                        total_cost, raw, "", tier_cfg["model"],
                    )
                last_error = error

        # Corrective retry: tell it precisely what was wrong.
        messages.append({"role": "assistant", "content": raw or "(empty reply)"})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That reply was invalid: {last_error}. "
                    "Reply again with ONLY a valid JSON object matching the schema."
                ),
            }
        )

    # Both attempts failed. Answer the student anyway.
    return _finish(
        {"skill": "general", "args": {"request": transcript}, "needs_confirmation": False},
        registry,
        "fallback",
        attempts,
        total_latency,
        total_cost,
        last_raw,
        last_error,
        tier_cfg["model"],
    )


def _finish(
    clean: dict,
    registry: SkillRegistry,
    source: str,
    attempts: int,
    latency_ms: int,
    cost: float,
    raw: str,
    error: str,
    model_id: str,
) -> Decision:
    skill_name = clean["skill"]

    # Who decides whether confirmation is needed?
    #
    # For a named skill: the skill file, and only the skill file. It is reviewed,
    # versioned, and describes what the skill actually does. The model's opinion is
    # discarded here on purpose - it over-flags harmless requests, and a phantom
    # "shall I go ahead?" on a safe action trains the student to click yes without
    # reading, which is exactly how a real confirmation prompt stops working.
    #
    # For "general": the model's flag, because there is no skill declaration to
    # consult and the request may well be something like "email my professor".
    if skill_name == "general":
        needs_confirmation = bool(clean["needs_confirmation"])
    else:
        skill = registry.get(skill_name)
        # A skill we cannot find is treated as needing confirmation: fail closed.
        needs_confirmation = bool(skill.requires_confirmation) if skill else True

    return Decision(
        skill=skill_name,
        args=clean["args"],
        needs_confirmation=needs_confirmation,
        source=source,
        attempts=attempts,
        tier=ROUTER_TIER,
        model=model_id,
        latency_ms=latency_ms,
        cost_usd=cost,
        raw=raw,
        validation_error=error,
    )
