"""The agent's tools. Three of them, and not one can write anything.

Step 9 gives the general path a plan instead of a single shot, and a plan needs
tools to act with. The spec said "search, sandboxed code". This module ships
search (web_search) and arithmetic (calculate), and the second one is deliberately
*not* a code sandbox. That is the main design decision in this file, so here is
the reasoning rather than just the result:

- **We are not the sandbox.** Running model-authored Python safely needs real
  isolation - seccomp, a container, no filesystem, no network - and none of that
  is available in a stdlib-only process we ask students to run on their own
  laptop. A subprocess with a timeout is not a sandbox; it is a way to run code
  and call it a sandbox.
- **The loop reads untrusted text on purpose.** Web results and page text are
  written by strangers. The moment a tool can execute a string that a model
  composed after reading that text, "ignore your instructions" in a search
  snippet becomes code execution on the student's machine. We would be building
  the injection path ourselves.
- **The useful case is arithmetic.** A study assistant needs percentages, date
  gaps, and grade averages. `ast`-walked evaluation of a whitelisted expression
  grammar does all of that, cannot import or call anything we did not list, and
  is short enough to review in one sitting.

So `calculate` is a whitelist evaluator, and the honest name for it is that.
Anything that genuinely needs a program - a plot, a simulation - is a Step 10
question, and it needs a real sandbox to answer.

The same discipline as the skills registry applies to the tool set itself: the
cards the model is shown (`tool_cards`) are generated from the handlers below, so
a tool cannot be advertised in the prompt without existing in the code, and the
model's arguments are validated before anything runs.

The third tool, `propose_skill`, arrived in Step 11 with the agent's own skill
writing. It is read-only for the same reason the other two are, and the same
reason is worth stating in a new place: what the loop may do is *ask*. The file it
drafts is checked against the registry's real rules and handed back with the
result; the write happens in the server, behind the approval gate, and only for a
file the student has read. See `backend/skillwriter.py` for what a file written
this way is allowed to be - one thing it is not allowed to be is a handler that
writes, so a drafted skill can say things and nothing else.

One thing here is not about the model at all. `web_search` returns `sources`
alongside its text: the pages it actually read, in the order they will be cited.
That list is the *only* route by which a URL can reach the panel, so a URL the
model composes in its reply stays prose rather than becoming a link - see
`backend/agent.py`. A search provider is remote input like any other, so every
URL is checked for an http(s) scheme here, at the edge, rather than in a
renderer that might forget.
"""

import ast
import json
import math
import operator
import os
from datetime import date, timedelta
import urllib.error
import urllib.request
from pathlib import Path

from nemotron import load_config
from skillwriter import SkillWriteError, check as check_skill

# Where the skill library lives, for the one tool that drafts a file into it. The
# server owns the live registry and re-checks every draft against *its* directory
# and its own configured tiers - this is the default for the tool used alone, and
# the two are the same path in a normal process.
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# How many results we ask for, and how much of each one we keep. Small on
# purpose: every result becomes prompt tokens on the next step of the loop, and
# the loop may run several steps.
MAX_RESULTS = 3
MAX_SNIPPET_CHARS = 300
MAX_TITLE_CHARS = 120
# Big enough for three whole result blocks. It used to be 1200, which cut the
# third one in half - fine when this text was only read, and not fine now that
# the model may cite what it contains: a block sliced mid-sentence would get a
# citation number without giving the model anything to check.
MAX_RESULT_TEXT_CHARS = 2400
MAX_QUERY_CHARS = 300
SEARCH_TIMEOUT = 15

MAX_EXPRESSION_CHARS = 300
MAX_EXPRESSION_DEPTH = 24
MAX_LIST_ITEMS = 500
# `9**9**9` is a valid expression and a way to make a laptop stop responding.
MAX_POWER = 1000


class ToolError(RuntimeError):
    """A tool could not do its job.

    The message is fed back to the model, so it says what went wrong *and* what
    the alternative is. A tool that fails silently, or that returns an empty
    string, is how a model ends up inventing an answer to cover for it.

    `code` is a coarse bucket for the action log - "no_key", "rejected_key",
    "unreachable". The log never records the message or the student's query,
    because a provider's error text can quote the query back at us, and the
    student's own words are the one thing that never goes in the log.
    """

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ search ---
#
# Two providers, and which one is live is decided by which key is in the
# environment - never by a default we cannot honour. Both request shapes below
# were read from the providers' own API documentation rather than remembered,
# because a guessed request shape fails as a 4xx on the student's first real
# question, which is the worst possible time to find out.
#
# Keys are read from the environment at call time. Nothing here is imported into
# a prompt, sent to the model, or written to the action log.

