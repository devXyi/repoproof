"""A deliberately small shell-command parser.

RepoProof never hands a documented command to a shell. It parses the command with this
module, vets every simple command against the policy, and executes the resulting ``argv``
directly, so what was parsed is exactly what runs.

Supported
    * simple commands with quoting (``'...'``, ``"..."``, backslash escapes)
    * ``VAR=value`` prefixes and ``$VAR`` / ``${VAR}`` / ``${VAR:-default}`` expansion
    * ``&&`` and ``;`` between commands (both mean "stop at the first failure")
    * ``>``, ``>>``, ``2>``, ``&>``, ``2>&1`` and ``>&2`` redirections to files
    * ``*``/``?``/``[...]`` globbing and a leading ``~``
    * trailing ``# comments`` and backslash line continuations

Rejected with :class:`Unsupported`
    * pipes, ``||``, background jobs, subshells, groups, control flow
    * command substitution (``$(...)`` and backticks), arithmetic, heredocs, ``<``
    * brace expansion, ``$'...'`` quoting, special parameters such as ``$1`` or ``$?``

Unsupported syntax is reported, never guessed at.
"""

from __future__ import annotations

import glob
import re
from collections.abc import Mapping
from dataclasses import dataclass


class ShellError(Exception):
    """Base class for parse problems."""


class Unsupported(ShellError):
    """Valid shell that RepoProof deliberately does not run (pipes, subshells, ...)."""


class Incomplete(ShellError):
    """The input ends inside a quote, after a backslash, or after ``&&``."""


@dataclass(frozen=True)
class Part:
    """A fragment of a word: literal text, a variable reference, or ``~``."""

    text: str
    kind: str = "lit"  # "lit" | "var" | "home"
    quoted: bool = False
    default: str | None = None


@dataclass(frozen=True)
class Word:
    """One shell word, kept as parts so quoting and expansion can be applied later."""

    parts: tuple[Part, ...]

    @property
    def plain_text(self) -> str | None:
        """The text of an unquoted, expansion-free word, otherwise ``None``."""
        if all(p.kind == "lit" and not p.quoted for p in self.parts):
            return "".join(p.text for p in self.parts)
        return None

    def display(self) -> str:
        """A readable rendering (variables shown as ``$NAME``)."""
        out = []
        for part in self.parts:
            if part.kind == "var":
                out.append("$" + part.text)
            elif part.kind == "home":
                out.append("~")
            else:
                out.append(part.text)
        return "".join(out)


@dataclass(frozen=True)
class Redirect:
    """``fd`` is 1, 2, or -1 for both. ``mode`` is ``>``, ``>>`` or ``dup``."""

    fd: int
    mode: str
    target: Word | None = None
    dup_of: int | None = None


@dataclass(frozen=True)
class SimpleCommand:
    assignments: tuple[tuple[str, Word], ...]
    words: tuple[Word, ...]
    redirects: tuple[Redirect, ...]


