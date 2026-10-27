"""Data model shared by the checks and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How serious a finding is. ``--fail-on`` is compared against this."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"

    @property
    def rank(self) -> int:
        return {"error": 2, "warning": 1, "note": 0}[self.value]


@dataclass(frozen=True)
class RuleInfo:
    """Static description of a rule (used by ``repoproof rules`` and SARIF)."""

    id: str
    name: str
    severity: Severity
    summary: str
    help: str


def _rules(*rules: RuleInfo) -> dict[str, RuleInfo]:
    return {rule.id: rule for rule in rules}


RULES: dict[str, RuleInfo] = _rules(
    RuleInfo(
        "RP001",
        "docs-not-found",
        Severity.WARNING,
        "A documentation file to verify could not be found.",
        "RepoProof looks for README.md by default. Name the files to verify under "
        "`[repoproof] docs` in repoproof.toml.",
    ),
    RuleInfo(
        "RP101",
        "command-failed",
        Severity.ERROR,
        "A documented command exited with a non-zero status.",
        "A new developer following the documentation would be stuck at this step. Fix the "
        "command, the instructions around it, or the repository itself.",
    ),
    RuleInfo(
        "RP102",
        "command-timeout",
        Severity.ERROR,
        "A documented command did not finish in time.",
        "Raise `[sandbox] timeout` (or a per-phase value in `[sandbox.timeouts]`) if the step is "
        "legitimately slow. Otherwise the command probably waits for input or never exits "
        "(servers and file watchers do not belong in a verified journey).",
    ),
    RuleInfo(
        "RP103",
        "command-not-found",
        Severity.ERROR,
        "A documented command is not available in a clean environment.",
        "The executable is not installed and no earlier documented step installs it. "
        "Document the prerequisite or add the missing install step.",
    ),
    RuleInfo(
        "RP104",
        "environment-setup-failed",
        Severity.ERROR,
        "RepoProof could not prepare the clean environment.",
        "Usually the Python `venv` module is unavailable (Debian/Ubuntu: install "
        "`python3-venv`) or the temporary directory is not writable.",
    ),
    RuleInfo(
        "RP105",
        "isolation-unavailable",
        Severity.ERROR,
        "Strict isolation was requested but cannot be enforced on this host.",
        "Network restrictions need Linux with unprivileged user namespaces. Run on such a "
        "host, inside a disposable container or VM, or drop `strict`.",
    ),
    RuleInfo(
        "RP201",
        "broken-link",
        Severity.ERROR,
        "A relative link points to a file or directory that does not exist.",
        "Paths are checked case-sensitively, like GitHub does, so a link that works on a "
        "case-insensitive laptop can still be broken for everyone else.",
    ),
    RuleInfo(
        "RP202",
        "broken-anchor",
        Severity.WARNING,
        "A link points to a heading or anchor that does not exist.",
        "Anchors are derived from headings using GitHub's slug rules. Renaming a heading "
        "silently breaks every link to it.",
    ),
    RuleInfo(
        "RP301",
        "env-undocumented",
        Severity.WARNING,
        "An environment variable is read by the code but documented nowhere.",
        "Add it to the README or to .env.example. The finding is a warning when the code "
        "shows no default for the variable and a note otherwise.",
    ),
    RuleInfo(
        "RP302",
        "env-missing-from-example",
        Severity.WARNING,
        "A required variable is documented but missing from .env.example.",
        "People copy .env.example to get started. Variables that are missing from it fail "
        "at runtime after an otherwise successful setup.",
    ),
    RuleInfo(
        "RP303",
        "env-stale-example",
        Severity.NOTE,
        "A variable listed in .env.example is never read by the code or deployment config.",
        "Remove it, or ignore it with `[env] ignore` if a library reads it indirectly.",
    ),
    RuleInfo(
        "RP304",
        "env-not-in-deployment",
        Severity.NOTE,
        "A required variable is not set in the deployment configuration.",
        "Checked against docker-compose files, Dockerfiles and Kubernetes manifests that "
        "declare environment variables.",
    ),
)

STEP_STATUSES = ("passed", "failed", "timeout", "skipped", "not_run", "planned")


@dataclass
class Finding:
    """One problem found by a check."""

    rule_id: str
    message: str
    severity: Severity | None = None
    file: str | None = None
    line: int | None = None
    command: str | None = None
    exit_code: int | None = None
    output: str | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.rule_id not in RULES:
            raise ValueError(f"unknown rule id: {self.rule_id}")
        if self.severity is None:
            self.severity = RULES[self.rule_id].severity

    @property
    def rule(self) -> RuleInfo:
        return RULES[self.rule_id]

    @property
    def level(self) -> Severity:
        assert self.severity is not None
        return self.severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule.name,
            "severity": self.level.value,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
            "hint": self.hint,
        }


@dataclass
class StepResult:
    """What happened to one documented command line."""

    doc: str
    line: int
    command: str
    status: str
    reason: str | None = None
    phase: str | None = None
    exit_code: int | None = None
    duration: float = 0.0
    network: str | None = None
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc": self.doc,
            "line": self.line,
            "command": self.command,
            "status": self.status,
            "reason": self.reason,
            "phase": self.phase,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration, 3),
            "network": self.network,
            "output": self.output,
        }


@dataclass
class Report:
    """The complete result of one ``repoproof check`` run."""

    version: str
    root: str
    started_at: str
    duration: float
    dry_run: bool
    docs: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    isolation: dict[str, Any] = field(default_factory=dict)

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.level is severity)

    def summary(self) -> dict[str, Any]:
        steps = {status: 0 for status in STEP_STATUSES}
        for step in self.steps:
            steps[step.status] = steps.get(step.status, 0) + 1
        return {
            "errors": self.count(Severity.ERROR),
            "warnings": self.count(Severity.WARNING),
            "notes": self.count(Severity.NOTE),
            "steps": {"total": len(self.steps), **steps},
        }

    def exit_code(self, fail_on: str = "error") -> int:
        """0 when nothing at or above ``fail_on`` was found, otherwise 1."""
        if fail_on == "never":
            return 0
        threshold = Severity(fail_on).rank
        return 1 if any(f.level.rank >= threshold for f in self.findings) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tool": {"name": "repoproof", "version": self.version},
            "root": self.root,
            "started_at": self.started_at,
            "duration_seconds": round(self.duration, 3),
            "dry_run": self.dry_run,
            "docs": list(self.docs),
            "checks": list(self.checks),
            "summary": self.summary(),
            "isolation": self.isolation,
            "findings": [f.to_dict() for f in self.findings],
            "steps": [s.to_dict() for s in self.steps],
        }