PROVIDERS = {
    "exa": {
        "env": "EXA_API_KEY",
        "url": "https://api.exa.ai/search",
        "docs": "https://exa.ai/docs/reference/search-api-guide",
        "call_sign": "requests are sent to api.exa.ai",
    },
    "tavily": {
        "env": "TAVILY_API_KEY",
        "url": "https://api.tavily.com/search",
        "docs": "https://docs.tavily.com/documentation/api-reference/endpoint/search",
        "call_sign": "requests are sent to api.tavily.com",
    },
}
# Order matters: the first configured provider wins.
PROVIDER_ORDER = ("exa", "tavily")


def _post_json(url, headers, payload, timeout, opener):
    """POST JSON and return (status, body). Network problems raise ToolError.

    `opener` replaces the network - tests pass a stand-in - and it is inside the
    same guard as urllib on purpose: a seam that fails differently from the thing
    it stands in for is a seam that hides bugs. Whatever raises, the caller gets a
    ToolError rather than a broken turn.
    """
    try:
        if opener is not None:
            return opener(url, headers, payload, timeout)
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # An HTTP status is the provider answering, so it is data, not a crash.
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except urllib.error.URLError as exc:
        raise ToolError(
            "could not reach {0}: {1}".format(url, exc.reason), "unreachable"
        ) from None
    except OSError as exc:
        # A reset or a timeout mid-read is not a URLError.
        raise ToolError(
            "could not reach {0}: {1}".format(url, exc), "unreachable"
        ) from None


def _http_message(provider, status, body):
    """Turn a non-200 into (a sentence the model can act on, a log code)."""
    label = PROVIDERS[provider]["env"]
    if status in (401, 403):
        return (
            "the search provider rejected the key in {0} (HTTP {1}). "
            "The key is wrong, expired, or out of credit - tell the student to "
            "check it rather than guessing at an answer.".format(label, status),
            "rejected_key",
        )
    if status == 429:
        return (
            "the search provider is rate limiting us (HTTP 429); try again later.",
            "rate_limited",
        )
    detail = (body or "").strip().replace("\n", " ")[:200]
    return (
        "the search provider returned HTTP {0}: {1}".format(status, detail),
        "provider_error",
    )


def search_provider():
    """Which provider this process can use, or None. Never raises."""
    for name in PROVIDER_ORDER:
        if (os.environ.get(PROVIDERS[name]["env"]) or "").strip():
            return name
    return None


def search_available():
    return search_provider() is not None


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _web_url(value):
    """A URL we are willing to show as a link, or "".

    Only http(s) gets through. A provider is remote input like any other, and
    `javascript:` in an href is a thing that has really happened to real
    products - so the check lives at the boundary instead of in each renderer.
    """
    url = (value or "").strip()
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return ""


def _search_text(results, budget=MAX_RESULT_TEXT_CHARS):
    """Render search results as text, and say how many of them fitted.

    Two details here are deliberate, and both were wrong once.

    **Whole blocks, never a mid-block cut.** The model may cite only a page whose
    text it was actually given, and this function's return value decides
    `sources` - so a block sliced in half is a page that gets a number without
    ever showing the model what it says. Blocks are admitted whole, and whatever
    did not fit is reported as omitted rather than quietly dropped.

    **No "1." "2." numbering.** The *loop* numbers citations, because it is the
    only layer that can number the same list the same way in the prompt and in the
    reply. When this tool numbered its results as well, a turn that searched twice
    had two independent 1s in it, and "[2]" meant two different pages depending on
    which list the reader had in mind.
    """
    blocks = []
    for result in results:
        block = "{0} - {1}".format(result["title"], result["url"] or "(no url)")
        if result["snippet"]:
            block += "\n   " + result["snippet"]
        blocks.append(block)

    lines = []
    for block in blocks:
        # The first block is kept however long it is: an empty result reads as
        # "the search found nothing", which is a different fact entirely.
        if lines and len("\n".join(lines + [block])) > budget:
            break
        lines.append(block)
    kept = len(lines)

    text = "\n".join(lines)
    if kept < len(blocks):
        text += "\n... ({0} more result(s) omitted to stay inside the prompt budget)".format(
            len(blocks) - kept
        )
    return text, kept


