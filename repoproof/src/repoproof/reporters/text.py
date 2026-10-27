"""The human-readable terminal report."""

from __future__ import annotations

from ..models import Finding, Report, Severity, StepResult

_COMMAND_RULES = frozenset({"RP101", "RP102", "RP103", "RP104", "RP105"})
_OUTPUT_LINES = 12


class _Style:
    def __init__(self, color: bool, unicode_ok: bool) -> None:
        self.color = color
        self.glyphs = (
            {"ok": "✔", "fail": "✖", "warn": "▲", "note": "•", "skip": "○", "plan": "▸", "bar": "│"}
            if unicode_ok
            else {"ok": "ok", "fail": "FAIL", "warn": "warn", "note": "note", "skip": "-", "plan": ">", "bar": "|"}
        )

    def paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self.paint("1", text)

    def dim(self, text: str) -> str:
        return self.paint("2", text)

    def red(self, text: str) -> str:
        return self.paint("31", text)

    def green(self, text: str) -> str:
        return self.paint("32", text)

    def yellow(self, text: str) -> str:
        return self.paint("33", text)

    def cyan(self, text: str) -> str:
        return self.paint("36", text)


def render_text(
    report: Report, *, color: bool = False, verbose: bool = False, unicode_ok: bool = True
) -> str:
    """Render ``report`` for a terminal."""
    style = _Style(color, unicode_ok)
    out: list[str] = []
    mode = " (dry run: nothing was executed)" if report.dry_run else ""
    out.append(style.bold(f"RepoProof {report.version}") + style.dim(f" · {report.root}{mode}"))

    by_step = {
        (f.file, f.line): f
        for f in report.findings
        if f.rule_id in _COMMAND_RULES and f.line is not None
    }
    shown = {(s.doc, s.line) for s in report.steps if s.status in ("failed", "timeout")}

    docs = list(dict.fromkeys(step.doc for step in report.steps))
    for doc in docs:
        steps = [s for s in report.steps if s.doc == doc]
        out.append("")
        out.append(style.bold("Commands") + style.dim(f" · {doc}"))
        width = max(len(f"{s.doc}:{s.line}") for s in steps)
        for step in steps:
            out.extend(_step_lines(step, by_step.get((step.doc, step.line)), style, width, verbose, report.dry_run))

    others = [
        f for f in report.findings if not (f.rule_id in _COMMAND_RULES and (f.file, f.line) in shown)
    ]
    if others:
        out.append("")
        out.append(style.bold("Findings"))
        for finding in others:
            out.extend(_finding_lines(finding, style))

    for warning in report.isolation.get("warnings", []):
        out.append("")
        out.append(style.yellow(f"{style.glyphs['warn']} {warning}"))

    out.append("")
    out.append(_summary(report, style))
    return "\n".join(out) + "\n"


def _step_lines(
    step: StepResult, finding: Finding | None, style: _Style, width: int, verbose: bool, dry_run: bool
) -> list[str]:
    glyphs = style.glyphs
    where = f"{step.doc}:{step.line}".ljust(width)
    command = _one_line(step.command, 64)
    took = f"  {step.duration:.1f}s" if step.duration >= 0.05 else ""
    if step.status == "passed":
        return [f"  {style.green(glyphs['ok'])} {where}  {command}{style.dim(took)}"]
    if step.status == "planned":
        phase = f" [{step.phase}]" if step.phase else ""
        return [f"  {style.cyan(glyphs['plan'])} {where}  {command}{style.dim('  would run' + phase)}"]
    if step.status in ("skipped", "not_run"):
        if not (verbose or dry_run or step.status == "not_run"):
            return []
        reason = step.reason or "skipped"
        return [f"  {style.dim(glyphs['skip'] + ' ' + where + '  ' + command + '  (' + reason + ')')}"]
    label = "timed out" if step.status == "timeout" else (
        f"exit {step.exit_code}" if step.exit_code is not None else "failed"
    )
    lines = [f"  {style.red(glyphs['fail'])} {where}  {command}{style.dim(took)}  {style.red(label)}"]
    indent = " " * (width + 6)
    if finding is not None:
        lines.append(f"{indent}{finding.message}")
        if finding.hint:
            lines.append(style.dim(f"{indent}{finding.hint}"))
    if step.output:
        tail = step.output.rstrip().splitlines()[-_OUTPUT_LINES:]
        lines.extend(style.dim(f"{indent}{glyphs['bar']} {_one_line(line, 150)}") for line in tail)
    return lines


def _finding_lines(finding: Finding, style: _Style) -> list[str]:
    glyphs = style.glyphs
    if finding.level is Severity.ERROR:
        mark = style.red(glyphs["fail"])
    elif finding.level is Severity.WARNING:
        mark = style.yellow(glyphs["warn"])
    else:
        mark = style.dim(glyphs["note"])
    where = ""
    if finding.file:
        where = finding.file + (f":{finding.line}" if finding.line else "") + "  "
    lines = [f"  {mark} {where}{finding.message} {style.dim('[' + finding.rule_id + ']')}"]
    if finding.hint:
        lines.append(style.dim(f"      {finding.hint}"))
    return lines


def _summary(report: Report, style: _Style) -> str:
    counts = report.summary()
    steps = counts["steps"]
    ran = steps["passed"] + steps["failed"] + steps["timeout"]
    parts = []
    for label, value in (("error", counts["errors"]), ("warning", counts["warnings"]), ("note", counts["notes"])):
        parts.append(f"{value} {label}{'' if value == 1 else 's'}")
    findings = ", ".join(parts)
    if counts["errors"]:
        headline = style.red(style.bold(findings))
    elif counts["warnings"]:
        headline = style.yellow(style.bold(findings))
    else:
        headline = style.green(style.bold(findings))
    detail = (
        f"commands: {ran} run, {steps['passed']} passed, {steps['failed'] + steps['timeout']} failed, "
        f"{steps['skipped'] + steps['not_run']} skipped"
    )
    if report.dry_run:
        detail = f"commands: {steps['planned']} would run, {steps['skipped']} skipped"
    if "commands" not in report.checks:
        detail = "checks: " + ", ".join(report.checks)
    return f"{headline}  {style.dim('· ' + detail + f' · {report.duration:.1f}s')}"


def _one_line(text: str, limit: int) -> str:
    first = text.splitlines()[0] if text else ""
    if len(text.splitlines()) > 1:
        first += " …"
    return first if len(first) <= limit else first[: limit - 1] + "…"
