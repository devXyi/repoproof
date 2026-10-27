"""A journey: the commands a documentation file asks a new developer to run, in order.

The runner turns fenced shell blocks into logical command lines, parses them with
:mod:`repoproof.shellparse`, vets every simple command with :class:`repoproof.policy.Policy`
and then executes the approved ones inside a :class:`repoproof.sandbox.Sandbox`.

A line is verified only if *every* command on it is approved; otherwise the whole line is
reported as skipped and nothing on it runs. ``cd``, ``export``, ``source`` and friends are
emulated in-process, so state carries from one step to the next like a real terminal session
(including an activated virtualenv) without ever invoking a shell.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .markdown import MarkdownDoc
from .models import Finding, StepResult
from .policy import Decision, Policy, matches, path_inside
from .redact import Redactor
from .sandbox import IsolationError, RedirectSpec, Sandbox, SandboxError, build_base_env
from .shellparse import (
    CommandList,
    Incomplete,
    ShellError,
    SimpleCommand,
    Unsupported,
    expand_text,
    expand_word,
    parse,
)

SHELL_LANGS = frozenset({"sh", "bash", "shell", "zsh", "ksh"})
PROMPT_LANGS = frozenset({"console", "shell-session", "shellsession", "terminal"})
MAX_CONTINUATION_LINES = 30
BUILTINS = frozenset({"cd", "export", "unset", "set", "deactivate", "source", "."})
PHASE_ORDER = ("install", "build", "test", "run", "setup", "info")
PYTHON_TOOLS = re.compile(
    r"^(python[\d.]*|pip[\d.]*|pytest|py\.test|tox|nox|uv|poetry|pdm|hatch|pipenv|coverage"
    r"|ruff|black|mypy|flake8|isort|twine|build)$"
)
_NETWORK_FAILURE = re.compile(
    r"temporary failure in name resolution|name or service not known|network is unreachable"
    r"|could not resolve host|getaddrinfo|nodename nor servname|connection (?:refused|reset)"
    r"|max retries exceeded|no matching distribution found|failed to establish a new connection"
    r"|enotfound|eai_again|dial tcp|unable to access .*https?://|could not connect to",
    re.IGNORECASE,
)
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_ENV_ARG = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BLOCKED_ENV = frozenset({"BASH_ENV", "ENV", "PYTHONSTARTUP", "PYTHONHOME", "IFS", "SHELLOPTS", "PS4"})
_CLONE_VALUE_OPTIONS = frozenset(
    {"-b", "--branch", "--depth", "-o", "--origin", "-c", "--config", "--reference",
     "-j", "--jobs", "--filter", "--template", "--separate-git-dir"}
)  # fmt: skip


@dataclass
class RawCommand:
    """One logical command line found in a code block."""

    line: int
    text: str
    parsed: CommandList | None = None
    error: str | None = None
    skip_reason: str | None = None


def extract_commands(doc: MarkdownDoc) -> list[RawCommand]:
    """Find the runnable command lines in a document's fenced code blocks."""
    commands: list[RawCommand] = []
    for block in doc.code_blocks:
        prompt_only = block.lang in PROMPT_LANGS or block.lang == ""
        if not (prompt_only or block.lang in SHELL_LANGS):
            continue
        for command in _logical_lines(block.lines, prompt_only):
            if "skip" in block.directives:
                command.skip_reason = "skipped by <!-- repoproof:skip -->"
            commands.append(command)
    return commands


def _command_text(text: str, prompt_only: bool) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if prompt_only:
        if not stripped.startswith("$ "):
            return None  # command output, not a command
        stripped = stripped[2:].strip()
    else:
        if stripped.startswith("#"):
            return None  # comment or shebang
        if stripped.startswith("$ "):
            stripped = stripped[2:].strip()
    return stripped or None


