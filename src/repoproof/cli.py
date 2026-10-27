"""The ``repoproof`` command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .config import (
    CHECKS,
    CONFIG_FILENAME,
    FAIL_ON_CHOICES,
    NETWORK_CHOICES,
    Config,
    ConfigError,
    load_config,
)
from .engine import CheckOptions, run_check
from .github import GitHubContext, format_annotations, upsert_comment, write_step_summary
from .models import RULES, Report
from .reporters.jsonfmt import render_json
from .reporters.markdown import render_markdown
from .reporters.sarif import render_sarif
from .reporters.text import render_text

ISSUES_URL = "https://github.com/devXyi/repoproof/issues"

CONFIG_TEMPLATE = """\
# repoproof.toml -- configuration for RepoProof (https://github.com/devXyi/repoproof)
# Every key is optional. Unknown keys are errors, so typos never silently disable a check.
version = 1

[repoproof]
# Documentation files to verify. Each one runs as its own journey in a fresh workspace.
docs = ["README.md"]
# Exit with status 1 when a finding of this severity (or worse) exists:
# "error" | "warning" | "note" | "never"
fail_on = "error"

[sandbox]
# "auto" builds a pristine virtualenv on demand, "none" turns that off, anything else
# names the interpreter to build it with (for example "python3.12").
python = "auto"
# Network policy for documented commands: "deny", "install" (package installs only) or "allow".
network = "install"
# Seconds per command (install, build and test have longer defaults) and for the whole run.
timeout = 300
total_timeout = 1800
# Continue after a failing step instead of stopping where a newcomer would be stuck.
keep_going = false
# Host environment variables to pass into the sandbox. Nothing else is inherited.
pass_env = []

[commands]
# Extra commands that may run. "*" is the only wildcard; a pattern is matched against the
# command's words joined by single spaces. Table form: { pattern = "make data", network = true }
allow = [
  # "mytool *",
]
# Documented commands that must never run (reported as skipped).
skip = []

# Replace a documented command before it is vetted, for instance to test the checkout
# instead of the published package:
# [[commands.rewrite]]
# match = "pip install mypackage"
# run = "pip install ."

[links]
enabled = true
# Link targets to leave alone (patterns, "*" matches anything).
ignore = []

