"""Runs the checks and assembles the :class:`~repoproof.models.Report`."""

from __future__ import annotations

import os
import posixpath
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .checks.env import check_env
from .checks.links import check_links
from .config import CHECKS, Config, read_toml
from .journey import JourneyInputs, run_journey
from .markdown import MarkdownDoc, parse_markdown
from .models import Finding, Report, Severity, StepResult
from .policy import Policy
from .redact import Redactor

README_NAMES = ("README.md", "Readme.md", "readme.md", "README.markdown", "README.MD")


@dataclass
class CheckOptions:
    """Run-time switches that are not part of ``repoproof.toml``."""

    only: frozenset[str] | None = None
    dry_run: bool = False
    tracked_only: bool = False
    keep_workspace: bool = False
    progress: Callable[[str], None] | None = None
    host_env: Mapping[str, str] | None = None


def discover_docs(root: Path, config: Config) -> tuple[dict[str, MarkdownDoc], list[Finding]]:
    """Load the documentation files to verify (``README.md`` unless configured)."""
    findings: list[Finding] = []
    names = list(config.docs)
    explicit = bool(names)
    if not explicit:
        for candidate in README_NAMES:
            if (root / candidate).is_file():
                names = [candidate]
                break
    docs: dict[str, MarkdownDoc] = {}
    if not names:
        findings.append(
            Finding(
                "RP001",
                "No README was found, so there are no documented steps to verify.",
                hint="Add a README.md, or name the files to verify under [repoproof] docs.",
            )
        )
    for name in names:
        relative = posixpath.normpath(name.replace("\\", "/"))
        path = root / relative
        if relative.startswith(("..", "/")):
            findings.append(
                Finding("RP001", f"'{name}' is outside the repository.", Severity.ERROR)
            )
        elif not path.is_file():
            severity = Severity.ERROR if explicit else None
            findings.append(Finding("RP001", f"Documentation file '{name}' was not found.", severity))
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            docs[relative] = parse_markdown(text, relative)
    return docs, findings


def project_info(root: Path) -> tuple[set[str], set[str]]:
    """Console scripts and importable module names the project itself ships."""
    scripts: set[str] = set()
    modules: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = read_toml(pyproject)
        project = data.get("project", {})
        if isinstance(project, dict):
            for key in ("scripts", "gui-scripts"):
                table = project.get(key, {})
                if isinstance(table, dict):
                    scripts.update(str(name) for name in table)
            name = project.get("name")
            if isinstance(name, str):
                modules.add(name.replace("-", "_"))
        poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
        if isinstance(poetry, dict) and isinstance(poetry.get("scripts"), dict):
            scripts.update(str(name) for name in poetry["scripts"])
    return scripts, modules


def _supports_execution() -> bool:
    """Documented commands can only be run (and cleaned up) on POSIX systems."""
    return os.name == "posix"


def _enabled(config: Config, options: CheckOptions) -> set[str]:
    if options.only is not None:
        return set(options.only)
    enabled = {"commands"}
    if config.links.enabled:
        enabled.add("links")
    if config.env.enabled:
        enabled.add("env")
    return enabled


def _merge_isolation(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if not current:
        return dict(new)
    merged = dict(current)
    warnings = list(current.get("warnings", []))
    warnings += [w for w in new.get("warnings", []) if w not in warnings]
    merged["warnings"] = warnings
    for key, value in new.items():
        if key != "warnings" and key not in merged:
            merged[key] = value
    return merged


def run_check(root: Path, config: Config, options: CheckOptions | None = None) -> Report:
    """Run every enabled check against the repository at ``root``."""
    options = options or CheckOptions()
    root = root.resolve()
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    docs, findings = discover_docs(root, config)
    enabled = _enabled(config, options)
    steps: list[StepResult] = []
    isolation: dict[str, Any] = {}

    if "commands" in enabled and not options.dry_run and not _supports_execution():
        findings.append(
            Finding(
                "RP104",
                "Running documented commands is supported on Linux and macOS only.",
                hint="Use WSL on Windows, or run the fast checks with --only links,env.",
            )
        )
    elif "commands" in enabled:
        host = os.environ if options.host_env is None else options.host_env
        scripts, modules = project_info(root)
        inputs = JourneyInputs(
            root=root,
            config=config,
            policy=Policy.from_config(config, scripts, modules),
            redactor=Redactor.from_environ(host, config.sandbox.pass_env),
            deadline=time.monotonic() + config.sandbox.total_timeout,
            dry_run=options.dry_run,
            tracked_only=options.tracked_only,
            keep=options.keep_workspace,
            progress=options.progress,
            host_env=host,
        )
        kept: list[str] = []
        for doc in docs.values():
            result = run_journey(doc, inputs)
            steps.extend(result.steps)
            findings.extend(result.findings)
            isolation = _merge_isolation(isolation, result.isolation)
            if result.workspace:
                kept.append(result.workspace)
        if kept:
            isolation["kept_workspaces"] = kept
    if "links" in enabled:
        findings.extend(check_links(root, docs, config.links))
    if "env" in enabled:
        findings.extend(check_env(root, docs, config.env))

    return Report(
        version=__version__,
        root=root.name or str(root),
        started_at=started.isoformat(timespec="seconds"),
        duration=time.monotonic() - clock,
        dry_run=options.dry_run,
        docs=list(docs),
        checks=[name for name in CHECKS if name in enabled],
        findings=findings,
        steps=steps,
        isolation=isolation,
    )
