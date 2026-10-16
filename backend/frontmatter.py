"""A small, strict parser for SKILL.md YAML frontmatter.

Why not PyYAML: this backend stays dependency-free on purpose, so
`python3 backend/server.py` is the entire install story for judges and for
`/demo` mode. Frontmatter only ever holds a handful of scalar keys, one nested
map, inline lists, and the occasional block string, so a focused parser covers
real skill files without pulling in a YAML implementation.

The design rule is **fail loudly, never guess**. Anything this parser does not
understand raises FrontmatterError naming the offending line, because a
silently mis-parsed skill is far worse than a skill that refuses to load: it
would route the student's request to the wrong behaviour without a trace.

Supported (all of it exercised by real Hermes skill files):
    key: value              plain, 'single', "double" quoted
    key: 12 / 1.5           ints and floats (a value that only *looks* numeric
                            stays text: "582e6104" and "01234567" are ids)
    key: true / false       booleans (also yes/no);  null / ~ -> None
    key: [a, b, c]          inline lists
    key:                    nested mapping on the following indented lines
      child: value
    key:                    block lists
      - item
      - name: value         list of mappings
        other: value
    key: |                  literal block string (newlines kept)
    key: >                  folded block string (newlines become spaces)
"""

import math


class FrontmatterError(ValueError):
    """Raised when frontmatter cannot be parsed with confidence."""


_BOOLS = {"true": True, "yes": True, "false": False, "no": False}
_NULLS = {"null", "~", ""}


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_inline_list(inner):
    """Split "a, 'b, c', [d]" on top-level commas only."""
    parts, current, depth, quote = [], [], 0, None
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip() != ""]


def parse_scalar(raw):
    """Turn one right-hand-side string into a Python value."""
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        return [parse_scalar(part) for part in _split_inline_list(value[1:-1])]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    lowered = value.lower()
    if lowered in _BOOLS:
        return _BOOLS[lowered]
    if lowered in _NULLS:
        return None
    # Numbers are only numbers when reading them back gives the same text.
    # The generated ids in this project (uuid4().hex[:8]) are written unquoted
    # and some of them look like numbers to Python: "582e6104" reads as
    # scientific notation and overflows to inf, and "01234567" reads as
    # 1234567. Either way the value comes back changed, which breaks the one
    # thing an id has to do - match itself after a reload. A mangled id is a
    # quiz you cannot open, answer or delete.
    try:
        number = int(value)
    except ValueError:
        pass
    else:
        if str(number) == value:
            return number
    try:
        number = float(value)
    except ValueError:
        pass
    else:
        if math.isfinite(number) and repr(number) == value:
            return number
    return value


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines, start, indent, limit=None):
    """Parse the block beginning at `start` whose keys sit at `indent`.

    `limit` is the index one past the last line belonging to this block - the
    closing '---' for frontmatter. Without it the parser walks into the document
    body, where a '---' horizontal rule looks like a key.

    Returns (value, index_of_next_unconsumed_line).
    """
    if limit is None:
        limit = len(lines)
    index = start
    mapping = {}
    is_list = False
    result = None

    while index < limit:
        line = lines[index]
        stripped = line.strip()

        # Skip blank lines and full-line YAML comments inside a block.
        # Only a line that STARTS with '#' is a comment - inside a '|' block
        # string a leading '#' is content, and that loop below keeps it.
        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        current = _indent_of(line)
        if current < indent:
            break  # dedented: block is finished
        if current > indent:
            raise FrontmatterError(
                f"line {index + 1}: unexpected indentation ({current} spaces)"
            )

        if stripped.startswith("- "):
            # A block list. Bail out if it started as a mapping.
            if mapping:
                raise FrontmatterError(
                    f"line {index + 1}: cannot mix list items and keys in one block"
                )
            if result is None:
                is_list, result = True, []
            item_text = stripped[2:].strip()
            if ":" in item_text and not item_text.startswith(("'", '"')):
                # - name: value  (a mapping inside a list item)
                key, _, remainder = item_text.partition(":")
                entry = {key.strip(): parse_scalar(remainder)}
                index += 1
                # Consume further keys belonging to this item.
                while index < limit:
                    nxt = lines[index]
                    if not nxt.strip():
                        break
                    if _indent_of(nxt) <= current:
                        break
                    child_key, _, child_val = nxt.strip().partition(":")
                    entry[child_key.strip()] = parse_scalar(child_val)
                    index += 1
                result.append(entry)
                continue
            result.append(parse_scalar(item_text))
            index += 1
            continue

        if is_list:
            raise FrontmatterError(
                f"line {index + 1}: cannot mix keys into a list block"
            )

        if ":" not in stripped:
            raise FrontmatterError(
                f"line {index + 1}: expected 'key: value' but found {stripped!r}"
            )

        key, _, remainder = stripped.partition(":")
        key = key.strip()
        remainder = remainder.strip()

        if remainder in ("|", ">"):
            # Block string: everything more indented than the key.
            literal = remainder == "|"
            collected = []
            index += 1
            while index < limit:
                if lines[index].strip() and _indent_of(lines[index]) <= current:
                    break
                collected.append(lines[index].strip())
                index += 1
            while collected and collected[-1] == "":
                collected.pop()
            mapping[key] = (
                "\n".join(collected) if literal else " ".join(c for c in collected if c)
            )
            continue

        if remainder == "":
            # Either a nested block or an empty value.
            index += 1
            look = index
            while look < limit and not lines[look].strip():
                look += 1
            if look < limit and _indent_of(lines[look]) > current:
                nested, index = _parse_block(
                    lines, index, _indent_of(lines[look]), limit
                )
                mapping[key] = nested
            else:
                mapping[key] = None
            continue

        mapping[key] = parse_scalar(remainder)
        index += 1

    return (result if is_list else mapping), index


def parse(text):
    """Split a SKILL.md into (frontmatter_dict, body).

    No frontmatter is not an error - it just means an empty dict - but
    malformed frontmatter is.
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError("frontmatter opened with '---' but never closed")

    data, _ = _parse_block(lines, 1, 0, end)
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter must be a mapping of keys to values")

    return data, "\n".join(lines[end + 1:])
