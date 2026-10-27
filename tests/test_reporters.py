from __future__ import annotations

import json

from repoproof.models import RULES, Finding, Report, StepResult
from repoproof.reporters.jsonfmt import render_json
from repoproof.reporters.markdown import MARKER, render_markdown
from repoproof.reporters.sarif import fingerprint, render_sarif
from repoproof.reporters.text import render_text


def sample(dry_run: bool = False, findings: list[Finding] | None = None) -> Report:
    failing = Finding(
        "RP101",
        "`python3 app.py` exited with status 1",
        file="README.md",
        line=17,
        command="python3 app.py",
        exit_code=1,
        output="Traceback (most recent call last):\nKeyError: 'DATABASE_URL'",
        hint="Set DATABASE_URL first.",
    )
    default = [
        failing,
        Finding("RP201", "The link target 'docs/setup.md' does not exist", file="README.md", line=20),
        Finding("RP301", "`API_TOKEN` is read at app.py:4 but is not documented", file="app.py", line=4),
        Finding("RP303", "`OLD_FLAG` is listed in .env.example", file=".env.example", line=2),
    ]
    steps = [
        StepResult("README.md", 9, "python3 -m venv .venv", "passed", exit_code=0, duration=1.234, phase="setup"),
        StepResult("README.md", 17, "python3 app.py", "failed", exit_code=1, duration=0.3, output=failing.output),
        StepResult("README.md", 18, "mytool --help", "skipped", reason="`mytool` is not on the allowlist"),
        StepResult("README.md", 19, "pytest", "not_run", reason="an earlier step failed"),
    ]
    return Report(
        version="0.1.0",
        root="demo",
        started_at="2026-10-24T12:00:00+00:00",
        duration=1.5,
        dry_run=dry_run,
        docs=["README.md"],
        checks=["commands", "links", "env"],
        findings=default if findings is None else findings,
        steps=steps,
        isolation={"backend": "local", "warnings": ["network restriction could not be enforced (test)"]},
    )


def test_json_report_shape():
    data = json.loads(render_json(sample()))
    assert data["schema_version"] == 1 and data["tool"] == {"name": "repoproof", "version": "0.1.0"}
    assert data["summary"] == {
        "errors": 2, "warnings": 1, "notes": 1,
        "steps": {"total": 4, "passed": 1, "failed": 1, "timeout": 0, "skipped": 1, "not_run": 1, "planned": 0},
    }  # fmt: skip
    first = data["findings"][0]
    assert first == {
        "rule_id": "RP101", "rule_name": "command-failed", "severity": "error",
        "message": "`python3 app.py` exited with status 1", "file": "README.md", "line": 17,
        "command": "python3 app.py", "exit_code": 1,
        "output": "Traceback (most recent call last):\nKeyError: 'DATABASE_URL'",
        "hint": "Set DATABASE_URL first.",
    }  # fmt: skip
    assert data["steps"][0]["duration_seconds"] == 1.234 and data["steps"][2]["reason"].startswith("`mytool`")
    assert data["docs"] == ["README.md"] and data["isolation"]["backend"] == "local"