def _sources(results, kept):
    """The subset of a search that may be cited: http(s), and inside the text."""
    sources = []
    for result in results[:kept]:
        url = _web_url(result["url"])
        if url:
            sources.append({"title": result["title"], "url": url})
    return sources


def _exa(query, key, opener, timeout, max_results):
    status, body = _post_json(
        PROVIDERS["exa"]["url"],
        {"x-api-key": key},
        {"query": query, "numResults": max_results, "text": True},
        timeout,
        opener,
    )
    if status != 200:
        message, code = _http_message("exa", status, body)
        raise ToolError(message, code)
    try:
        payload = json.loads(body)
    except ValueError:
        raise ToolError(
            "the search provider returned a response we could not read.", "bad_response"
        ) from None
    results = []
    for item in (payload.get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": _clip(item.get("title") or "untitled", MAX_TITLE_CHARS),
                "url": _clip(item.get("url") or "", 300),
                "snippet": _clip(item.get("text") or item.get("summary") or "", MAX_SNIPPET_CHARS),
            }
        )
    return results


def _tavily(query, key, opener, timeout, max_results):
    status, body = _post_json(
        PROVIDERS["tavily"]["url"],
        {"Authorization": "Bearer " + key},
        {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            # We want snippets, not whole pages: raw content would blow the
            # prompt budget on the very first search.
            "include_raw_content": False,
            "include_answer": False,
        },
        timeout,
        opener,
    )
    if status != 200:
        message, code = _http_message("tavily", status, body)
        raise ToolError(message, code)
    try:
        payload = json.loads(body)
    except ValueError:
        raise ToolError(
            "the search provider returned a response we could not read.", "bad_response"
        ) from None
    results = []
    for item in (payload.get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": _clip(item.get("title") or "untitled", MAX_TITLE_CHARS),
                "url": _clip(item.get("url") or "", 300),
                "snippet": _clip(item.get("content") or "", MAX_SNIPPET_CHARS),
            }
        )
    return results


def web_search(query, *, api_key=None, provider=None, opener=None, timeout=SEARCH_TIMEOUT,
               max_results=MAX_RESULTS):
    """Search the web. -> {"provider", "query", "results", "text"}.

    Raises ToolError with an actionable message on any failure, including having
    no key at all - which is the common case and must not read like a bug in the
    student's setup.
    """
    query = " ".join((query or "").split())
    if not query:
        raise ToolError("web_search needs a non-empty `query` string.", "bad_request")
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS].rstrip()

    provider = provider or search_provider()
    if provider is None:
        raise ToolError(
            "web_search is not configured: no search API key is set. Set "
            + " or ".join(
                "{0} (from {1})".format(PROVIDERS[n]["env"], PROVIDERS[n]["docs"])
                for n in PROVIDER_ORDER
            )
            + " in .env and restart the backend. Do not fill the gap from memory: "
            "tell the student you could not check this, and stop there.",
            "no_key",
        )
    if provider not in PROVIDERS:
        raise ToolError("unknown search provider {0!r}.".format(provider), "bad_provider")

    key = (api_key or os.environ.get(PROVIDERS[provider]["env"]) or "").strip()
    if not key:
        raise ToolError(
            "{0} is empty, so web_search cannot run.".format(PROVIDERS[provider]["env"]),
            "no_key",
        )

    try:
        fetch = {"exa": _exa, "tavily": _tavily}[provider]
        results = fetch(query, key, opener, timeout, max(1, min(int(max_results), 10)))
    except ValueError:
        raise ToolError(
            "web_search needs a whole number of results.", "bad_request"
        ) from None
    if not results:
        return {
            "provider": provider,
            "query": query,
            "results": [],
            "sources": [],
            "kept": 0,
            "text": "no results for that query.",
        }
    text, kept = _search_text(results)
    return {
        "provider": provider,
        "query": query,
        "results": results,
        # What the model is allowed to cite, in the order it will be numbered.
        # Nothing else ever becomes a link in the panel.
        "sources": _sources(results, kept),
        "kept": kept,
        "text": text,
    }