def _logical_lines(entries: Sequence[tuple[int, str]], prompt_only: bool) -> list[RawCommand]:
    out: list[RawCommand] = []
    buffer: list[str] | None = None
    start = 0
    heredoc_end: str | None = None
    for number, text in entries:
        if heredoc_end is not None:
            if text.strip() == heredoc_end:
                heredoc_end = None
            continue
        if buffer is None:
            candidate = _command_text(text, prompt_only)
            if candidate is None:
                continue
            buffer, start = [candidate], number
        else:
            buffer.append(text)
        source = "\n".join(buffer)
        try:
            parsed = parse(source)
        except Incomplete as exc:
            if len(buffer) >= MAX_CONTINUATION_LINES:
                out.append(RawCommand(start, source.strip(), None, str(exc)))
                buffer = None
            continue
        except Unsupported as exc:
            out.append(RawCommand(start, source.strip(), None, str(exc)))
            heredoc = _HEREDOC.search(source)
            if heredoc:
                heredoc_end = heredoc.group(1)
            buffer = None
            continue
        if parsed.commands:
            out.append(RawCommand(start, source.strip(), parsed))
        buffer = None
    if buffer is not None:
        text = "\n".join(buffer).strip()
        out.append(RawCommand(start, text, None, "unterminated quote or line continuation"))
    return out


@dataclass
class Session:
    """Terminal-like state shared by the steps of a journey."""

    cwd: str
    env: dict[str, str]
    workspace: str
    home: str
    aliases: set[str] = field(default_factory=set)

    def fork(self) -> Session:
        return Session(self.cwd, dict(self.env), self.workspace, self.home, set(self.aliases))

    def adopt(self, other: Session) -> None:
        self.cwd, self.env, self.aliases = other.cwd, dict(other.env), set(other.aliases)


@dataclass
class Prepared:
    """A simple command after expansion and policy evaluation."""

    kind: str  # external | builtin | assign | clone
    argv: list[str] = field(default_factory=list)
    assigns: dict[str, str] = field(default_factory=dict)
    redirects: RedirectSpec | None = None
    decision: Decision | None = None
    skip_kind: str | None = None  # skip | blocked | unlisted | unsupported
    reason: str | None = None


@dataclass
class Outcome:
    """Why a step did not pass."""

    status: str  # failed | timeout | not_found | env_error | isolation
    command: str
    exit_code: int | None = None
    output: str = ""
    message: str = ""
    hint: str | None = None
    network: str | None = None


@dataclass
class Applied:
    outcome: Outcome | None = None
    duration: float = 0.0
    network: str | None = None


