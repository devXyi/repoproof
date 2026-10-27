"""Markdown report for pull-request comments and the GitHub job summary."""

from __future__ import annotations

import re

from ..models import Finding, Report, Severity

MARKER = "<!-- repoproof:report -->"
RULES_URL = "https://github.com/devXyi/repoproof/blob/main/docs/rules.md"
_MAX_OUTPUT_LINES = 40
_MAX_OUTPUT_CHARS = 3000
_MAX_LISTED = 40


def render_markdown(
    report: Report,
    *,
    repo_url: str | None = None,
    sha: str | None = None,
    max_chars: int = 60000,
) -> str:
    """Render ``report`` as Markdown. ``repo_url`` and ``sha`` turn locations into permalinks."""
    errors = [f for f in report.findings if f.level is Severity.ERROR]
    minor = [f for f in report.findings if f.level is not Severity.ERROR]
    lines = [MARKER, _title(report), ""]
    for finding in errors:
        lines.extend(_detail(finding, repo_url, sha))
    if minor:
        noun = "finding" if len(minor) == 1 else "findings"
        lines += ["<details>", f"<summary>Warnings and notes ({len(minor)} {noun})</summary>", ""]
        for finding in minor[:_MAX_LISTED]:
            icon = "⚠️" if finding.level is Severity.WARNING else "ℹ️"
            lines.append(f"- {icon} `{finding.rule_id}` {_where(finding, repo_url, sha)}{finding.message}")
        if len(minor) > _MAX_LISTED:
            lines.append(f"- … and {len(minor) - _MAX_LISTED} more (see the JSON or SARIF report)")
        lines += ["", "</details>", ""]
    lines.append(_footer(report))
    text = "\n".join(lines) + "\n"
    if len(text) > max_chars:
        cut = max_chars - 200
        text = text[:cut].rstrip() + "\n\n_…report truncated; see the job log for the full output._\n"
    return text


def _title(report: Report) -> str:
    counts = report.summary()
    errors, warnings, notes = counts["errors"], counts["warnings"], counts["notes"]
    if not (errors or warnings or notes):
        return "## ✅ RepoProof: the documented setup works"
    parts = [f"{errors} error{'' if errors == 1 else 's'}"]
    if warnings:
        parts.append(f"{warnings} warning{'' if warnings == 1 else 's'}")
    if notes:
        parts.append(f"{notes} note{'' if notes == 1 else 's'}")
    icon = "❌" if errors else "⚠️"
    return f"## {icon} RepoProof: {', '.join(parts)}"


def _where(finding: Finding, repo_url: str | None, sha: str | None) -> str:
    if not finding.file:
        return ""
    label = finding.file + (f":{finding.line}" if finding.line else "")
    if repo_url and sha:
        anchor = f"#L{finding.line}" if finding.line else ""
        return f"[{label}]({repo_url}/blob/{sha}/{finding.file}{anchor}) — "
    return f"`{label}` — "


def _detail(finding: Finding, repo_url: str | None, sha: str | None) -> list[str]:
    where = _where(finding, repo_url, sha).removesuffix(" — ")
    heading = f"### `{finding.rule_id}` {finding.rule.summary.rstrip('.')}"
    lines = [heading + (f" — {where}" if where else ""), ""]
    if finding.command:
        lines += [_fenced(finding.command, "bash"), ""]
    lines.append(finding.message)
    if finding.hint:
        lines += ["", f"> **Hint:** {finding.hint}"]
    if finding.output:
        tail = finding.output.rstrip().splitlines()[-_MAX_OUTPUT_LINES:]
        output = "\n".join(tail)[-_MAX_OUTPUT_CHARS:]
        lines += ["", "<details>", "<summary>Output (end of log)</summary>", "", _fenced(output, "text"), "", "</details>"]
    lines.append("")
    return lines


def _fenced(text: str, language: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _footer(report: Report) -> str:
    steps = report.summary()["steps"]
    ran = steps["passed"] + steps["failed"] + steps["timeout"]
    return (
        f"<sub>RepoProof {report.version} · {ran} commands run, "
        f"{steps['skipped'] + steps['not_run']} skipped · {report.duration:.1f}s · "
        f"[rule reference]({RULES_URL})</sub>"
    )