[env]
enabled = true
# Variable names to leave out of the comparison (patterns).
ignore = []
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoproof",
        description="Prove that your repository's setup instructions actually work.",
    )
    parser.add_argument("--version", action="version", version=f"repoproof {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    check = commands.add_parser(
        "check",
        help="verify a repository's documented setup",
        description="Run the approved commands from a repository's documentation in a clean, "
        "throw-away workspace, then check its links and configuration consistency.",
    )
    check.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
    check.add_argument("-c", "--config", metavar="FILE", help="config file (default: <path>/repoproof.toml)")
    check.add_argument("--docs", action="append", metavar="FILE", help="documentation file to verify (repeatable)")
    check.add_argument("--only", metavar="CHECKS", help=f"comma-separated subset of: {', '.join(CHECKS)}")
    check.add_argument("--dry-run", action="store_true", help="show what would run; execute nothing")
    check.add_argument("--fail-on", choices=FAIL_ON_CHOICES, help="severity that fails the run (default: error)")
    check.add_argument("--network", choices=NETWORK_CHOICES, help="network policy for documented commands")
    check.add_argument("--timeout", type=int, metavar="SECONDS", help="default per-command timeout")
    check.add_argument("--keep-going", action="store_true", help="continue after a failing step")
    check.add_argument("--strict-isolation", action="store_true", help="fail instead of running without a requested isolation")
    check.add_argument("--tracked-only", action="store_true", help="copy only git-tracked files, like a pure clone")
    check.add_argument("--keep-workspace", action="store_true", help="do not delete the temporary workspace")
    check.add_argument("--format", choices=("text", "json", "sarif", "markdown"), default="text", help="report format on stdout")
    check.add_argument("--json", metavar="FILE", help="also write a JSON report ('-' for stdout)")
    check.add_argument("--sarif", metavar="FILE", help="also write a SARIF report")
    check.add_argument("--markdown", metavar="FILE", help="also write a Markdown report")
    check.add_argument(
        "--github-comment",
        nargs="?",
        const="on-failure",
        choices=("on-failure", "always"),
        help="post or update a pull-request comment (needs GITHUB_TOKEN)",
    )
    check.add_argument("--no-github", action="store_true", help="no annotations or job summary on GitHub Actions")
    check.add_argument("-q", "--quiet", action="store_true", help="no progress and no text report")
    check.add_argument("-v", "--verbose", action="store_true", help="also list skipped commands")
    check.add_argument("--no-color", action="store_true", help="disable colored output")

    init = commands.add_parser("init", help=f"write a starter {CONFIG_FILENAME}")
    init.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
    init.add_argument("--force", action="store_true", help=f"overwrite an existing {CONFIG_FILENAME}")

    rules = commands.add_parser("rules", help="list the rules RepoProof reports")
    rules.add_argument("--format", choices=("text", "markdown"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "check": _check,
        "init": _init,
        "rules": _rules,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"repoproof: configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("repoproof: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort handler for the CLI
        if os.environ.get("REPOPROOF_DEBUG"):
            raise
        print(f"repoproof: internal error: {exc!r}", file=sys.stderr)
        print(f"This is a bug. Please report it at {ISSUES_URL} (REPOPROOF_DEBUG=1 shows the traceback).", file=sys.stderr)
        return 3


def _check(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        print(f"repoproof: not a directory: {root}", file=sys.stderr)
        return 2
    if os.environ.get("REPOPROOF_ACTIVE") and not args.dry_run:
        print(
            "repoproof: refusing to run inside a RepoProof sandbox. A documented command "
            "invoked `repoproof check`; skip it with [commands] skip or rewrite it to add --dry-run.",
            file=sys.stderr,
        )
        return 2
    config = load_config(root, Path(args.config) if args.config else None)
    _apply_overrides(config, args)
    only = _parse_only(args.only)

    progress = None
    if not (args.quiet or args.dry_run):
        arrow = "▶" if _unicode_ok(sys.stderr) else ">"
        progress = lambda message: print(f"{arrow} {message}", file=sys.stderr, flush=True)  # noqa: E731
    options = CheckOptions(
        only=only,
        dry_run=args.dry_run,
        tracked_only=args.tracked_only,
        keep_workspace=args.keep_workspace,
        progress=progress,
    )
    report = run_check(root, config, options)

    context = GitHubContext.from_env()
    markdown = render_markdown(report, repo_url=context.repo_url, sha=context.sha)
    _emit_stdout(report, markdown, args)
    for path, text in (
        (args.json, lambda: render_json(report)),
        (args.sarif, lambda: render_sarif(report)),
        (args.markdown, lambda: markdown),
    ):
        if path:
            _write(path, text())
    _github(report, markdown, context, args)

    for workspace in report.isolation.get("kept_workspaces", []):
        print(f"repoproof: workspace kept at {workspace}", file=sys.stderr)
    return report.exit_code(config.fail_on)


def _apply_overrides(config: Config, args: argparse.Namespace) -> None:
    if args.docs:
        config.docs = list(args.docs)
    if args.fail_on:
        config.fail_on = args.fail_on
    if args.network:
        config.sandbox.network = args.network
    if args.timeout:
        config.sandbox.timeout = args.timeout
    if args.keep_going:
        config.sandbox.keep_going = True
    if args.strict_isolation:
        config.sandbox.strict = True


def _parse_only(value: str | None) -> frozenset[str] | None:
    if not value:
        return None
    names = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise ConfigError(f"--only: unknown check '{unknown[0]}' (choose from {', '.join(CHECKS)})")
    return frozenset(names)


def _emit_stdout(report: Report, markdown: str, args: argparse.Namespace) -> None:
    if args.format == "json":
        text = render_json(report)
    elif args.format == "sarif":
        text = render_sarif(report)
    elif args.format == "markdown":
        text = markdown
    elif args.quiet:
        return
    else:
        text = render_text(
            report,
            color=_use_color(sys.stdout, args.no_color),
            verbose=args.verbose,
            unicode_ok=_unicode_ok(sys.stdout),
        )
    sys.stdout.write(text)
    sys.stdout.flush()


def _github(report: Report, markdown: str, context: GitHubContext, args: argparse.Namespace) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true" and not args.no_github:
        stream = sys.stdout if args.format == "text" else sys.stderr
        for line in format_annotations(report.findings):
            print(line, file=stream)
        write_step_summary(markdown)
    if args.github_comment:
        clean = not report.findings
        ok, message = upsert_comment(
            context, markdown, create=not (clean and args.github_comment == "on-failure")
        )
        if not ok:
            print(f"repoproof: warning: {message}", file=sys.stderr)


def _write(path: str, text: str) -> None:
    if path == "-":
        sys.stdout.write(text)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _use_color(stream: TextIO, no_color: bool) -> bool:
    if no_color or "NO_COLOR" in os.environ:
        return False
    return bool(os.environ.get("FORCE_COLOR")) or stream.isatty()


def _unicode_ok(stream: TextIO) -> bool:
    return "utf" in (getattr(stream, "encoding", None) or "").lower()


def _init(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        print(f"repoproof: not a directory: {root}", file=sys.stderr)
        return 2
    target = root / CONFIG_FILENAME
    if target.exists() and not args.force:
        print(f"repoproof: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")
    return 0


def render_rules(markdown: bool = False) -> str:
    """The rule catalogue (also used to generate docs/rules.md)."""
    if not markdown:
        width = max(len(rule.name) for rule in RULES.values())
        lines = [
            f"{rule.id}  {rule.name.ljust(width)}  {rule.severity.value.ljust(7)}  {rule.summary}"
            for rule in RULES.values()
        ]
        return "\n".join(lines) + "\n"
    lines = [
        "# Rule reference",
        "",
        "<!-- Generated by `repoproof rules --format markdown`; edit the rule texts in "
        "src/repoproof/models.py. -->",
        "",
        "Every finding carries a rule ID. The severity shown is the default; a few rules lower it "
        "when the evidence is weaker (see the rule text).",
        "",
        "| ID | Name | Default severity |",
        "| --- | --- | --- |",
    ]
    lines += [f"| [{r.id}](#{r.id.lower()}) | `{r.name}` | {r.severity.value} |" for r in RULES.values()]
    for rule in RULES.values():
        lines += [
            "",
            f"## {rule.id}",
            "",
            f"**`{rule.name}`** · default severity: {rule.severity.value}",
            "",
            rule.summary,
            "",
            rule.help,
        ]
    return "\n".join(lines) + "\n"


def _rules(args: argparse.Namespace) -> int:
    sys.stdout.write(render_rules(markdown=args.format == "markdown"))
    return 0
