"""Reading and writing Obsidian notes: front matter, wikilinks, tags, tasks, headings.

Pure functions on text, no file access. The front matter is read with a small parser of the YAML Obsidian writes
(scalars, inline and block lists, one level of nested keys) and changed *surgically*: a property Clara does not touch
keeps its exact lines, so the person's own formatting, comments and nested values survive.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# --- text helpers ---------------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case without accents: how names and words are compared (é = e, Œ = oe)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def mask_code(text: str) -> str:
    """The text with fenced code blocks and inline code replaced by spaces (same length, same lines), so that
    nothing written as an example in a code block is taken for a link, a tag or a task."""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0] * 3
                out.append(" " * len(line))
                continue
            out.append(INLINE_CODE_RE.sub(lambda found: " " * len(found.group(0)), line))
        else:
            if match and match.group(1)[0] == fence[0] and line.strip().strip(fence[0]) == "":
                fence = None
            out.append(" " * len(line))
    return "\n".join(out)


# --- front matter ---------------------------------------------------------------------------------------

KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_\-. ]*?)\s*:(?:\s+(.*)|\s*)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")
NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)$")
RESERVED_WORDS = {"true", "false", "null", "~", "yes", "no", "on", "off", "y", "n"}


@dataclass(frozen=True)
class Split:
    front: str | None  # the lines between the two `---`, without them (None: no front matter)
    body: str  # everything after
    body_line: int  # the 1-based line number the body starts at


def split_frontmatter(text: str) -> Split:
    if text.startswith("\ufeff"):
        text = text[1:]
    if not (text.startswith("---\n") or text.startswith("---\r\n") or text == "---"):
        return Split(None, text, 1)
    lines = text.split("\n")
    for number in range(1, len(lines)):
        if lines[number].rstrip("\r").rstrip() in ("---", "..."):
            front = "\n".join(lines[1:number])
            return Split(front, "\n".join(lines[number + 1 :]), number + 2)
    return Split(None, text, 1)  # never closed: it is not front matter


def _split_inline(text: str) -> list[str]:
    """`a, "b, c", d` -> ['a', '"b, c"', 'd'] (commas inside quotes and brackets do not split)."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    last = "".join(current).strip()
    if last or parts:
        parts.append(last)
    return parts


QUOTED_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
SINGLE_RE = re.compile(r"^'((?:[^']|'')*)'\s*(?:#.*)?$")
ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw[:1] == '"':
        match = QUOTED_RE.match(raw)
        if match:
            return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), "\\" + m.group(1)), match.group(1))
        return raw
    if raw[:1] == "'":
        match = SINGLE_RE.match(raw)
        return match.group(1).replace("''", "'") if match else raw
    if raw.startswith("#"):
        return None
    raw = re.split(r"\s#", raw, maxsplit=1)[0].rstrip()
    if raw in ("", "~") or raw.lower() == "null":
        return None
    if raw.startswith("[[") and raw.endswith("]]") and "," not in raw:
        return raw  # a wikilink the person wrote without quotes
    if raw[0] == "[" and raw.endswith("]"):
        return [parse_scalar(item) for item in _split_inline(raw[1:-1].strip()) if item != ""]
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if NUMBER_RE.match(raw) and not (len(raw) > 1 and raw.startswith("0") and raw[1].isdigit()):
        return float(raw) if "." in raw else int(raw)
    return raw


def block_key(line: str) -> str | None:
    """The key a front matter line starts, or None for a continuation (indented, list item, comment)."""
    if not line or line[0] in " \t-#":
        return None
    match = KEY_RE.match(line)
    return match.group(1).strip() if match else None


def front_blocks(front: str) -> list[tuple[str | None, list[str]]]:
    """The front matter as `(key, lines)` blocks in order; lines before the first key (comments) have key None."""
    blocks: list[tuple[str | None, list[str]]] = []
    for line in front.split("\n") if front else []:
        key = block_key(line)
        if key is not None or not blocks:
            blocks.append((key, [line]))
        else:
            blocks[-1][1].append(line)
    return blocks


