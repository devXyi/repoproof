"""A small, dependency-free Markdown scanner.

This is intentionally not a CommonMark parser. It extracts only what RepoProof needs, with
accurate 1-based line numbers: fenced code blocks, links, headings and anchors. Fences may
be indented (as they are inside list items), HTML comments are ignored, and an HTML comment
of the form ``<!-- repoproof:skip -->`` marks the code block that follows it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*?)[ \t]*$")
_FENCE_CLOSE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_DIRECTIVE = re.compile(r"<!--\s*repoproof\s*:\s*([a-z][a-z-]*)\s*-->", re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->")
_REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*(?P<target><[^>]*>|\S+)")
_HTML_LINK = re.compile(
    r"""<(?P<tag>a|img)\b[^>]*?\b(?P<attr>href|src)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""",
    re.IGNORECASE,
)
_HTML_ID = re.compile(r"""\b(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.IGNORECASE)
_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NOT_PARAGRAPH = re.compile(r"^(?:[>|<#]|[-*+]\s|\d+[.)]\s)")


@dataclass(frozen=True)
class CodeBlock:
    info: str
    lang: str
    start_line: int
    lines: tuple[tuple[int, str], ...]
    directives: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Link:
    target: str
    line: int
    is_image: bool = False
    kind: str = "inline"  # inline | reference | html


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int
    slug: str


@dataclass
class MarkdownDoc:
    path: str
    text: str
    code_blocks: list[CodeBlock] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    headings: list[Heading] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)


@dataclass
class _OpenFence:
    char: str
    length: int
    indent: int
    info: str
    start: int
    directives: frozenset[str]
    lines: list[tuple[int, str]] = field(default_factory=list)


def parse_markdown(text: str, path: str = "") -> MarkdownDoc:
    """Scan ``text`` and return its code blocks, links, headings and anchors."""
    lines = text.splitlines()
    doc = MarkdownDoc(path=path, text=text)
    raw_headings: list[tuple[int, str, int]] = []

    fence: _OpenFence | None = None
    in_comment = False
    pending: set[str] = set()
    paragraph: tuple[int, str] | None = None

    first = 0
    if lines and lines[0].strip() == "---":  # YAML front matter
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                first = j + 1
                break

    for idx in range(first, len(lines)):
        number = idx + 1
        raw = lines[idx]

        if fence is not None:
            closing = _FENCE_CLOSE.match(raw)
            if closing and closing["fence"][0] == fence.char and len(closing["fence"]) >= fence.length:
                doc.code_blocks.append(_finish(fence))
                fence = None
            else:
                fence.lines.append((number, _dedent(raw, fence.indent)))
            continue

        if in_comment:
            end = raw.find("-->")
            if end == -1:
                continue
            in_comment = False
            raw = " " * (end + 3) + raw[end + 3 :]

        pending.update(d.lower() for d in _DIRECTIVE.findall(raw))

        opening = _FENCE_OPEN.match(raw)
        if opening and not (opening["fence"][0] == "`" and "`" in opening["info"]):
            fence = _OpenFence(
                char=opening["fence"][0],
                length=len(opening["fence"]),
                indent=len(opening["indent"].expandtabs(4)),
                info=opening["info"],
                start=number,
                directives=frozenset(pending),
            )
            pending = set()
            paragraph = None
            continue

        visible = _COMMENT.sub(lambda m: " " * len(m.group(0)), raw)
        if "<!--" in visible:
            visible = visible.split("<!--", 1)[0]
            in_comment = True
        stripped = visible.strip()
        if not stripped:
            paragraph = None
            continue
        pending.clear()  # a directive only applies to the block that directly follows it

        underline = _SETEXT.match(visible)
        if underline and paragraph is not None:
            level = 1 if underline[1][0] == "=" else 2
            raw_headings.append((level, paragraph[1], paragraph[0]))
            paragraph = None
            continue
        atx = _ATX.match(visible)
        if atx:
            raw_headings.append((len(atx[1]), atx[2], number))
            paragraph = None
        else:
            paragraph = None if _NOT_PARAGRAPH.match(stripped) else (number, stripped)

        scan = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), visible)
        reference = _REF_DEF.match(scan)
        if reference:
            doc.links.append(Link(_clean_target(reference["target"]), number, False, "reference"))
        for target, is_image in _inline_links(scan):
            doc.links.append(Link(target, number, is_image, "inline"))
        for html in _HTML_LINK.finditer(scan):
            target = html["dq"] if html["dq"] is not None else html["sq"]
            doc.links.append(Link(target, number, html["tag"].lower() == "img", "html"))
        for ident in _HTML_ID.finditer(scan):
            doc.anchors.add(ident[1] or ident[2])

    if fence is not None:  # an unterminated fence runs to the end of the document
        doc.code_blocks.append(_finish(fence))

    seen: dict[str, int] = {}
    for level, heading_text, number in raw_headings:
        base = github_slug(heading_text)
        count = seen.get(base, 0)
        seen[base] = count + 1
        slug = base if count == 0 else f"{base}-{count}"
        doc.headings.append(Heading(level, heading_text, number, slug))
        doc.anchors.add(slug)
    return doc