class _StepFailure(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def _blocked_env(name: str) -> bool:
    return name in _BLOCKED_ENV or name.startswith(("LD_", "DYLD_"))


@dataclass
class JourneyResult:
    steps: list[StepResult]
    findings: list[Finding]
    isolation: dict[str, Any]
    workspace: str | None = None


class JourneyRunner:
    """Plans and (unless ``sandbox`` is ``None``) executes one document's commands."""

    def __init__(
        self,
        *,
        doc: str,
        commands: Sequence[RawCommand],
        config: Config,
        policy: Policy,
        redactor: Redactor,
        sandbox: Sandbox | None,
        base_env: Mapping[str, str],
        workspace: str,
        deadline: float,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.doc = doc
        self.commands = list(commands)
        self.config = config
        self.policy = policy
        self.redactor = redactor
        self.sandbox = sandbox
        self.base_env = dict(base_env)
        self.workspace = workspace
        self.deadline = deadline
        self.progress = progress
        self._skip_notes: list[str] = []

    # -- main loop ---------------------------------------------------------------------

    def run(self) -> tuple[list[StepResult], list[Finding]]:
        session = Session(
            cwd=self.workspace,
            env=dict(self.base_env),
            workspace=self.workspace,
            home=self.base_env.get("HOME", "/"),
        )
        steps: list[StepResult] = []
        findings: list[Finding] = []
        halted = False
        for raw in self.commands:
            label = self.redactor.redact(raw.text)
            result = StepResult(doc=self.doc, line=raw.line, command=label, status="skipped")
            steps.append(result)
            if halted:
                result.status, result.reason = "not_run", "an earlier step failed"
                continue
            if self.progress and self.sandbox is not None:
                self.progress(f"{self.doc}:{raw.line}  {label.splitlines()[0]}")
            outcome = self._process_line(raw, session, result)
            if outcome is None:
                continue
            findings.append(self._finding(result, outcome))
            if outcome.status in ("env_error", "isolation") or not self.config.sandbox.keep_going:
                halted = True
        return steps, findings

    def _process_line(self, raw: RawCommand, session: Session, result: StepResult) -> Outcome | None:
        if raw.error is not None:
            return self._skip(result, f"unsupported shell syntax: {raw.error}", "unsupported")
        if raw.skip_reason is not None:
            return self._skip(result, raw.skip_reason, "skip")
        assert raw.parsed is not None
        commands = self._rewrite(raw.parsed.commands)

        planning = session.fork()
        plan: list[Prepared] = []
        for command in commands:
            prepared = self._prepare(command, planning, resolve=False)
            if prepared.skip_kind is not None:
                return self._skip(result, prepared.reason or "not allowed", prepared.skip_kind)
            applied = self._apply(prepared, planning, execute=False)
            if applied.outcome is not None:
                return self._skip(result, applied.outcome.message, "blocked")
            plan.append(prepared)
        result.phase = _dominant_phase(plan)

        if self.sandbox is None:  # dry run: keep the simulated state for later lines
            session.adopt(planning)
            result.status = "planned"
            if all(p.kind == "clone" for p in plan):
                result.status, result.reason = "skipped", "replaced by the local checkout"
            return None

        total, network = 0.0, None
        for command in commands:
            prepared = self._prepare(command, session, resolve=True)
            if prepared.skip_kind is not None:
                shown = " ".join(prepared.argv) or result.command
                outcome = Outcome("failed", shown, message=prepared.reason or "blocked")
                return self._failed(result, outcome, total, network)
            applied = self._apply(prepared, session, execute=True)
            total += applied.duration
            network = applied.network or network
            if applied.outcome is not None:
                return self._failed(result, applied.outcome, total, network)
        result.status, result.exit_code, result.duration, result.network = "passed", 0, total, network
        if all(p.kind == "clone" for p in plan):
            result.status, result.reason = "skipped", "replaced by the local checkout"
        return None

    # -- bookkeeping -------------------------------------------------------------------

    def _skip(self, result: StepResult, reason: str, kind: str) -> None:
        result.status, result.reason = "skipped", reason
        if kind in ("unlisted", "blocked", "unsupported"):
            self._skip_notes.append(f"`{result.command.splitlines()[0]}` ({self.doc}:{result.line})")
        return None

    def _failed(
        self, result: StepResult, outcome: Outcome, duration: float, network: str | None
    ) -> Outcome:
        result.status = "timeout" if outcome.status == "timeout" else "failed"
        result.exit_code = outcome.exit_code
        result.duration = duration
        result.network = outcome.network or network
        result.reason = outcome.message or None
        result.output = outcome.output or None
        return outcome

    def _finding(self, result: StepResult, outcome: Outcome) -> Finding:
        command = outcome.command
        hints = [outcome.hint] if outcome.hint else []
        if outcome.status == "timeout":
            rule, message = "RP102", f"`{command}` {outcome.message or 'did not finish in time'}"
        elif outcome.status == "not_found":
            rule = "RP103"
            message = f"`{command.split()[0] if command else '?'}` is not available in a clean environment"
            hints.append("Install it in an earlier documented step, or list it as a prerequisite.")
        elif outcome.status == "env_error":
            rule, message = "RP104", outcome.message
        elif outcome.status == "isolation":
            rule, message = "RP105", outcome.message
        else:
            rule = "RP101"
            code = outcome.exit_code
            if code is None:
                message = f"`{command}` failed: {outcome.message}"
            elif code < 0:
                message = f"`{command}` was terminated by signal {-code}"
            else:
                message = f"`{command}` exited with status {code}"
        looks_offline = _NETWORK_FAILURE.search(outcome.output or "") is not None
        if looks_offline and outcome.status in ("failed", "timeout"):
            if outcome.network == "denied":
                hints.append(
                    "This step ran without network access ([sandbox] network = "
                    f'"{self.config.sandbox.network}"). If it needs the network, mark it with '
                    '`network = true` in [commands] allow, or set [sandbox] network = "allow".'
                )
            else:
                hints.append(
                    "The step could not reach the network or a package index. Check connectivity, "
                    "or pass proxy settings through with [sandbox] pass_env."
                )
        if self._skip_notes and rule in ("RP101", "RP102", "RP103"):
            shown = ", ".join(self._skip_notes[:3])
            hints.append(f"Skipped earlier, and possibly needed: {shown}.")
        return Finding(
            rule,
            message,
            file=self.doc,
            line=result.line,
            command=command or None,
            exit_code=outcome.exit_code,
            output=outcome.output or None,
            hint=" ".join(hints) or None,
        )

    # -- rewriting and preparing -------------------------------------------------------

    def _rewrite(self, commands: Sequence[SimpleCommand]) -> list[SimpleCommand]:
        if not self.config.rewrite:
            return list(commands)
        out: list[SimpleCommand] = []
        for command in commands:
            shown = " ".join(word.display() for word in command.words)
            for rule in self.config.rewrite:
                if matches(rule.match, shown):
                    out.extend(parse(rule.run).commands)
                    break
            else:
                out.append(command)
        return out

    def _prepare(self, command: SimpleCommand, session: Session, *, resolve: bool) -> Prepared:
        env, home = session.env, session.home
        assigns = {name: expand_text(word, env, home) for name, word in command.assignments}
        for name in assigns:
            if _blocked_env(name):
                return Prepared("assign", skip_kind="blocked", reason=f"refuses to set {name}")
        if not command.words:
            if command.redirects:
                return Prepared("assign", skip_kind="unsupported", reason="redirection without a command")
            return Prepared("assign", assigns=assigns)
        argv: list[str] = []
        for word in command.words:
            argv.extend(expand_word(word, env, home, session.cwd))
        name = argv[0]
        if name in BUILTINS:
            if command.redirects:
                return Prepared("builtin", argv, skip_kind="unsupported", reason=f"redirecting `{name}` is not supported")
            return Prepared("builtin", argv)
        if name == "git" and len(argv) > 1 and argv[1] == "clone":
            return Prepared("clone", argv)
        decision = self.policy.decide(argv, cwd=session.cwd, workspace=session.workspace, resolve=resolve)
        if decision.verdict != "allow":
            return Prepared("external", argv, decision=decision, skip_kind=decision.verdict, reason=decision.reason)
        redirects, problem = self._redirects(command, session, resolve)
        if problem:
            return Prepared("external", argv, decision=decision, skip_kind="blocked", reason=problem)
        return Prepared("external", argv, assigns, redirects, decision)

    def _redirects(
        self, command: SimpleCommand, session: Session, resolve: bool
    ) -> tuple[RedirectSpec | None, str | None]:
        if not command.redirects:
            return None, None
        targets: dict[int, Any] = {1: "capture", 2: "capture"}
        for redirect in command.redirects:
            if redirect.mode == "dup":
                if redirect.fd == 2 and redirect.dup_of == 1:
                    targets[2] = targets[1]
                elif redirect.fd == 1 and redirect.dup_of == 2:
                    targets[1] = targets[2]
                else:
                    return None, "unsupported file descriptor redirection"
                continue
            assert redirect.target is not None
            paths = expand_word(redirect.target, session.env, session.home, session.cwd)
            if len(paths) != 1:
                return None, "ambiguous redirection target"
            path = paths[0]
            if path == "/dev/null":
                spec: Any = "null"
            elif path_inside(path, session.cwd, session.workspace, resolve=resolve):
                absolute = os.path.normpath(os.path.join(session.cwd, path))
                spec = ("file", absolute, redirect.mode == ">>")
            else:
                return None, f"redirection target '{path}' is outside the workspace"
            for fd in (1, 2) if redirect.fd == -1 else (redirect.fd,):
                targets[fd] = spec
        return RedirectSpec(targets[1], targets[2]), None

    # -- applying ----------------------------------------------------------------------

    def _apply(self, prepared: Prepared, session: Session, *, execute: bool) -> Applied:
        if prepared.kind == "assign":
            session.env.update(prepared.assigns)
            return Applied()
        if prepared.kind == "clone":
            self._clone(prepared.argv, session)
            return Applied()
        if prepared.kind == "builtin":
            return self._builtin(prepared, session, execute)
        return self._external(prepared, session) if execute else Applied()

    def _builtin(self, prepared: Prepared, session: Session, execute: bool) -> Applied:
        name, args = prepared.argv[0], prepared.argv[1:]
        try:
            if name == "cd":
                self._cd(args, session, execute)
            elif name == "export":
                self._export(args, session)
            elif name == "unset":
                for arg in args:
                    session.env.pop(arg, None)
            elif name in ("source", "."):
                self._source(args, session, execute)
        except _StepFailure as exc:
            shown = " ".join(prepared.argv)
            return Applied(Outcome("failed", shown, None, "", str(exc), exc.hint))
        return Applied()

    def _cd(self, args: list[str], session: Session, execute: bool) -> None:
        if len(args) != 1 or args[0].startswith("-"):
            raise _StepFailure("cd: expected exactly one directory")
        arg = args[0]
        target = self._cd_target(arg, session)
        if not path_inside(target, session.cwd, session.workspace, resolve=execute):
            raise _StepFailure(f"cd: '{arg}' is outside the workspace")
        if not execute:
            session.cwd = os.path.normpath(target)
            return
        if not os.path.isdir(target):
            raise _StepFailure(f"cd: no such directory: {arg}", _suggest(arg, session.cwd))
        session.cwd = os.path.realpath(target)

    @staticmethod
    def _cd_target(arg: str, session: Session) -> str:
        if os.path.isabs(arg):
            return arg
        parts = [p for p in arg.split("/") if p not in ("", ".")]
        if parts and parts[0] in session.aliases and not os.path.exists(os.path.join(session.cwd, parts[0])):
            return os.path.join(session.workspace, *parts[1:])  # `cd <cloned dir>`
        return os.path.join(session.cwd, arg)

    @staticmethod
    def _export(args: list[str], session: Session) -> None:
        for arg in args:
            match = _ENV_ARG.fullmatch(arg)
            if match:
                _set_env(session, match.group(1), match.group(2))
            elif not _ENV_NAME.fullmatch(arg):
                raise _StepFailure(f"export: unsupported argument '{arg}'")

    def _source(self, args: list[str], session: Session, execute: bool) -> None:
        if not args:
            raise _StepFailure("source: a file name is required")
        arg = args[0]
        target = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(session.cwd, arg))
        if not path_inside(target, session.cwd, session.workspace, resolve=execute):
            raise _StepFailure(f"source: '{arg}' is outside the workspace")
        is_activate = os.path.basename(target).startswith("activate")
        if execute and not os.path.isfile(target):
            raise _StepFailure(f"source: no such file: {arg}", _suggest(arg, session.cwd))
        if is_activate:
            _activate(session, os.path.dirname(os.path.dirname(target)))
        elif execute:
            _source_env_file(target, arg, session)

    @staticmethod
    def _clone(argv: list[str], session: Session) -> None:
        positional: list[str] = []
        i = 2
        while i < len(argv):
            arg = argv[i]
            if arg in _CLONE_VALUE_OPTIONS:
                i += 2
                continue
            if not arg.startswith("-"):
                positional.append(arg)
            i += 1
        if not positional:
            return
        target = positional[1] if len(positional) > 1 else re.split(r"[/:]", positional[0].rstrip("/"))[-1]
        target = target[:-4] if target.endswith(".git") else target
        name = os.path.basename(target.rstrip("/"))
        if name:
            session.aliases.add(name)

    def _tidy(self, text: str) -> str:
        """Hide the temporary directory layout so output reads like a normal checkout."""
        assert self.sandbox is not None
        work, root = str(self.sandbox.work), str(self.sandbox.root)
        return text.replace(work + "/", "").replace(work, ".").replace(root, "<sandbox>")

    def _external(self, prepared: Prepared, session: Session) -> Applied:
        assert self.sandbox is not None and prepared.decision is not None
        argv, decision = prepared.argv, prepared.decision
        shown = self.redactor.redact(" ".join(argv))
        if self.sandbox.python_enabled and "VIRTUAL_ENV" not in session.env and _needs_venv(argv):
            try:
                _activate(session, str(self.sandbox.ensure_venv()))
            except SandboxError as exc:
                return Applied(Outcome("env_error", shown, message=str(exc)))
        env = dict(session.env)
        env.update(prepared.assigns)
        executable = _find_executable(argv[0], env, session.cwd)
        if executable is None:
            return Applied(Outcome("not_found", shown, 127, message=f"`{argv[0]}` was not found"))
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            message = "the overall time budget ([sandbox] total_timeout) is used up"
            return Applied(Outcome("timeout", shown, message=message))
        sandbox_cfg = self.config.sandbox
        limit = decision.timeout or sandbox_cfg.timeouts.get(decision.phase, sandbox_cfg.timeout)
        timeout = min(float(limit), remaining)
        try:
            result = self.sandbox.run(
                [executable, *argv[1:]],
                cwd=session.cwd,
                env=env,
                timeout=timeout,
                network=Policy.network_allowed(decision, sandbox_cfg.network),
                redirects=prepared.redirects,
            )
        except IsolationError as exc:
            return Applied(Outcome("isolation", shown, message=str(exc)))
        output = self._tidy(self.redactor.redact(result.output))
        if result.timed_out:
            message = f"did not finish within {int(timeout)}s"
            outcome = Outcome("timeout", shown, None, output, message, None, result.network)
            return Applied(outcome, result.duration, result.network)
        if result.exit_code != 0:
            outcome = Outcome("failed", shown, result.exit_code, output, "", None, result.network)
            return Applied(outcome, result.duration, result.network)
        return Applied(None, result.duration, result.network)


# -- helpers ---------------------------------------------------------------------------


def _dominant_phase(plan: Sequence[Prepared]) -> str | None:
    phases = {p.decision.phase for p in plan if p.decision is not None}
    for phase in PHASE_ORDER:
        if phase in phases:
            return phase
    return "setup" if plan else None


def _needs_venv(argv: Sequence[str]) -> bool:
    if not PYTHON_TOOLS.match(os.path.basename(argv[0])):
        return False
    return not (len(argv) >= 3 and argv[1] == "-m" and argv[2] == "venv")


def _find_executable(name: str, env: Mapping[str, str], cwd: str) -> str | None:
    if "/" in name:
        path = name if os.path.isabs(name) else os.path.join(cwd, name)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which(name, path=env.get("PATH", ""))


def _suggest(path: str, cwd: str) -> str | None:
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts:
        return None
    try:
        names = os.listdir(cwd)
    except OSError:
        return None
    close = difflib.get_close_matches(parts[0], names, n=1, cutoff=0.6)
    if not close or close[0] == parts[0]:
        return None
    return f"Did you mean `{'/'.join([close[0], *parts[1:]])}`?"


def _set_env(session: Session, name: str, value: str) -> None:
    if _blocked_env(name):
        raise _StepFailure(f"refusing to set {name}")
    session.env[name] = value


def _activate(session: Session, venv_dir: str) -> None:
    """Emulate ``source <venv>/bin/activate``."""
    bin_dir = os.path.join(venv_dir, "bin")
    entries = [e for e in session.env.get("PATH", "").split(os.pathsep) if e]
    previous = session.env.get("VIRTUAL_ENV")
    if previous:
        old_bin = os.path.normpath(os.path.join(previous, "bin"))
        entries = [e for e in entries if os.path.normpath(e) != old_bin]
    session.env["PATH"] = os.pathsep.join([bin_dir, *entries])
    session.env["VIRTUAL_ENV"] = venv_dir
    session.env.pop("PYTHONHOME", None)


def _source_env_file(path: str, shown: str, session: Session) -> None:
    """Emulate ``source .env`` for files that contain only assignments and ``export``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _StepFailure(f"source: cannot read {shown}: {exc}") from exc
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parsed = parse(stripped)
        except ShellError as exc:
            raise _StepFailure(f"source: {shown}:{number}: unsupported syntax ({exc})") from exc
        for command in parsed.commands:
            for name, word in command.assignments:
                _set_env(session, name, expand_text(word, session.env, session.home))
            if not command.words:
                continue
            words = [expand_text(w, session.env, session.home) for w in command.words]
            if words[0] != "export":
                raise _StepFailure(f"source: {shown}:{number}: only assignments and `export` are supported")
            for arg in words[1:]:
                match = _ENV_ARG.fullmatch(arg)
                if match:
                    _set_env(session, match.group(1), match.group(2))


@dataclass
class JourneyInputs:
    """Everything :func:`run_journey` needs besides the document itself."""

    root: Path
    config: Config
    policy: Policy
    redactor: Redactor
    deadline: float
    dry_run: bool = False
    tracked_only: bool = False
    keep: bool = False
    progress: Callable[[str], None] | None = None
    host_env: Mapping[str, str] = field(default_factory=lambda: os.environ)


def run_journey(doc: MarkdownDoc, inputs: JourneyInputs) -> JourneyResult:
    """Run (or, for a dry run, plan) every documented command in ``doc``."""
    commands = extract_commands(doc)
    cfg = inputs.config
    idle = {"backend": "none", "network_policy": cfg.sandbox.network, "warnings": []}
    if not commands:
        return JourneyResult([], [], idle)
    if inputs.dry_run:
        base = build_base_env(
            inputs.host_env,
            home="/sandbox/home",
            tmp="/sandbox/tmp",
            pass_env=cfg.sandbox.pass_env,
            inherit_proxy=cfg.sandbox.inherit_proxy,
        )
        runner = JourneyRunner(
            doc=doc.path, commands=commands, config=cfg, policy=inputs.policy,
            redactor=inputs.redactor, sandbox=None, base_env=base, workspace="/workspace",
            deadline=inputs.deadline,
        )  # fmt: skip
        steps, findings = runner.run()
        return JourneyResult(steps, findings, {**idle, "dry_run": True})
    try:
        with Sandbox(
            inputs.root, cfg.sandbox, tracked_only=inputs.tracked_only,
            keep=inputs.keep, host_env=inputs.host_env,
        ) as sandbox:  # fmt: skip
            runner = JourneyRunner(
                doc=doc.path, commands=commands, config=cfg, policy=inputs.policy,
                redactor=inputs.redactor, sandbox=sandbox, base_env=sandbox.base_env,
                workspace=str(sandbox.work), deadline=inputs.deadline, progress=inputs.progress,
            )  # fmt: skip
            steps, findings = runner.run()
            kept = str(sandbox.root) if inputs.keep else None
            return JourneyResult(steps, findings, sandbox.describe(), kept)
    except SandboxError as exc:
        finding = Finding("RP104", str(exc), file=doc.path)
        return JourneyResult([], [finding], idle)