# ----------------------------------------------------------------- calculate --
#
# A whitelist evaluator, not eval(): the AST is walked and every node has to be
# an arithmetic operation, a number, a whitelisted function, or a list of those.
# There is no name lookup, so `os`, `__import__` and attribute access are not
# "blocked", they are simply not part of the grammar.

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}
_ALLOWED_FUNCS = {
    "abs": abs,
    "ceil": math.ceil,
    "cos": math.cos,
    "degrees": math.degrees,
    "exp": math.exp,
    "floor": math.floor,
    "len": len,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "max": max,
    "mean": lambda *values: _mean(values),
    "min": min,
    "radians": math.radians,
    "round": round,
    "sin": math.sin,
    "sqrt": math.sqrt,
    "sum": sum,
    "tan": math.tan,
}


def _mean(values):
    numbers = _flatten(values)
    if not numbers:
        raise ToolError("mean() needs at least one number.")
    return sum(numbers) / float(len(numbers))


def _flatten(values):
    """One level of nesting, which is what `mean([1, 2, 3])` and `mean(1, 2)` need."""
    items = values[0] if len(values) == 1 and isinstance(values[0], list) else values
    numbers = []
    for value in items:
        if isinstance(value, list):
            numbers.extend(v for v in value if not isinstance(v, list))
        else:
            numbers.append(value)
    return numbers


def _power(base, exponent):
    # `9**9**9` is the one expression here that can wedge a machine instead of
    # raising, so the exponent is bounded rather than trusted.
    if isinstance(exponent, (int, float)) and abs(exponent) > MAX_POWER:
        raise ToolError(
            "exponent {0} is larger than this tool allows ({1}).".format(exponent, MAX_POWER)
        )
    return base ** exponent


def _eval(node, depth=0):
    if depth > MAX_EXPRESSION_DEPTH:
        raise ToolError("that expression nests too deeply.")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolError("only numbers are allowed in an expression.")
        return node.value
    # Python 3.7 keeps these for compatibility; both mean "a number literal".
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            return _power(_eval(node.left, depth + 1), _eval(node.right, depth + 1))
        handler = _ALLOWED_BINOPS.get(type(node.op))
        if handler is None:
            raise ToolError("that operator is not one I can evaluate.")
        return handler(_eval(node.left, depth + 1), _eval(node.right, depth + 1))
    if isinstance(node, ast.UnaryOp):
        handler = _ALLOWED_UNARY.get(type(node.op))
        if handler is None:
            raise ToolError("that unary operator is not one I can evaluate.")
        return handler(_eval(node.operand, depth + 1))
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        raise ToolError(
            "{0!r} is not something this tool knows. It evaluates arithmetic only, "
            "so there is nothing to look up.".format(node.id)
        )
    if isinstance(node, ast.List):
        if len(node.elts) > MAX_LIST_ITEMS:
            raise ToolError("that list is longer than this tool allows.")
        return [_eval(item, depth + 1) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(item, depth + 1) for item in node.elts)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ToolError(
                "that function is not one this tool has. Available: "
                + ", ".join(sorted(_ALLOWED_FUNCS))
                + "."
            )
        if node.keywords:
            raise ToolError("keyword arguments are not supported here.")
        args = [_eval(arg, depth + 1) for arg in node.args]
        try:
            return _ALLOWED_FUNCS[node.func.id](*args)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError("{0}() called incorrectly: {1}".format(node.func.id, exc)) from None
    raise ToolError(
        "{0} is not part of what this tool evaluates.".format(type(node).__name__)
    )