def test_sarif_is_structurally_valid_and_consistent():
    log = json.loads(render_sarif(sample()))
    assert log["version"] == "2.1.0" and log["$schema"].endswith("sarif-2.1.0.json")
    (run,) = log["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] == "RepoProof" and driver["version"] == "0.1.0"
    rule_ids = [rule["id"] for rule in driver["rules"]]
    assert rule_ids == list(RULES)
    for rule in driver["rules"]:
        assert rule["shortDescription"]["text"] and rule["help"]["text"]
        assert rule["helpUri"].endswith(f"docs/rules.md#{rule['id'].lower()}")
        assert rule["defaultConfiguration"]["level"] in {"error", "warning", "note"}
    results = run["results"]
    assert [r["level"] for r in results] == ["error", "error", "warning", "note"]
    for result in results:
        assert rule_ids[result["ruleIndex"]] == result["ruleId"]
        location = result["locations"][0]["physicalLocation"]
        assert location["artifactLocation"]["uriBaseId"] == "%SRCROOT%" and location["region"]["startLine"] >= 1
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "README.md"
    assert "Set DATABASE_URL first." in results[0]["message"]["text"]
    prints = {r["partialFingerprints"]["repoproof/v1"] for r in results}
    assert len(prints) == 4


def test_sarif_fingerprints_are_stable_across_line_moves_but_not_across_problems():
    base = Finding("RP101", "m", file="README.md", line=10, command="pip install x")
    moved = Finding("RP101", "m", file="README.md", line=42, command="pip install x")
    other = Finding("RP101", "m", file="README.md", line=10, command="pip install y")
    assert fingerprint(base) == fingerprint(moved) != fingerprint(other)


def test_sarif_results_without_a_file_have_no_location():
    log = json.loads(render_sarif(sample(findings=[Finding("RP001", "No README")])))
    assert "locations" not in log["runs"][0]["results"][0]


def test_text_report_shows_what_failed_and_why():
    text = render_text(sample())
    assert "RepoProof 0.1.0 · demo" in text
    assert "✔ README.md:9" in text and "✖ README.md:17" in text and "exit 1" in text
    assert "│ KeyError: 'DATABASE_URL'" in text and "Set DATABASE_URL first." in text
    assert "mytool" not in text  # skipped steps are listed only with --verbose
    assert "pytest" in text and "an earlier step failed" in text
    assert "[RP201]" in text and "[RP301]" in text and "[RP303]" in text
    assert "2 errors, 1 warning, 1 note" in text and "network restriction could not be enforced" in text
    assert "mytool" in render_text(sample(), verbose=True)


def test_text_report_ascii_and_color_modes():
    plain = render_text(sample(), unicode_ok=False)
    assert "ok README.md:9" in plain and "FAIL README.md:17" in plain
    assert not any(ord(ch) > 127 for ch in plain.replace("·", ""))
    assert "\033[" not in render_text(sample())
    assert "\033[" in render_text(sample(), color=True)


def test_text_report_for_a_dry_run_lists_planned_and_skipped_steps():
    report = sample(dry_run=True, findings=[])
    report.steps[0].status = "planned"
    text = render_text(report)
    assert "(dry run: nothing was executed)" in text and "would run [setup]" in text
    assert "mytool" in text and "0 errors, 0 warnings, 0 notes" in text


def test_a_clean_report_says_so():
    clean = sample(findings=[])
    assert render_markdown(clean).splitlines()[1] == "## ✅ RepoProof: the documented setup works"
    assert "0 errors" in render_text(clean)


def test_markdown_report_for_pull_requests():
    text = render_markdown(sample(), repo_url="https://github.com/o/r", sha="abc123")
    lines = text.splitlines()
    assert lines[0] == MARKER and lines[1] == "## ❌ RepoProof: 2 errors, 1 warning, 1 note"
    assert "### `RP101` A documented command exited with a non-zero status" in text
    assert "[README.md:17](https://github.com/o/r/blob/abc123/README.md#L17)" in text
    assert "```bash\npython3 app.py\n```" in text and "> **Hint:** Set DATABASE_URL first." in text
    assert "<summary>Output (end of log)</summary>" in text and "KeyError: 'DATABASE_URL'" in text
    assert "<summary>Warnings and notes (2 findings)</summary>" in text
    assert "`RP301`" in text and "`RP303`" in text
    assert "2 commands run, 2 skipped" in text


def test_markdown_fences_grow_around_backticks_in_output():
    finding = Finding("RP101", "m", file="README.md", line=1, command="x", output="before\n```\nafter")
    text = render_markdown(sample(findings=[finding]))
    assert "````text\nbefore\n```\nafter\n````" in text


def test_markdown_is_truncated_but_keeps_its_marker():
    findings = [
        Finding("RP101", "m" * 200, file="README.md", line=n, command="c", output="x" * 2000) for n in range(1, 60)
    ]
    text = render_markdown(sample(findings=findings), max_chars=5000)
    assert len(text) <= 5200 and text.startswith(MARKER) and "report truncated" in text


def test_markdown_lists_are_capped():
    notes = [Finding("RP303", f"`V{n}` is stale", file=".env.example", line=n) for n in range(1, 61)]
    text = render_markdown(sample(findings=notes))
    assert "and 20 more" in text and "`V40`" in text and "`V41`" not in text


def test_the_summary_does_not_claim_commands_when_they_were_not_checked():
    report = sample(findings=[])
    report.checks = ["links", "env"]
    text = render_text(report)
    assert "checks: links, env" in text and "commands:" not in text