def parse_block(lines: list[str]) -> Any:
    """The value of one front matter block (its first line is `key: ...`)."""
    match = KEY_RE.match(lines[0])
    inline = (match.group(2) or "").strip() if match else ""
    rest = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
    if inline and inline[0] in "|>":
        folded = inline[0] == ">"
        text = [line.strip() for line in lines[1:]]
        return (" " if folded else "\n").join(text).strip()
    if inline:
        return parse_scalar(inline)
    if not rest:
        return None
    if all(line.lstrip().startswith("-") for line in rest) and rest[0].lstrip().startswith("-"):
        return [parse_scalar(line.lstrip()[1:].strip()) for line in rest]
    nested: dict[str, Any] = {}
    for line in rest:
        child = KEY_RE.match(line.strip())
        if child and not line.strip().startswith("-"):
            nested[child.group(1).strip()] = parse_scalar(child.group(2) or "")
        else:
            return "\n".join(line.strip() for line in rest)
    return nested


def parse_properties(front: str | None) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key, lines in front_blocks(front or ""):
        if key is not None:
            props[key] = parse_block(lines)
    return props


def _needs_quotes(text: str) -> bool:
    if text == "" or text != text.strip() or "\n" in text:
        return True
    if text.lower() in RESERVED_WORDS or NUMBER_RE.match(text):
        return True
    if text[0] in "[]{}&*!|>'\"%@`#-?:,":
        return True
    return ": " in text or " #" in text or text.endswith(":")


def dump_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value)
    if DATE_RE.match(text) or DATETIME_RE.match(text):
        return text  # Obsidian reads these as dates
    if _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def dump_property(key: str, value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            return [f"{key}: []"]
        return [f"{key}:"] + [f"  - {dump_scalar(item)}" for item in items]
    if isinstance(value, dict):
        if not value:
            return [f"{key}: {{}}"]
        return [f"{key}:"] + [f"  {child}: {dump_scalar(item)}" for child, item in value.items()]
    scalar = dump_scalar(value)
    return [f"{key}: {scalar}" if scalar else f"{key}:"]


def apply_properties(text: str, set_values: dict[str, Any] | None = None, remove: list[str] | None = None) -> str:
    """The note's text with front matter properties set (added at the end if new) and removed; every other line of
    the front matter is left as it was. A note without front matter gets one."""
    set_values = set_values or {}
    remove = remove or []
    parts = split_frontmatter(text)
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    blocks = front_blocks(parts.front) if parts.front is not None else []
    done: set[str] = set()
    out: list[str] = []
    for key, lines in blocks:
        if key is not None and key in remove:
            continue
        if key is not None and key in set_values:
            out.extend(dump_property(key, set_values[key]))
            done.add(key)
        else:
            out.extend(lines)
    for key, value in set_values.items():
        if key not in done and key not in remove:
            out.extend(dump_property(key, value))
    while out and out[-1] == "":
        out.pop()
    if not out:
        return bom + parts.body.lstrip("\n") if parts.front is not None else text
    body = parts.body
    return f"{bom}---\n" + "\n".join(out) + "\n---\n" + body


def normal_tag(tag: str) -> str:
    return tag.strip().lstrip("#").strip().strip("/")


def tags_of(props: dict[str, Any]) -> list[str]:
    """The front matter `tags` (a list, or a text of tags separated by commas or spaces), without their `#`."""
    value = props.get("tags", props.get("tag"))
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [part for part in re.split(r"[,\s]+", value) if part]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    return [tag for tag in (normal_tag(str(item)) for item in items) if tag]


def aliases_of(props: dict[str, Any]) -> list[str]:
    value = props.get("aliases", props.get("alias"))
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item).strip()]


# --- links, tags, headings, tasks ----------------------------------------------------------------------

WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]|#^\n]*)((?:#[^\[\]|\n]*)?)((?:\|[^\[\]\n]*)?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[([^\[\]\n]*)\]\(<?([^()<>\s][^()<>]*?)>?(?:\s+\"[^\"]*\")?\)")
TAG_RE = re.compile(r"(?<![\w/&#\[(])#([^\W\d][\w\-/]*|[\w\-/]*[^\W\d][\w\-/]*)")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
TASK_RE = re.compile(r"^(\s*)([-*+])\s+\[(.)\]\s+(.*)$")
DUE_RE = re.compile(r"(?:📅\s*|\[?due::\s*)(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Link:
    target: str  # the note it names, without anchor and alias; or the path of a markdown link
    line: int
    anchor: str = ""  # "#Heading" or "#^block"
    alias: str = ""
    embed: bool = False
    markdown: bool = False  # [text](path.md) rather than [[wikilink]]


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class Task:
    line: int
    done: bool
    text: str
    due: str = ""


def _line_of(text: str, offset: int, first_line: int) -> int:
    return first_line + text.count("\n", 0, offset)


def find_links(text: str, first_line: int = 1) -> list[Link]:
    masked = mask_code(text)
    links: list[Link] = []
    for match in WIKILINK_RE.finditer(masked):
        target = text[match.start(2) : match.end(2)].strip()
        if not target and not match.group(3):
            continue
        links.append(
            Link(
                target, _line_of(text, match.start(), first_line), text[match.start(3) : match.end(3)],
                text[match.start(4) + 1 : match.end(4)].strip() if match.group(4) else "", bool(match.group(1)),
            )
        )
    for match in MDLINK_RE.finditer(masked):
        raw = text[match.start(3) : match.end(3)].strip()
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) or raw.startswith("#"):
            continue  # a web address or an anchor of this note
        path, _, anchor = raw.partition("#")
        links.append(
            Link(
                path, _line_of(text, match.start(), first_line), "#" + anchor if anchor else "",
                text[match.start(2) : match.end(2)], bool(match.group(1)), markdown=True,
            )
        )
    links.sort(key=lambda link: link.line)
    return links


def find_tags(text: str) -> list[str]:
    masked = WIKILINK_RE.sub(lambda found: " " * len(found.group(0)), mask_code(text))
    seen: dict[str, None] = {}
    for line in masked.split("\n"):
        if HEADING_RE.match(line):
            line = line.lstrip("#")  # `# Title` is a heading; a `#tag` after it is still a tag
        for match in TAG_RE.finditer(line):
            seen.setdefault(match.group(1).strip("/"), None)
    return [tag for tag in seen if tag]


def find_headings(text: str, first_line: int = 1) -> list[Heading]:
    masked = mask_code(text)
    found: list[Heading] = []
    for number, line in enumerate(masked.split("\n")):
        match = HEADING_RE.match(line)
        if match:
            original = text.split("\n")[number]
            found.append(Heading(len(match.group(1)), HEADING_RE.match(original).group(2), first_line + number))
    return found


def find_tasks(text: str, first_line: int = 1) -> list[Task]:
    masked = mask_code(text)
    original = text.split("\n")
    tasks: list[Task] = []
    for number, line in enumerate(masked.split("\n")):
        if not TASK_RE.match(line):
            continue
        match = TASK_RE.match(original[number])
        if not match:
            continue
        mark, content = match.group(3), match.group(4).strip()
        due = DUE_RE.search(content)
        tasks.append(Task(first_line + number, mark in "xX", content, due.group(1) if due else ""))
    return tasks


def first_paragraph(body: str, limit: int = 160) -> str:
    """The first line of prose of a note (not a heading, a list marker alone or a comment): its summary."""
    masked = mask_code(body)
    original = body.split("\n")
    for number, line in enumerate(masked.split("\n")):
        stripped = original[number].strip()
        if not line.strip() or stripped.startswith(("#", "<!--", "---", ">", "|")):
            continue
        stripped = re.sub(r"^[-*+]\s+(\[.\]\s+)?", "", stripped)
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1].rstrip() + "…"
    return ""


