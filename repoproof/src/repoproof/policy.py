"""The command policy: which documented commands RepoProof is willing to run.

Every simple command is classified as one of

``allow``     matches a built-in rule, a ``[commands] allow`` pattern, or a command the
              project itself ships (``[project.scripts]``); it runs;
``skip``      matches ``[commands] skip``; it is reported as skipped;
``blocked``   privilege escalation, system package managers and similar; it never runs and
              no configuration can allow it;
``unlisted``  anything else; it is reported as skipped until someone approves it.

The allowlist is a safety rail against *documentation* hazards (``sudo``, ``rm -rf``,
``curl | sh``). It is not a sandbox: an approved ``pip install`` or ``pytest`` still executes
the repository's own code. See docs/security-model.md.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .config import AllowRule, Config

_PYTHON = re.compile(r"^python(\d+(\.\d+)*)?$")
_PIP = re.compile(r"^pip(\d+(\.\d+)*)?$")
_MODE = re.compile(r"^(?:[ugoa]*[-+=][rwxXst]+(?:,[ugoa]*[-+=][rwxXst]+)*|[0-7]{3,4})$")

#: Executables that are never run, with the reason shown to the user.
DENIED: dict[str, str] = {
    "sudo": "privilege escalation",
    "su": "privilege escalation",
    "doas": "privilege escalation",
    "pkexec": "privilege escalation",
    "chown": "changes file ownership",
    "chgrp": "changes file ownership",
    "mount": "changes system mounts",
    "umount": "changes system mounts",
    "dd": "raw device access",
    "fdisk": "raw device access",
    "parted": "raw device access",
    "shutdown": "powers the machine off",
    "reboot": "powers the machine off",
    "halt": "powers the machine off",
    "poweroff": "powers the machine off",
    "systemctl": "manages system services",
    "service": "manages system services",
    "crontab": "changes scheduled jobs",
    "kill": "signals arbitrary processes",
    "killall": "signals arbitrary processes",
    "pkill": "signals arbitrary processes",
    "eval": "evaluates arbitrary shell text",
    "exec": "replaces the process",
    "apt": "system package manager",
    "apt-get": "system package manager",
    "dpkg": "system package manager",
    "yum": "system package manager",
    "dnf": "system package manager",
    "rpm": "system package manager",
    "apk": "system package manager",
    "pacman": "system package manager",
    "zypper": "system package manager",
    "snap": "system package manager",
    "brew": "system package manager",
}

#: Commands that only touch the files named in their arguments. Their path arguments must
#: stay inside the workspace.
PATH_COMMANDS = frozenset({"cp", "mv", "rm", "mkdir", "touch", "cat", "ls", "chmod"})

#: Commands with no side effects outside their own output.
HARMLESS = frozenset({"echo", "printf", "pwd", "true", "false"})


@dataclass(frozen=True)
class Rule:
    """A command pattern. ``*`` is the only wildcard and matches anything."""

    pattern: str
    phase: str = "run"
    network: bool = False
    timeout: int | None = None
    source: str = "builtin"


@dataclass(frozen=True)
class Decision:
    verdict: str  # allow | skip | blocked | unlisted
    reason: str
    phase: str = "run"
    network: bool = False
    timeout: int | None = None


def _command(prefix: str, phase: str, network: bool = False) -> tuple[Rule, Rule]:
    """A rule for ``prefix`` on its own and for ``prefix`` followed by any arguments."""
    return Rule(prefix, phase, network), Rule(prefix + " *", phase, network)


def _builtin_rules() -> tuple[Rule, ...]:
    groups = [
        # Python
        _command("python -m venv", "setup"),
        _command("python -m pip install", "install", True),
        _command("pip install", "install", True),
        _command("pip list", "info"),
        _command("pip show", "info"),
        _command("pip freeze", "info"),
        _command("pip --version", "info"),
        _command("python --version", "info"),
        _command("python -m build", "build", True),
        _command("python -m pytest", "test"),
        _command("pytest", "test"),
        _command("tox", "test", True),
        _command("nox", "test", True),
        _command("uv venv", "setup"),
        _command("uv sync", "install", True),
        _command("uv pip install", "install", True),
        _command("uv run pytest", "test", True),
        _command("poetry install", "install", True),
        _command("poetry run pytest", "test"),
        _command("pdm install", "install", True),
        # Node.js
        _command("npm install", "install", True),
        _command("npm ci", "install", True),
        _command("npm test", "test"),
        _command("npm run build", "build"),
        _command("npm run test", "test"),
        _command("yarn install", "install", True),
        _command("yarn test", "test"),
        _command("yarn build", "build"),
        _command("pnpm install", "install", True),
        _command("pnpm test", "test"),
        _command("pnpm build", "build"),
        # Go
        _command("go mod download", "install", True),
        _command("go build", "build", True),
        _command("go test", "test", True),
        _command("go vet", "test", True),
        _command("go run", "run", True),
        # Rust
        _command("cargo fetch", "install", True),
        _command("cargo build", "build", True),
        _command("cargo check", "build", True),
        _command("cargo test", "test", True),
        _command("cargo run", "run", True),
        # Make
        _command("make", "build"),
    ]
    return tuple(rule for group in groups for rule in group)


BUILTIN_RULES = _builtin_rules()


def matches(pattern: str, text: str) -> bool:
    """Match ``text`` against ``pattern`` where ``*`` matches anything and all else is literal."""
    regex = ".*".join(re.escape(piece) for piece in pattern.split("*"))
    return re.fullmatch(regex, text, re.DOTALL) is not None


def canonical_name(argv0: str) -> str:
    """``python3.12`` and ``pip3`` become ``python`` and ``pip``. Other executables invoked
    through a path keep that path, so ``./rm`` can never impersonate the system ``rm``."""
    base = os.path.basename(argv0)
    if _PYTHON.match(base):
        return "python"
    if _PIP.match(base):
        return "pip"
    return argv0 if "/" in argv0 else base


def path_inside(path: str, cwd: str, workspace: str, *, resolve: bool) -> bool:
    """True if ``path`` (relative to ``cwd``) lies within ``workspace``.

    With ``resolve`` symlinks are followed, so a link that points out of the workspace is
    rejected. Without it the check is purely lexical (used while planning).
    """
    if "\x00" in path:
        return False
    joined = path if os.path.isabs(path) else os.path.join(cwd, path)
    joined = os.path.normpath(joined)
    base = os.path.normpath(workspace)
    if resolve:
        joined, base = os.path.realpath(joined), os.path.realpath(base)
    return joined == base or joined.startswith(base + os.sep)


def vet_path_args(
    name: str, args: Sequence[str], cwd: str, workspace: str, *, resolve: bool
) -> str | None:
    """Return a reason if a path-touching command reaches outside the workspace."""
    options: list[str] = []
    paths: list[str] = []
    after_dashes = False
    for arg in args:
        if after_dashes:
            paths.append(arg)
        elif arg == "--":
            after_dashes = True
        elif arg.startswith("-") and arg != "-":
            options.append(arg)
        else:
            paths.append(arg)
    for option in options:
        if option in ("-t", "-T", "--reference") or option.startswith(
            ("--target-directory", "--reference=")
        ):
            return f"`{name} {option}` is not supported"
    if name == "chmod" and paths and _MODE.match(paths[0]):
        paths = paths[1:]
    for path in paths:
        if not path_inside(path, cwd, workspace, resolve=resolve):
            return f"`{name}` touches '{path}', which is outside the workspace"
    return None


@dataclass
class Policy:
    """Decides whether one expanded command may run."""

    allow: list[AllowRule] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    project_scripts: frozenset[str] = frozenset()
    project_modules: frozenset[str] = frozenset()

    @classmethod
    def from_config(
        cls,
        config: Config,
        project_scripts: Iterable[str] = (),
        project_modules: Iterable[str] = (),
    ) -> Policy:
        return cls(
            allow=list(config.allow),
            skip=list(config.skip),
            project_scripts=frozenset(project_scripts),
            project_modules=frozenset(project_modules),
        )

    def decide(
        self, argv: Sequence[str], *, cwd: str, workspace: str, resolve: bool = False
    ) -> Decision:
        if not argv:
            return Decision("unlisted", "empty command")
        name = canonical_name(argv[0])
        base = os.path.basename(argv[0])

        reason = DENIED.get(base)
        if reason is None and base.startswith("mkfs"):
            reason = "creates file systems"
        if reason is not None:
            return Decision("blocked", f"`{base}` is never run ({reason})")

        raw = " ".join(argv)
        canonical = " ".join([name, *argv[1:]])
        for pattern in self.skip:
            if matches(pattern, raw) or matches(pattern, canonical):
                return Decision("skip", "skipped by [commands] skip")

        if name in PATH_COMMANDS:
            problem = vet_path_args(name, argv[1:], cwd, workspace, resolve=resolve)
            if problem:
                return Decision("blocked", problem)
            return Decision("allow", "built-in file command", phase="setup")
        if name in HARMLESS:
            return Decision("allow", "harmless built-in", phase="info")

        for rule in self.allow:
            if matches(rule.pattern, raw) or matches(rule.pattern, canonical):
                network = bool(rule.network)
                return Decision("allow", "allowed by [commands] allow", rule.phase, network, rule.timeout)
        for rule in BUILTIN_RULES:
            if matches(rule.pattern, canonical):
                return Decision("allow", "built-in rule", rule.phase, rule.network, rule.timeout)

        if name == "python" and len(argv) > 1:
            if argv[1].endswith(".py") and not argv[1].startswith("-"):
                return Decision("allow", "runs a script from the repository", phase="run")
            if argv[1] == "-m" and len(argv) > 2:
                module = argv[2].split(".")[0].replace("-", "_")
                if module in self.project_modules:
                    return Decision("allow", "runs the project's own module", phase="run")
        if name in self.project_scripts:
            return Decision("allow", "a command shipped by this project", phase="run")

        return Decision(
            "unlisted",
            f"`{name}` is not on the allowlist (add a pattern to [commands] allow to verify it)",
        )

    @staticmethod
    def network_allowed(decision: Decision, mode: str) -> bool:
        """Whether a step may use the network under ``[sandbox] network = mode``."""
        if mode == "allow":
            return True
        if mode == "deny":
            return False
        return decision.network