@dataclass(frozen=True)
class CommandList:
    commands: tuple[SimpleCommand, ...]


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_DEFAULT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*):-([^$`\"'\\]*)")
_BRACE_EXPANSION = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_WORD_END = " \t\r\n;&|<>()"
_RESERVED = frozenset(
    {
        "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
        "case", "esac", "function", "select", "time", "!", "[[", "coproc",
    }
)  # fmt: skip


def parse(src: str) -> CommandList:
    """Parse one logical command line.

    Raises :class:`Incomplete` when more input is needed and :class:`Unsupported` for
    syntax RepoProof refuses to run. An empty or comment-only line yields no commands.
    """
    tokens = _tokenize(src)
    commands: list[SimpleCommand] = []
    assignments: list[tuple[str, Word]] = []
    words: list[Word] = []
    redirects: list[Redirect] = []
    last_op = ""

    def finish() -> None:
        nonlocal assignments, words, redirects
        if words:
            first = words[0].plain_text
            if first in _RESERVED:
                raise Unsupported("shell control flow")
            if first in ("{", "}"):
                raise Unsupported("command groups")
        commands.append(SimpleCommand(tuple(assignments), tuple(words), tuple(redirects)))
        assignments, words, redirects = [], [], []

    for kind, value in tokens:
        if kind == "op":
            if assignments or words or redirects:
                finish()
            elif value == "&&":
                raise Unsupported("syntax error near '&&'")
            last_op = str(value)
            continue
        last_op = ""
        if kind == "redir":
            assert isinstance(value, Redirect)
            redirects.append(value)
            continue
        assert isinstance(value, Word)
        if not words:
            assignment = _as_assignment(value)
            if assignment is not None:
                assignments.append(assignment)
                continue
        words.append(value)

    if assignments or words or redirects:
        finish()
    elif last_op == "&&":
        raise Incomplete("command ends with '&&'")
    return CommandList(tuple(commands))


def expand_word(
    word: Word, env: Mapping[str, str], home: str, cwd: str | None = None
) -> list[str]:
    """Expand variables, ``~`` and (when ``cwd`` is given) globs. Never splits words."""
    text: list[str] = []
    pattern: list[str] = []
    wildcard = False
    for part in word.parts:
        if part.kind == "lit":
            text.append(part.text)
            if not part.quoted and any(ch in part.text for ch in "*?["):
                wildcard = True
                pattern.append(part.text)
            else:
                pattern.append(glob.escape(part.text))
            continue
        value = home if part.kind == "home" else _lookup(part, env)
        text.append(value)
        pattern.append(glob.escape(value))
    if wildcard and cwd is not None:
        matches = sorted(glob.glob("".join(pattern), root_dir=cwd))
        if matches:
            return matches
    return ["".join(text)]


def expand_text(word: Word, env: Mapping[str, str], home: str) -> str:
    """Expand a word to exactly one string (no globbing)."""
    return expand_word(word, env, home, None)[0]


def _lookup(part: Part, env: Mapping[str, str]) -> str:
    value = env.get(part.text)
    if (value is None or value == "") and part.default is not None:
        return part.default
    return value or ""


def _as_assignment(word: Word) -> tuple[str, Word] | None:
    if not word.parts:
        return None
    first = word.parts[0]
    if first.kind != "lit" or first.quoted:
        return None
    match = _ASSIGN.match(first.text)
    if match is None:
        return None
    rest = first.text[match.end() :]
    parts = ([Part(rest)] if rest else []) + list(word.parts[1:])
    return match.group(1), Word(tuple(parts))


def _tokenize(src: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r":
            i += 1
        elif c == "\n":
            tokens.append(("op", ";"))
            i += 1
        elif c == "#":
            while i < n and src[i] != "\n":
                i += 1
        elif c == ";":
            if src.startswith(";;", i):
                raise Unsupported("case statements")
            tokens.append(("op", ";"))
            i += 1
        elif c == "&":
            if src.startswith("&&", i):
                tokens.append(("op", "&&"))
                i += 2
            elif src.startswith("&>", i):
                i, redirect = _read_redirect(src, i, -1)
                tokens.append(("redir", redirect))
            else:
                raise Unsupported("background jobs ('&')")
        elif c == "|":
            raise Unsupported("'||' lists" if src.startswith("||", i) else "pipes")
        elif c in "()":
            raise Unsupported("subshells and grouping")
        elif c == "<":
            raise Unsupported("input redirection and here-documents")
        elif c == ">":
            i, redirect = _read_redirect(src, i, 1)
            tokens.append(("redir", redirect))
        elif c.isdigit() and i + 1 < n and src[i + 1] == ">":
            if c not in "12":
                raise Unsupported("file descriptor redirection")
            i, redirect = _read_redirect(src, i + 1, int(c))
            tokens.append(("redir", redirect))
        else:
            word, i = _read_word(src, i)
            if word.parts:  # a lone backslash-newline yields an empty word; drop it
                tokens.append(("word", word))
    return tokens


def _read_redirect(src: str, i: int, fd: int) -> tuple[int, Redirect]:
    n = len(src)
    if src.startswith("&>>", i):
        fd, mode, i = -1, ">>", i + 3
    elif src.startswith("&>", i):
        fd, mode, i = -1, ">", i + 2
    elif src.startswith(">>", i):
        mode, i = ">>", i + 2
    elif src.startswith(">&", i):
        i += 2
        if i < n and src[i] in "12" and (i + 1 >= n or src[i + 1] in " \t;&\n"):
            return i + 1, Redirect(fd, "dup", None, int(src[i]))
        raise Unsupported("file descriptor redirection")
    elif src.startswith(">|", i):
        raise Unsupported("clobbering redirection")
    else:
        mode, i = ">", i + 1
    while i < n and src[i] in " \t":
        i += 1
    if i >= n or src[i] in ";&|<>()\n":
        raise Unsupported("redirection without a target")
    word, i = _read_word(src, i)
    return i, Redirect(fd, mode, word, None)


def _read_word(src: str, i: int) -> tuple[Word, int]:
    n = len(src)
    start = i
    parts: list[Part] = []
    lit: list[str] = []

    def flush() -> None:
        if lit:
            parts.append(Part("".join(lit)))
            lit.clear()

    while i < n:
        c = src[i]
        if c in _WORD_END:
            break
        if c == "\\":
            if i + 1 >= n:
                raise Incomplete("trailing backslash")
            if src[i + 1] != "\n":  # backslash-newline is a line continuation
                flush()
                parts.append(Part(src[i + 1], quoted=True))
            i += 2
        elif c == "'":
            end = src.find("'", i + 1)
            if end == -1:
                raise Incomplete("unterminated single quote")
            flush()
            parts.append(Part(src[i + 1 : end], quoted=True))
            i = end + 1
        elif c == '"':
            flush()
            i = _read_double(src, i + 1, parts)
        elif c == "`":
            raise Unsupported("command substitution")
        elif c == "$":
            flush()
            part, i = _read_dollar(src, i, quoted=False)
            parts.append(part)
        elif c == "~" and i == start:
            nxt = src[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt == "/" or nxt in _WORD_END:
                flush()
                parts.append(Part("", kind="home"))
                i += 1
            else:
                raise Unsupported("tilde expansion")
        else:
            lit.append(c)
            i += 1
    flush()
    for part in parts:
        if part.kind == "lit" and not part.quoted and _BRACE_EXPANSION.search(part.text):
            raise Unsupported("brace expansion")
    return Word(tuple(parts)), i


def _read_double(src: str, i: int, parts: list[Part]) -> int:
    n = len(src)
    buf: list[str] = []
    emitted = False

    def flush() -> None:
        nonlocal emitted
        if buf:
            parts.append(Part("".join(buf), quoted=True))
            buf.clear()
            emitted = True

    while i < n:
        c = src[i]
        if c == '"':
            flush()
            if not emitted:
                parts.append(Part("", quoted=True))  # "" is a real, empty argument
            return i + 1
        if c == "\\" and i + 1 < n:
            nxt = src[i + 1]
            if nxt in '"\\$`':
                buf.append(nxt)
                i += 2
            elif nxt == "\n":
                i += 2
            else:
                buf.append("\\")
                i += 1
        elif c == "`":
            raise Unsupported("command substitution")
        elif c == "$":
            flush()
            part, i = _read_dollar(src, i, quoted=True)
            parts.append(part)
            emitted = True
        else:
            buf.append(c)
            i += 1
    raise Incomplete("unterminated double quote")


def _read_dollar(src: str, i: int, *, quoted: bool) -> tuple[Part, int]:
    n = len(src)
    nxt = src[i + 1] if i + 1 < n else ""
    if nxt == "(":
        raise Unsupported("command substitution")
    if nxt == "{":
        end = src.find("}", i + 2)
        if end == -1:
            raise Incomplete("unterminated '${'")
        body = src[i + 2 : end]
        if _NAME.fullmatch(body):
            return Part(body, kind="var", quoted=quoted), end + 1
        with_default = _DEFAULT.fullmatch(body)
        if with_default:
            part = Part(with_default.group(1), "var", quoted, with_default.group(2))
            return part, end + 1
        raise Unsupported("parameter expansion")
    if not quoted and nxt in ("'", '"'):
        raise Unsupported("ANSI-C and locale quoting")
    name = _NAME.match(src, i + 1)
    if name:
        return Part(name.group(), kind="var", quoted=quoted), name.end()
    if nxt and (nxt.isdigit() or nxt in "?!@*#$-"):
        raise Unsupported("special shell parameters")
    return Part("$", quoted=True), i + 1
