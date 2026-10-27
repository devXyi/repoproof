from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoproof import __version__, cli
from repoproof.cli import ISSUES_URL, main, render_rules
from repoproof.config import load_config
from repoproof.reporters.markdown import MARKER

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "examples" / "broken-project"
BROKEN_LINK = "# T\n\n[gone](docs/missing.md)\n"
BROKEN_ANCHOR = "# T\n\n[gone](#missing)\n"


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def venv_available() -> bool:
    try:
        import ensurepip  # noqa: F401
    except ImportError:
        return False
    return True


def test_version_and_usage(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0 and f"repoproof {__version__}" in capsys.readouterr().out
    code, out, _ = run(capsys)
    assert code == 2 and "check" in out and "init" in out and "rules" in out


def test_exit_codes_follow_fail_on(make_repo, capsys):
    clean = make_repo({"README.md": "# Fine\n"})
    assert run(capsys, "check", str(clean), "--no-color")[0] == 0
    broken = make_repo({"README.md": BROKEN_LINK}, name="broken")
    assert run(capsys, "check", str(broken))[0] == 1
    assert run(capsys, "check", str(broken), "--fail-on", "never")[0] == 0
    soft = make_repo({"README.md": BROKEN_ANCHOR}, name="soft")  # a warning, not an error
    assert run(capsys, "check", str(soft))[0] == 0
    assert run(capsys, "check", str(soft), "--fail-on", "warning")[0] == 1


def test_fail_on_can_come_from_the_config_file(make_repo, capsys):
    repo = make_repo({"README.md": BROKEN_ANCHOR, "repoproof.toml": '[repoproof]\nfail_on = "warning"\n'})
    assert run(capsys, "check", str(repo))[0] == 1
    assert run(capsys, "check", str(repo), "--fail-on", "error")[0] == 0  # the command line wins


def test_dry_run_prints_the_plan_without_executing(make_repo, capsys):
    repo = make_repo({"README.md": "```sh\ntouch should-not-exist\nsudo make install\n```\n"})
    code, out, err = run(capsys, "check", str(repo), "--dry-run", "--only", "commands")
    assert code == 0 and "would run [setup]" in out and "never run" in out and err == ""


def test_only_selects_checks(make_repo, capsys):
    repo = make_repo({"README.md": BROKEN_LINK + "```sh\nfalse\n```\n"})
    code, out, _ = run(capsys, "check", str(repo), "--only", "links", "--format", "json")
    data = json.loads(out)
    assert code == 1 and data["checks"] == ["links"] and data["steps"] == []
    assert [f["rule_id"] for f in data["findings"]] == ["RP201"]


def test_machine_readable_stdout(make_repo, capsys):
    repo = make_repo({"README.md": BROKEN_LINK})
    _, out, _ = run(capsys, "check", str(repo), "--only", "links", "--format", "json")
    assert json.loads(out)["summary"]["errors"] == 1
    _, out, _ = run(capsys, "check", str(repo), "--only", "links", "--format", "sarif")
    assert json.loads(out)["runs"][0]["results"][0]["ruleId"] == "RP201"
    _, out, _ = run(capsys, "check", str(repo), "--only", "links", "--format", "markdown")
    assert out.startswith(MARKER)


def test_report_files_are_written_alongside_the_text_report(make_repo, tmp_path, capsys):
    repo = make_repo({"README.md": BROKEN_LINK})
    reports = tmp_path / "out" / "nested"
    code, out, _ = run(
        capsys, "check", str(repo), "--only", "links", "--json", str(reports / "r.json"),
        "--sarif", str(reports / "r.sarif"), "--markdown", str(reports / "r.md"),
    )  # fmt: skip
    assert code == 1 and "RP201" in out
    assert json.loads((reports / "r.json").read_text())["summary"]["errors"] == 1
    assert json.loads((reports / "r.sarif").read_text())["version"] == "2.1.0"
    assert (reports / "r.md").read_text().startswith(MARKER)
    _, out, _ = run(capsys, "check", str(repo), "--only", "links", "-q", "--json", "-")
    assert json.loads(out)["summary"]["errors"] == 1  # '-' means stdout, and -q silences the text report


def test_quiet_mode_prints_no_text_report(make_repo, capsys):
    repo = make_repo({"README.md": BROKEN_LINK})
    code, out, err = run(capsys, "check", str(repo), "-q")
    assert (code, out, err) == (1, "", "")


def test_usage_and_configuration_errors_exit_two(make_repo, tmp_path, capsys):
    repo = make_repo({"README.md": "# T\n"})
    code, _, err = run(capsys, "check", str(tmp_path / "nowhere"))
    assert code == 2 and "not a directory" in err
    code, _, err = run(capsys, "check", str(repo), "--only", "nonsense")
    assert code == 2 and "unknown check 'nonsense'" in err
    bad = make_repo({"README.md": "# T\n", "repoproof.toml": "[sandbox]\nnetwrok = 'deny'\n"}, name="bad")
    code, _, err = run(capsys, "check", str(bad))
    assert code == 2 and "configuration error" in err and "did you mean 'network'" in err
    code, _, err = run(capsys, "check", str(repo), "--config", str(tmp_path / "missing.toml"))
    assert code == 2 and "config file not found" in err


def test_nested_runs_are_refused_but_dry_runs_are_fine(make_repo, monkeypatch, capsys):
    repo = make_repo({"README.md": "# T\n"})
    monkeypatch.setenv("REPOPROOF_ACTIVE", "1")
    code, _, err = run(capsys, "check", str(repo))
    assert code == 2 and "refusing to run inside a RepoProof sandbox" in err
    assert run(capsys, "check", str(repo), "--dry-run")[0] == 0


def test_init_writes_a_config_that_loads(make_repo, capsys):
    repo = make_repo({"README.md": "# T\n"})
    code, out, _ = run(capsys, "init", str(repo))
    assert code == 0 and "Created" in out
    assert load_config(repo).docs == ["README.md"]
    code, _, err = run(capsys, "init", str(repo))
    assert code == 2 and "already exists" in err
    assert run(capsys, "init", str(repo), "--force")[0] == 0
    assert run(capsys, "init", str(repo / "nope"))[0] == 2


def test_rules_listing(capsys):
    code, out, _ = run(capsys, "rules")
    assert code == 0 and "RP101  command-failed" in out and "RP304" in out
    code, out, _ = run(capsys, "rules", "--format", "markdown")
    assert out.startswith("# Rule reference") and "## RP201" in out


def test_the_rules_document_is_generated_from_the_code():
    published = (REPO_ROOT / "docs" / "rules.md").read_text(encoding="utf-8")
    assert published == render_rules(markdown=True), (
        "docs/rules.md is stale; regenerate it with: repoproof rules --format markdown > docs/rules.md"
    )


def test_github_actions_annotations_and_job_summary(make_repo, tmp_path, monkeypatch, capsys):
    repo = make_repo({"README.md": BROKEN_LINK})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code, out, _ = run(capsys, "check", str(repo), "--only", "links")
    assert code == 1 and "::error file=README.md,line=3,title=RP201 broken-link::" in out
    assert summary.read_text().startswith(MARKER)
    # machine-readable stdout stays parseable: annotations move to stderr
    _, out, err = run(capsys, "check", str(repo), "--only", "links", "--format", "json")
    assert json.loads(out) and "::error " in err
    summary.unlink()
    _, out, _ = run(capsys, "check", str(repo), "--only", "links", "--no-github")
    assert "::error" not in out and not summary.exists()


def test_the_pull_request_comment_flag(make_repo, monkeypatch, capsys):
    calls: list[tuple[str, bool]] = []
    result = {"value": (True, "comment created")}

    def fake_upsert(context, body, *, create=True, transport=None):
        calls.append((body.splitlines()[0], create))
        return result["value"]

    monkeypatch.setattr(cli, "upsert_comment", fake_upsert)
    clean = make_repo({"README.md": "# Fine\n"})
    broken = make_repo({"README.md": BROKEN_LINK}, name="broken")
    run(capsys, "check", str(broken), "--only", "links", "--github-comment")
    run(capsys, "check", str(clean), "--only", "links", "--github-comment")
    run(capsys, "check", str(clean), "--only", "links", "--github-comment", "always")
    assert calls == [(MARKER, True), (MARKER, False), (MARKER, True)]
    result["value"] = (False, "could not post the comment (HTTP 403)")
    code, _, err = run(capsys, "check", str(broken), "--only", "links", "--github-comment")
    assert code == 1 and "warning: could not post the comment (HTTP 403)" in err  # never changes the verdict
    code, _, _ = run(capsys, "check", str(clean), "--only", "links", "--github-comment")
    assert code == 0


def test_internal_errors_are_reported_with_a_pointer_to_the_issue_tracker(make_repo, monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_check", explode)
    repo = make_repo({"README.md": "# T\n"})
    code, _, err = run(capsys, "check", str(repo))
    assert code == 3 and "internal error: RuntimeError('boom')" in err and ISSUES_URL in err
    monkeypatch.setenv("REPOPROOF_DEBUG", "1")
    with pytest.raises(RuntimeError, match="boom"):
        main(["check", str(repo)])


def test_interrupts_exit_130(make_repo, monkeypatch, capsys):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_check", interrupt)
    code, _, err = run(capsys, "check", str(make_repo({"README.md": "# T\n"})))
    assert code == 130 and "interrupted" in err


@pytest.mark.skipif(not venv_available(), reason="venv/ensurepip is unavailable")
def test_the_demo_project_shows_every_kind_of_finding(capsys):
    code, out, _ = run(capsys, "check", str(DEMO), "--format", "json", "-q")
    data = json.loads(out)
    found = {(f["rule_id"], f["file"], f["line"]) for f in data["findings"]}
    assert code == 1
    assert found == {
        ("RP101", "README.md", 10),  # source venv/bin/activate: the venv is called .venv
        ("RP101", "README.md", 17),  # python3 app.py: crashes because DATABASE_URL is not set
        ("RP201", "README.md", 20),  # link to docs/setup.md, which does not exist
        ("RP202", "README.md", 21),  # link to the #installation heading, which does not exist
        ("RP301", "app.py", 4),  # API_TOKEN is required but documented nowhere
        ("RP301", "app.py", 5),  # DEBUG is optional and undocumented (a note)
        ("RP303", ".env.example", 2),  # OLD_FLAG is never read
    }
    activate = next(f for f in data["findings"] if f["line"] == 10)
    assert activate["hint"] == "Did you mean `.venv/bin/activate`?"