def github_slug(heading: str) -> str:
    """Approximate GitHub's heading anchor: lowercase, punctuation dropped, spaces to ``-``."""
    text = _IMAGE.sub("", heading)
    text = _LINK_TEXT.sub(r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\\(.)", r"\1", text)
    pieces = []
    for piece in re.split(r"(`+[^`]*`+)", text):
        if len(piece) >= 2 and piece.startswith("`") and piece.endswith("`"):
            pieces.append(piece.strip("`"))
        else:
            pieces.append(_strip_emphasis(piece))
    text = "".join(pieces).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _strip_emphasis(text: str) -> str:
    text = re.sub(r"\*+|~~", "", text)
    return re.sub(r"(?<!\w)_+(?=\S)|(?<=\S)_+(?!\w)", "", text)


def _finish(fence: _OpenFence) -> CodeBlock:
    return CodeBlock(
        info=fence.info,
        lang=_lang_of(fence.info),
        start_line=fence.start,
        lines=tuple(fence.lines),
        directives=fence.directives,
    )


def _lang_of(info: str) -> str:
    match = re.match(r"[{.\s]*([A-Za-z0-9_+#-]+)", info)
    return match[1].lower() if match else ""


def _dedent(line: str, indent: int) -> str:
    line = line.expandtabs(4)
    removable = min(indent, len(line) - len(line.lstrip(" ")))
    return line[removable:]


def _clean_target(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target


def _opening_bracket(text: str, close: int) -> int:
    depth = 0
    for i in range(close, -1, -1):
        if text[i] == "]":
            depth += 1
        elif text[i] == "[":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _inline_links(text: str) -> Iterator[tuple[str, bool]]:
    """Yield ``(target, is_image)`` for every ``[text](target)`` on one line."""
    pos = 0
    while True:
        close = text.find("](", pos)
        if close == -1:
            return
        opening = _opening_bracket(text, close)
        k = close + 2
        pos = k
        if opening == -1:
            continue
        while k < len(text) and text[k] in " \t":
            k += 1
        if k < len(text) and text[k] == "<":
            end = text.find(">", k + 1)
            if end == -1:
                continue
            target, k = text[k + 1 : end], end + 1
        else:
            start, depth = k, 0
            while k < len(text):
                ch = text[k]
                if ch == "\\":
                    k += 2
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch in " \t" and depth == 0:
                    break
                k += 1
            target = text[start:k]
        pos = k
        target = re.sub(r"\\([()<>\[\]\s])", r"\1", target).strip()
        if target:
            yield target, opening > 0 and text[opening - 1] == "!"