def _format_number(value):
    """A number the way a person would write it, not the way Python prints it."""
    if isinstance(value, bool):
        raise ToolError("that expression evaluated to a true/false value, not a number.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if value == int(value) and abs(value) < 1e16:
            return str(int(value))
        return "{0:.10g}".format(round(value, 10))
    raise ToolError("that expression evaluated to something that is not a number.")


def calculate(expression):
    """Evaluate one arithmetic expression. -> {"expression", "value", "text"}."""
    expression = (expression or "").strip()
    if not expression:
        raise ToolError('calculate needs an `expression`, for example "(84 + 91) / 2".')
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ToolError(
            "that expression is longer than this tool allows ({0} chars).".format(
                MAX_EXPRESSION_CHARS
            )
        )
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError("that is not an expression I can parse: {0}".format(exc.msg)) from None
    except RecursionError:
        # CPython's parser gives up before we do on some inputs; either way this is
        # a malformed expression, not a crashed turn.
        raise ToolError("that expression nests too deeply.") from None
    try:
        value = _eval(tree)
    except ToolError:
        raise
    except RecursionError:
        raise ToolError("that expression nests too deeply.") from None
    except ZeroDivisionError:
        raise ToolError("that divides by zero.") from None
    except OverflowError:
        raise ToolError("that number is too large for this tool to work with.") from None
    except ValueError as exc:
        raise ToolError("that has no answer in real numbers ({0}).".format(exc)) from None
    return {"expression": expression, "value": value, "text": _format_number(value)}


# ------------------------------------------------------------ propose_skill --
#
# The one tool that produces something *for* the student's library, and it does
# not touch it. It validates the file the model wrote against the registry's own
# rules and hands it back: the model gets to hear what is wrong with it and try
# again before the student is ever asked anything, which is the difference between
# a drafting tool and a broken one.

_tiers_cache = None


def configured_tiers():
    """Which tiers a skill may name, read from the same config the server loads.

    Read here rather than passed in because the tool is dispatched by name from a
    loop that knows nothing about config, and imported rather than hard-coded in
    the tool card for the reason `skills.py` gives about the enum: a vocabulary
    that lives in prose is a vocabulary that drifts. An unreadable config is the
    server's problem to report, not a tool failure - with no list the tier check is
    simply skipped here and the server's own check still applies.
    """
    global _tiers_cache
    if _tiers_cache is None:
        try:
            _tiers_cache = list(load_config()["tiers"])
        except (OSError, ValueError, KeyError, TypeError):
            _tiers_cache = []
    return _tiers_cache


def propose_skill(name, skill_md, *, root=None, tiers=None):
    """Check a skill file the model wrote. -> {"text", "skill", "draft"}.

    Nothing is written. The returned `draft` is the file as it will be shown to the
    student and, if they agree, saved byte for byte.
    """
    try:
        skill = check_skill(
            root or SKILLS_DIR,
            name,
            skill_md,
            configured_tiers() if tiers is None else tiers,
        )
    except SkillWriteError as exc:
        raise ToolError(
            "that skill file was not accepted: {0} Every fenced block needs its own "
            "opening and closing ``` line, and the frontmatter needs both --- lines. "
            "Fix the file and propose it again, or tell the student you could not "
            "write it.".format(exc),
            exc.code,
        ) from None
    arguments = ", ".join(skill.input_schema) or "no arguments"
    return {
        "text": (
            "NOT SAVED YET. The file is a valid skill: {name!r}, tier {tier}, "
            "arguments: {args}. It would be written to skills/{name}/SKILL.md, and "
            "only if the student approves the exact text you wrote. Say what the "
            "skill does and that you have drafted it - never that it is saved."
        ).format(name=skill.name, tier=skill.tier, args=arguments),
        "skill": skill.name,
        "draft": skill_md,
    }


# How far the calendar tool looks, and how far it may be asked to. The brief shows
# a week; a student asking "what's coming up" may mean the month, and thirty days
# is small enough to stay inside a prompt.
CALENDAR_DAYS_DEFAULT = 7
CALENDAR_DAYS_MAX = 30


def read_calendar(days=None, *, section=None, today=None):
    """The student's Google Calendar, read-only. -> {"text": ...}.

    `section` is injected by the server, which owns the token store, the demo flag
    and the read-only token - the same seam `opener` is for search. Nothing in here
    touches Google, which is why this tool cannot write: there is no code path from
    it to an API call at all.

    The text is what the model reasons over, so it carries the absolute dates as
    well as the relative phrase: "tomorrow" is not enough to plan around, and
    "2026-10-20 (in 3 days)" is.
    """
    if section is None:
        raise ToolError(
            "this server has no calendar attached, so there is nothing to read.",
            "no_calendar",
        )
    if days in (None, ""):
        horizon = CALENDAR_DAYS_DEFAULT
    else:
        try:
            # `7.0` is a whole number spelled differently; `2.5` is a different
            # window than the one asked for, so it is refused rather than rounded.
            number = float(days)
            if number != int(number):
                raise ValueError("not a whole number")
            horizon = int(number)
        except (TypeError, ValueError):
            raise ToolError(
                "days has to be a whole number between 1 and {0}.".format(CALENDAR_DAYS_MAX),
                "bad_args",
            ) from None
        if horizon < 1 or horizon > CALENDAR_DAYS_MAX:
            raise ToolError(
                "days has to be a whole number between 1 and {0}.".format(CALENDAR_DAYS_MAX),
                "bad_args",
            )

    payload = section(horizon) or {}
    error = payload.get("error") or {}
    if error and isinstance(error, dict):
        raise ToolError(
            str(error.get("message") or "the calendar could not be read"),
            str(error.get("code") or "failed"),
        )
    if not payload.get("connected"):
        raise ToolError(
            str(
                payload.get("explanation")
                or "the student's Google Calendar is not connected, so there is "
                "nothing to read. They can connect it from the Today tab of the "
                "panel, where the Connect Google Calendar link is."
            ),
            "not_connected",
        )

    day = today or date.today()
    end = day + timedelta(days=horizon)
    lines = payload.get("lines") or []
    names = [str(cal.get("name") or "") for cal in payload.get("calendars") or [] if isinstance(cal, dict)]
    if not lines:
        return {
            "text": (
                "Nothing is scheduled on the student's calendar between {0} and {1} "
                "({2} days){3}. Say the calendar is empty for that window; do not "
                "describe an event that is not here."
            ).format(
                day.isoformat(),
                end.isoformat(),
                horizon,
                " across " + ", ".join(names) if names else "",
            )
        }

    events = payload.get("events") or []
    rows = []
    for index, line in enumerate(lines):
        event = events[index] if index < len(events) else {}
        day_text = str(event.get("day") or "")
        rows.append("- {0}{1}".format(line, " [" + day_text + "]" if day_text else ""))

    text = "The student's calendar, {0} to {1} ({2} days){3}:\n{4}".format(
        day.isoformat(),
        end.isoformat(),
        horizon,
        " across " + ", ".join(names) if names else "",
        "\n".join(rows),
    )
    if (payload.get("counts") or {}).get("more"):
        text += (
            "\n(Google had more events than this list holds; say so if the student "
            "asks for the whole picture.)"
        )
    return {"text": text}


# ------------------------------------------------------------- the tool set ---
#
# One place that says what exists. The loop renders its prompt from these cards
# and dispatches through this table, so the prompt and the handlers cannot drift
# apart - the same property the skills registry has, for the same reason.

_TOOL_CARDS = (
    {
        "name": "web_search",
        "args": {"query": "string"},
        "when": (
            "Use it when the answer depends on a current fact you cannot verify "
            "from memory: a date, a release, a price, a score, a syllabus that "
            "changes. Do not use it for definitions or anything the student's own "
            "files already answer."
        ),
    },
    {
        "name": "calculate",
        "args": {"expression": "string, arithmetic only"},
        "when": (
            "Use it for any arithmetic you would otherwise do in your head: "
            "grades, percentages, averages. It evaluates numbers, "
            "+ - * / // % **, brackets, pi/e, and the functions "
            "abs, ceil, cos, degrees, exp, floor, len, log, log10, log2, max, "
            "mean, min, radians, round, sin, sqrt, sum, tan. It cannot read "
            "files, use the network, or run a program, and it has no dates: a "
            "countdown is a fact you are given, not a calculation you do."
        ),
    },
    {
        "name": "read_calendar",
        "args": {"days": "integer 1-30, optional; default 7"},
        "when": (
            "Use it whenever the student asks about their own schedule: what is on "
            "their calendar, their next event, what is coming up this week, whether "
            "they are busy on a given day. It reads their Google Calendar "
            "(read-only) and returns the events as lines with dates. Never say you "
            "cannot see their calendar without calling it, and never state an event "
            "that is not in the result - if the result is empty, the window is empty."
        ),
    },
    {
        "name": "propose_skill",
        "args": {
            "name": "string, lowercase letters, digits and underscores",
            "skill_md": "string, the entire SKILL.md file",
        },
        # The format is spelled out as a whole file rather than described, because
        # the first version described it and the model got it wrong four times in a
        # row on the live account: frontmatter with no closing '---', and `context`
        # filled with prose about the course instead of one of its four values. A
        # model that has never seen a SKILL.md copies a template; it does not
        # reconstruct a spec. This is the one card long enough to be worth its
        # tokens, and it is only in the prompt on general turns.
        "when": (
            "Use it when the student asks you to save *how* you did something so it "
            "can be used again: 'save that as a skill', 'make a skill for my lab "
            "reports'. It checks the file you wrote and does not save it - the "
            "student sees the exact text and decides, so never say a skill has been "
            "saved. `skill_md` is a complete SKILL.md, exactly this shape - the "
            "closing --- and both fenced blocks are part of the file:\n"
            "---\n"
            "name: <the same name as the argument>\n"
            'description: "<one line saying what it does>"\n'
            'when_to_use: "<the request that should choose it, in the student\'s words>"\n'
            'not_for: "<the requests that should not>"\n'
            "tier: super\n"
            "# context: [page]   <- optional, and only from: page, previous, memory, quoted\n"
            "---\n"
            "\n"
            "# <name>\n"
            "\n"
            "## When to Use\n"
            "\n"
            "<when this skill should run>\n"
            "\n"
            "## Input\n"
            "\n"
            '```json\n{"goal": {"type": "string", "required": true, "description": "<what it is>"}}\n```\n'
            "\n"
            "## Prompt\n"
            "\n"
            "```\n<the prompt, using {goal} for the arguments above>\n```\n"
            "The action must be prompt - a skill written this way may only say "
            "things, never select a handler that writes."
        ),
    },
)

TOOL_NAMES = tuple(card["name"] for card in _TOOL_CARDS)


def tool_cards():
    """The tool set as the model is shown it. Ordered, so the prompt is stable."""
    return [dict(card) for card in _TOOL_CARDS]


def tool_help():
    """The tool cards as one JSON object, for the loop's system prompt."""
    return json.dumps({"tools": tool_cards()}, ensure_ascii=False, indent=2)


def run_tool(name, args, *, opener=None, timeout=SEARCH_TIMEOUT, skills_root=None, calendar=None):
    """Run one tool call. Never raises for anything the model did.

    -> {"name", "ok", "text", "sources", "reason", "draft"}. A failure is a
    *result*: the loop hands it back to the model, which is the only party that can
    choose to do something else. An exception here would abort a turn instead.

    Every path answers with the same keys, `draft` included, because the loop reads
    it without asking which tool ran - and a key that exists on the happy path and
    not on the failure path is exactly the kind of hole the citation tests found in
    this function's `sources` once already.
    """
    name = (name or "").strip()
    args = args if isinstance(args, dict) else {}
    if name not in TOOL_NAMES:
        return {
            "name": name or "?",
            "ok": False,
            "text": "TOOL FAILED: there is no tool called {0!r}. The only tools are: "
            "{1}.".format(name, ", ".join(TOOL_NAMES)),
            "sources": [],
            "draft": None,
            "reason": "unknown_tool",
        }
    try:
        if name == "calculate":
            result = calculate(args.get("expression"))
        elif name == "read_calendar":
            result = read_calendar(args.get("days"), section=calendar)
        elif name == "propose_skill":
            result = propose_skill(args.get("name"), args.get("skill_md"), root=skills_root)
        else:
            result = web_search(args.get("query"), opener=opener, timeout=timeout)
    except ToolError as exc:
        return {
            "name": name,
            "ok": False,
            "text": "TOOL FAILED: {0}".format(exc),
            "sources": [],
            "draft": None,
            "reason": exc.code,
        }
    return {
        "name": name,
        "ok": True,
        "text": result["text"],
        # Search results carry the pages behind them; every other tool has none. A
        # failed search has none either - the empty list is the point, because the
        # alternative is a model with no results and a citation to write anyway.
        "sources": result.get("sources") or [],
        # The skill file this step drafted, as {name, text}, or None. The loop
        # carries it; it is not written by anything that runs in here.
        "draft": (
            {"name": result["skill"], "text": result["draft"]}
            if result.get("draft")
            else None
        ),
        "reason": "",
    }