def section_range(text: str, heading: str) -> tuple[int, int, int] | None:
    """(index of the heading line, first line after the section, level) of the section called `heading` (case and
    accents ignored; `## Log` and `Log` both work), counting lines from 0; None when there is none. A section
    ends at the next heading of the same or a higher level."""
    wanted = heading.strip().lstrip("#").strip()
    lines = text.split("\n")
    masked = mask_code(text).split("\n")
    for number, line in enumerate(masked):
        match = HEADING_RE.match(line)
        if match and fold(HEADING_RE.match(lines[number]).group(2)) == fold(wanted):
            level = len(match.group(1))
            end = len(lines)
            for later in range(number + 1, len(lines)):
                follow = HEADING_RE.match(masked[later])
                if follow and len(follow.group(1)) <= level:
                    end = later
                    break
            return number, end, level
    return None


def add_to_section(text: str, heading: str, addition: str, level: int = 2) -> str:
    """`addition` at the end of the section `heading` (created at the end of the note when missing)."""
    addition = addition.strip("\n")
    lines = text.split("\n")
    found = section_range(text, heading)
    if found is None:
        title = heading.strip().lstrip("#").strip()
        base = text.rstrip("\n")
        return f"{base}\n\n{'#' * level} {title}\n\n{addition}\n" if base else f"{'#' * level} {title}\n\n{addition}\n"
    heading_line, end, _ = found
    last = end
    while last > heading_line + 1 and lines[last - 1].strip() == "":
        last -= 1
    before, after = lines[:last], lines[end:]
    joined = before + ([""] if last == heading_line + 1 else []) + [addition]
    if after:
        joined += [""] + after if after[0].strip() else after
    result = "\n".join(joined)
    return result if result.endswith("\n") else result + "\n"


# --- dates in templates ----------------------------------------------------------------------------------

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
    "December",
)
MOMENT_RE = re.compile(r"YYYY|YY|MMMM|MMM|MM|M|DDDD|DD|D|dddd|ddd|HH|H|mm|ss|\[[^\]]*\]")


def format_date(moment: datetime | date, pattern: str) -> str:
    """The moment.js patterns Obsidian templates use: YYYY MM DD HH mm ss dddd MMMM ..."""
    time_part = moment if isinstance(moment, datetime) else datetime(moment.year, moment.month, moment.day)

    def one(match: re.Match) -> str:
        token = match.group(0)
        return {
            "YYYY": f"{time_part.year:04d}", "YY": f"{time_part.year % 100:02d}", "MMMM": MONTHS[time_part.month - 1],
            "MMM": MONTHS[time_part.month - 1][:3], "MM": f"{time_part.month:02d}", "M": str(time_part.month),
            "DDDD": f"{time_part.timetuple().tm_yday:03d}", "DD": f"{time_part.day:02d}", "D": str(time_part.day),
            "dddd": DAYS[time_part.weekday()], "ddd": DAYS[time_part.weekday()][:3], "HH": f"{time_part.hour:02d}",
            "H": str(time_part.hour), "mm": f"{time_part.minute:02d}", "ss": f"{time_part.second:02d}",
        }.get(token, token[1:-1] if token.startswith("[") else token)

    return MOMENT_RE.sub(one, pattern)


TEMPLATE_RE = re.compile(r"\{\{\s*(title|date|time|datetime|content)(?::([^}]*?))?\s*\}\}")


def fill_template(template: str, title: str, moment: datetime, content: str = "") -> str:
    """Obsidian's template variables: {{title}} {{date}} {{time}} {{date:FORMAT}} {{time:FORMAT}}, plus
    {{datetime}} and {{content}} (where Clara's text goes when the template has the place for it)."""

    def one(match: re.Match) -> str:
        name, pattern = match.group(1), (match.group(2) or "").strip()
        if name == "title":
            return title
        if name == "content":
            return content
        if name == "date":
            return format_date(moment, pattern or "YYYY-MM-DD")
        if name == "time":
            return format_date(moment, pattern or "HH:mm")
        return format_date(moment, pattern or "YYYY-MM-DDTHH:mm")

    return TEMPLATE_RE.sub(one, template)
