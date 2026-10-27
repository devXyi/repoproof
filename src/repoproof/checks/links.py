"""Internal documentation links: do the files, directories and anchors they name exist?

Only relative links are checked; external URLs are never fetched. Paths are matched
case-sensitively, exactly like GitHub does, so a link that works on a case-insensitive
laptop but breaks on GitHub is still reported.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote

from ..config import LinksConfig
from ..markdown import Link, MarkdownDoc, parse_markdown
from ..models import Finding
from ..policy import matches

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_ANCHOR = re.compile(r"^L\d+(?:C\d+)?(?:-L\d+(?:C\d+)?)?$")
_MARKDOWN_SUFFIXES = (".md", ".markdown")


class _Tree:
    """Case-sensitive existence checks with cached directory listings."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._listings: dict[str, set[str]] = {}

    def _names(self, relative_dir: str) -> set[str]:
        if relative_dir not in self._listings:
            try:
                self._listings[relative_dir] = set(os.listdir(self.root / relative_dir))
            except OSError:
                self._listings[relative_dir] = set()
        return self._listings[relative_dir]

    def exists(self, relative: str) -> bool:
        if relative in ("", "."):
            return True
        current = ""
        for part in relative.split("/"):
            if part not in self._names(current):
                return False
            current = posixpath.join(current, part) if current else part
        return True

    def is_file(self, relative: str) -> bool:
        return self.exists(relative) and (self.root / relative).is_file()


def check_links(
    root: Path, docs: Mapping[str, MarkdownDoc], config: LinksConfig | None = None
) -> list[Finding]:
    """Check every relative link in ``docs`` (keyed by repo-relative POSIX path)."""
    config = config or LinksConfig()
    tree = _Tree(root)
    parsed: dict[str, MarkdownDoc | None] = dict(docs)
    findings: list[Finding] = []
    for relative, doc in docs.items():
        for link in doc.links:
            finding = _check_link(relative, doc, link, tree, parsed, config)
            if finding is not None:
                findings.append(finding)
    return findings


def _check_link(
    source: str,
    doc: MarkdownDoc,
    link: Link,
    tree: _Tree,
    parsed: dict[str, MarkdownDoc | None],
    config: LinksConfig,
) -> Finding | None:
    target = link.target.strip()
    if not target or target.startswith("//") or _SCHEME.match(target) or "{{" in target:
        return None  # external, templated or empty
    path_part, _, fragment = target.partition("#")
    path_part = path_part.split("?", 1)[0]
    fragment = unquote(fragment)

    if path_part == "":
        if fragment and not _has_anchor(doc, fragment):
            return _anchor_finding(source, link, fragment, source)
        return None

    decoded = unquote(path_part)
    if decoded.startswith("/"):
        resolved = posixpath.normpath(decoded.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
    if any(matches(pattern, resolved) or matches(pattern, target) for pattern in config.ignore):
        return None
    kind = "image" if link.is_image else "link"
    if resolved == ".." or resolved.startswith("../"):
        return Finding(
            "RP201",
            f"The {kind} '{target}' points outside the repository",
            file=source,
            line=link.line,
        )
    if not tree.exists(resolved):
        looked = "" if resolved == target else f" (looked for '{resolved}')"
        return Finding(
            "RP201",
            f"The {kind} target '{target}' does not exist{looked}",
            file=source,
            line=link.line,
            hint=_suggest(resolved, tree),
        )
    if fragment and not _LINE_ANCHOR.match(fragment) and resolved.lower().endswith(_MARKDOWN_SUFFIXES):
        other = _load(resolved, tree, parsed)
        if other is not None and not _has_anchor(other, fragment):
            return _anchor_finding(source, link, fragment, resolved)
    return None


def _has_anchor(doc: MarkdownDoc, fragment: str) -> bool:
    wanted = fragment.lower()
    return any(anchor.lower() == wanted for anchor in doc.anchors)


def _anchor_finding(source: str, link: Link, fragment: str, where: str) -> Finding:
    return Finding(
        "RP202",
        f"The anchor '#{fragment}' was not found in {where}",
        file=source,
        line=link.line,
        hint="Anchors come from headings; renaming a heading breaks links to it.",
    )


def _load(relative: str, tree: _Tree, parsed: dict[str, MarkdownDoc | None]) -> MarkdownDoc | None:
    if relative not in parsed:
        parsed[relative] = None
        if tree.is_file(relative):
            try:
                text = (tree.root / relative).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            parsed[relative] = parse_markdown(text, relative)
    return parsed[relative]


def _suggest(resolved: str, tree: _Tree) -> str | None:
    """Point out a case-only mismatch, the most common cause on case-insensitive systems."""
    current = ""
    parts: list[str] = []
    for part in resolved.split("/"):
        names = tree._names(current)
        if part in names:
            actual = part
        else:
            candidates = [name for name in names if name.lower() == part.lower()]
            if len(candidates) != 1:
                return None
            actual = candidates[0]
        parts.append(actual)
        current = posixpath.join(current, actual) if current else actual
    corrected = "/".join(parts)
    if corrected == resolved:
        return None
    return f"A path that differs only in case exists: '{corrected}'."
