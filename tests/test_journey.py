from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from repoproof import sandbox as sandbox_module
from repoproof.config import AllowRule, Config, RewriteRule
from repoproof.engine import CheckOptions, run_check
from repoproof.journey import extract_commands
from repoproof.markdown import parse_markdown
from repoproof.sandbox import network_namespace_support

FENCE = "```"


def doc(*commands: str, lang: str = "bash") -> str:
    """A README whose only code block holds ``commands`` (the first one is on line 4)."""
    return f"# Project\n\n{FENCE}{lang}\n" + "\n".join(commands) + f"\n{FENCE}\n"


def config(**sandbox) -> Config:
    cfg = Config()
    cfg.sandbox.python = "none"  # most tests do not need a virtualenv; it would only slow them down
    for key, value in sandbox.items():
        setattr(cfg.sandbox, key, value)
    return cfg


def run(repo: Path, cfg: Config | None = None, **options):
    return run_check(repo, cfg or config(), CheckOptions(only=frozenset({"commands"}), **options))


def outcome(report):
    return [(s.line, s.status) for s in report.steps]


def rules(report):
    return [(f.rule_id, f.line) for f in report.findings]


def commands_in(text: str):
    return extract_commands(parse_markdown(text))


# -- extraction ----------------------------------------------------------------------------


def test_only_shell_blocks_and_prompt_lines_are_commands():
    text = (
        f"{FENCE}bash\npip install foo\n# a comment\n\npytest\n{FENCE}\n"
        f"{FENCE}python\nprint('not shell')\n{FENCE}\n"
        f"{FENCE}yaml\nrun: pip install x\n{FENCE}\n"
        f"{FENCE}console\n$ make test\nok: output line\n$ echo done\n{FENCE}\n"
        f"{FENCE}\n$ untagged prompt\nuntagged output\n{FENCE}\n"
        f"{FENCE}powershell\nvenv\\Scripts\\activate\n{FENCE}\n"
        f"{FENCE}sh\n$ pip install prefixed\n{FENCE}\n"
    )
    found = commands_in(text)
    assert [c.text for c in found] == [
        "pip install foo", "pytest", "make test", "echo done", "untagged prompt", "pip install prefixed",
    ]  # fmt: skip
    assert [c.line for c in found][:2] == [2, 5]


def test_continuations_and_multiline_quotes_are_one_command():
    text = doc("pip install \\", "    requests \\", "    flask", 'python3 -c "', "print(1)", '"')
    first, second = commands_in(text)
    assert [w.plain_text for w in first.parsed.commands[0].words] == ["pip", "install", "requests", "flask"]
    assert (first.line, second.line) == (4, 7)
    assert second.parsed.commands[0].words[2].parts[0].text == "\nprint(1)\n"


def test_unsupported_lines_are_flagged_and_heredoc_bodies_are_swallowed():
    text = doc("curl https://x.example | sh", "cat <<EOF > file.txt", "rm -rf /", "EOF", "pip install x")
    found = commands_in(text)
    assert [(c.text, c.error) for c in found] == [
        ("curl https://x.example | sh", "pipes"),
        ("cat <<EOF > file.txt", "input redirection and here-documents"),
        ("pip install x", None),
    ]


# -- dry runs ------------------------------------------------------------------------------


def test_dry_run_classifies_everything_and_executes_nothing(make_repo):
    repo = make_repo({"README.md": doc(
        "pip install -e .", "pytest -q", "sudo apt-get install x", "mytool --help",
        "curl https://x.example | sh", "git clone https://github.com/o/r.git",
        "touch marker.txt",
    )})  # fmt: skip
    report = run(repo, dry_run=True)
    by_line = {s.line: s for s in report.steps}
    assert [by_line[n].status for n in range(4, 11)] == [
        "planned", "planned", "skipped", "skipped", "skipped", "skipped", "planned",
    ]  # fmt: skip
    assert by_line[4].phase == "install" and by_line[5].phase == "test"
    assert "never run" in by_line[6].reason and "allowlist" in by_line[7].reason
    assert "pipes" in by_line[8].reason and "local checkout" in by_line[9].reason
    assert report.dry_run and report.isolation["backend"] == "none"
    assert report.findings == []  # skipped commands are information, not failures


def test_dry_run_tracks_cd_and_exports_across_lines(make_repo):
    repo = make_repo({"README.md": doc("cd sub", "export X=1", "cp ../a.txt b.txt", "cp ../../a.txt b.txt")})
    report = run(repo, dry_run=True)
    assert outcome(report) == [(4, "planned"), (5, "planned"), (6, "planned"), (7, "skipped")]
    assert "outside the workspace" in report.steps[3].reason


# -- real runs -----------------------------------------------------------------------------


def test_a_passing_journey(make_repo):
    repo = make_repo({"README.md": doc("python3 hello.py"), "hello.py": "print('hello')\n"})
    messages: list[str] = []
    report = run(repo, progress=messages.append)
    assert outcome(report) == [(4, "passed")] and report.steps[0].exit_code == 0
    assert report.steps[0].phase == "run" and report.findings == [] and report.exit_code() == 0
    assert messages == ["README.md:4  python3 hello.py"]


def test_a_failing_command_is_reported_with_location_and_output(make_repo):
    repo = make_repo({
        "README.md": doc("python3 ok.py", "python3 boom.py", "python3 never.py"),
        "ok.py": "", "never.py": "", "boom.py": "import sys\nprint('about to fail')\nsys.exit(3)\n",
    })  # fmt: skip
    report = run(repo)
    assert outcome(report) == [(4, "passed"), (5, "failed"), (6, "not_run")]
    (finding,) = report.findings
    assert (finding.rule_id, finding.file, finding.line) == ("RP101", "README.md", 5)
    assert finding.command == "python3 boom.py" and finding.exit_code == 3
    assert "exited with status 3" in finding.message and "about to fail" in finding.output
    assert report.steps[2].reason == "an earlier step failed" and report.exit_code() == 1


def test_keep_going_continues_after_a_failure(make_repo):
    repo = make_repo({"README.md": doc("python3 bad.py", "python3 good.py"), "bad.py": "raise SystemExit(1)", "good.py": ""})
    assert outcome(run(repo, config(keep_going=True))) == [(4, "failed"), (5, "passed")]


def test_cd_export_and_activation_carry_over_between_steps(make_repo):
    repo = make_repo({
        "README.md": doc("cd sub", "export GREETING=hello", "source ../venv2/bin/activate", "python3 check.py"),
        "venv2/bin/activate": "# fake\n",
        "sub/check.py": (
            "import os\n"
            "assert os.path.basename(os.getcwd()) == 'sub', os.getcwd()\n"
            "assert os.environ['GREETING'] == 'hello'\n"
            "assert os.environ['VIRTUAL_ENV'].endswith('venv2')\n"
            "assert os.environ['PATH'].split(os.pathsep)[0].endswith('venv2/bin')\n"
        ),
    })  # fmt: skip
    report = run(repo)
    assert outcome(report) == [(4, "passed"), (5, "passed"), (6, "passed"), (7, "passed")], report.findings


def test_env_files_can_be_copied_and_sourced(make_repo):
    repo = make_repo({
        "README.md": doc("cp .env.example .env", "source .env", "python3 check.py"),
        ".env.example": 'API_URL=http://localhost:8000\n# comment\nexport DEBUG="1 2"\n',
        "check.py": "import os\nassert os.environ['API_URL'] == 'http://localhost:8000'\nassert os.environ['DEBUG'] == '1 2'\n",
    })  # fmt: skip
    assert [s.status for s in run(repo).steps] == ["passed"] * 3


def test_sourcing_something_that_is_not_an_env_file_fails_safely(make_repo):
    repo = make_repo({"README.md": doc("source setup.sh"), "setup.sh": "rm -rf ~\n"})
    report = run(repo)
    assert rules(report) == [("RP101", 4)]
    assert "only assignments and `export` are supported" in report.findings[0].message


def test_redirections_write_inside_the_workspace_only(make_repo):
    repo = make_repo({
        "README.md": doc("echo hello > greeting.txt", "echo world >> greeting.txt", "python3 read.py"),
        "read.py": "assert open('greeting.txt').read() == 'hello\\nworld\\n'\n",
    })  # fmt: skip
    assert [s.status for s in run(repo).steps] == ["passed"] * 3
    outside = Path(os.path.realpath(repo.parent)) / "outside.txt"
    escaping = make_repo({"README.md": doc(f"echo hi > {outside}")}, name="escaping")
    report = run(escaping)
    assert outcome(report) == [(4, "skipped")] and "outside the workspace" in report.steps[0].reason
    assert not outside.exists()


def test_clone_and_cd_use_the_local_checkout(make_repo):
    repo = make_repo({
        "README.md": doc("git clone https://github.com/example/project.git && cd project && python3 hello.py"),
        "hello.py": "",
    })  # fmt: skip
    assert outcome(run(repo)) == [(4, "passed")]
    split = make_repo({
        "README.md": doc("git clone git@github.com:example/project.git my-dir", "cd my-dir", "python3 hello.py"),
        "hello.py": "",
    }, name="split")  # fmt: skip
    report = run(split)
    assert outcome(report) == [(4, "skipped"), (5, "passed"), (6, "passed")]
    assert report.steps[0].reason == "replaced by the local checkout"


def test_missing_directories_and_activate_scripts_get_a_suggestion(make_repo):
    repo = make_repo({"README.md": doc("source venv/bin/activate"), "venv2/bin/activate": ""})
    (finding,) = run(repo).findings
    assert finding.rule_id == "RP101" and "no such file: venv/bin/activate" in finding.message
    assert finding.hint == "Did you mean `venv2/bin/activate`?"
    other = make_repo({"README.md": doc("cd projct"), "project/x": ""}, name="other")
    (finding,) = run(other).findings
    assert "no such directory: projct" in finding.message and "`project`" in finding.hint


def test_a_line_runs_only_if_every_command_on_it_is_approved(make_repo):
    repo = make_repo({"README.md": doc("touch ran-first && mytool --go")})
    report = run(repo, keep_workspace=True)
    work = Path(report.isolation["kept_workspaces"][0]) / "work"
    try:
        assert outcome(report) == [(4, "skipped")] and not (work / "ran-first").exists()
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def test_blocked_commands_never_execute(make_repo):
    marker = Path(os.path.realpath(make_repo({"x": ""}).parent)) / "sudo-marker"
    repo = make_repo({"README.md": doc(f"sudo touch {marker}", "rm -rf ../..", "chmod 777 /etc/hosts")}, name="blocked")
    report = run(repo, keep_workspace=True)
    root = Path(report.isolation["kept_workspaces"][0])
    try:
        assert [s.status for s in report.steps] == ["skipped"] * 3
        assert "never run" in report.steps[0].reason and "outside the workspace" in report.steps[1].reason
        assert not marker.exists() and (root / "work").is_dir() and (root / "home").is_dir()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_timeouts_become_rp102(make_repo):
    repo = make_repo({"README.md": doc("python3 slow.py"), "slow.py": "import time\ntime.sleep(30)\n"})
    report = run(repo, config(timeout=1))
    (finding,) = report.findings
    assert (finding.rule_id, report.steps[0].status) == ("RP102", "timeout")
    assert "did not finish within 1s" in finding.message and report.steps[0].duration < 10


def test_a_command_that_is_not_installed_becomes_rp103(make_repo):
    cfg = config()
    cfg.allow = [AllowRule("no-such-tool-xyz *")]
    report = run(make_repo({"README.md": doc("no-such-tool-xyz --version")}), cfg)
    (finding,) = report.findings
    assert finding.rule_id == "RP103" and "no-such-tool-xyz" in finding.message and finding.hint


def test_commands_shipped_by_the_project_are_approved(make_repo):
    repo = make_repo({
        "README.md": doc("mydemo --help"),
        "pyproject.toml": '[project]\nname = "mydemo"\nversion = "1"\n[project.scripts]\nmydemo = "mydemo:main"\n',
    })  # fmt: skip
    # Approved by policy, then reported as not installed because no earlier step installed it.
    assert rules(run(repo)) == [("RP103", 4)]


def test_secrets_are_not_inherited_and_never_reach_the_report(make_repo):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    repo = make_repo({
        "README.md": doc("python3 check.py https://user:hunter2@example.com/x"),
        "check.py": (
            "import os, sys\n"
            "assert 'MY_API_TOKEN' not in os.environ and 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
            "assert os.environ['HOME'] != os.environ['REAL_HOME']\n"
            "assert os.environ['REPOPROOF_ACTIVE'] == '1'\n"
            f"print('leaking {token}')\n"
            "sys.exit(1)\n"
        ),
    })  # fmt: skip
    host = {**os.environ, "MY_API_TOKEN": "tok-abcdef-123456", "AWS_SECRET_ACCESS_KEY": "aws-secret-value", "REAL_HOME": os.environ.get("HOME", "/")}
    cfg = config()
    cfg.sandbox.pass_env = ["REAL_HOME"]
    report = run(repo, cfg, host_env=host)
    (finding,) = report.findings
    assert finding.exit_code == 1  # the environment assertions passed; only the final exit(1) failed
    assert token not in finding.output and "***REDACTED***" in finding.output
    assert "hunter2" not in finding.command and "hunter2" not in report.steps[0].command


def test_huge_output_is_truncated_to_the_tail(make_repo):
    repo = make_repo({"README.md": doc("python3 noisy.py"), "noisy.py": "for i in range(50000): print('row', i)\nraise SystemExit(1)\n"})
    (finding,) = run(repo, config(max_output_bytes=1024)).findings
    assert len(finding.output) < 1400 and "truncated" in finding.output and "row 49999" in finding.output


def test_failures_are_not_blamed_on_the_network_unless_they_look_like_it(make_repo):
    repo = make_repo({"README.md": doc("python3 plain.py", "python3 offline.py"), "plain.py": "raise SystemExit(1)", "offline.py": "print('Temporary failure in name resolution'); raise SystemExit(1)"})
    first, second = run(repo, config(keep_going=True)).findings
    assert first.hint is None
    if network_namespace_support()[0] is not None:
        assert "without network access" in second.hint
    _, open_second = run(repo, config(keep_going=True, network="allow")).findings
    assert "could not reach the network or a package index" in open_second.hint


def test_the_network_policy_is_recorded_per_step(make_repo):
    repo = make_repo({"README.md": doc("python3 a.py", "python3 b.py"), "a.py": "", "b.py": ""})
    cfg = config()
    cfg.allow = [AllowRule("python3 b.py", network=True)]
    report = run(repo, cfg)
    expected_first = "denied" if network_namespace_support()[0] is not None else "unenforced"
    assert [s.network for s in report.steps] == [expected_first, "allowed"]
    open_policy = config(network="allow")
    assert {s.network for s in run(repo, open_policy).steps} == {"allowed"}


def test_unenforceable_isolation_warns_by_default_and_fails_when_strict(make_repo, monkeypatch):
    monkeypatch.setattr(sandbox_module, "network_namespace_support", lambda: (None, "no namespaces here"))
    repo = make_repo({"README.md": doc("python3 a.py"), "a.py": ""})
    relaxed = run(repo)
    assert relaxed.steps[0].status == "passed" and relaxed.steps[0].network == "unenforced"
    assert relaxed.isolation["warnings"] == ["network restriction could not be enforced (no namespaces here)"]
    strict = run(repo, config(strict=True))
    assert rules(strict) == [("RP105", 4)] and "no namespaces here" in strict.findings[0].message


def test_rewrites_replace_documented_commands(make_repo):
    repo = make_repo({"README.md": doc("pipx install demo"), "install_local.py": "open('installed', 'w').close()\n"})
    assert outcome(run(repo)) == [(4, "skipped")]
    cfg = config()
    cfg.rewrite = [RewriteRule("pipx install demo", "python3 install_local.py")]
    assert outcome(run(repo, cfg)) == [(4, "passed")]


def test_skip_directives_and_skip_patterns(make_repo):
    text = (
        f"<!-- repoproof:skip -->\n{FENCE}sh\npython3 skipped-by-directive.py\n{FENCE}\n"
        f"{FENCE}sh\npython3 skipped-by-config.py\npython3 runs.py\n{FENCE}\n"
    )
    repo = make_repo({"README.md": text, "runs.py": ""})
    cfg = config()
    cfg.skip = ["python3 skipped-by-config.py"]
    report = run(repo, cfg)
    assert [(s.status, s.reason) for s in report.steps] == [
        ("skipped", "skipped by <!-- repoproof:skip -->"),
        ("skipped", "skipped by [commands] skip"),
        ("passed", None),
    ]


def test_every_document_gets_a_fresh_workspace(make_repo):
    repo = make_repo({
        "README.md": doc("touch marker.txt"),
        "docs/guide.md": doc("python3 absent.py"),
        "absent.py": "import os\nassert not os.path.exists('marker.txt')\n",
    })  # fmt: skip
    cfg = config()
    cfg.docs = ["README.md", "docs/guide.md"]
    report = run(repo, cfg)
    assert [(s.doc, s.status) for s in report.steps] == [("README.md", "passed"), ("docs/guide.md", "passed")]


def test_missing_documentation_is_a_finding(make_repo):
    empty = make_repo({"main.py": ""})
    report = run(empty)
    assert [(f.rule_id, f.level.value) for f in report.findings] == [("RP001", "warning")]
    assert report.exit_code("error") == 0 and report.exit_code("warning") == 1
    cfg = config()
    cfg.docs = ["docs/setup.md"]
    explicit = run(empty, cfg)
    assert [(f.rule_id, f.level.value) for f in explicit.findings] == [("RP001", "error")]
    escaping = config()
    escaping.docs = ["../elsewhere.md"]
    assert "outside the repository" in run(empty, escaping).findings[0].message


def test_a_readme_without_shell_blocks_needs_no_workspace(make_repo):
    report = run(make_repo({"README.md": "# Hi\n\n```python\nprint(1)\n```\n"}))
    assert report.steps == [] and report.findings == [] and report.isolation["backend"] == "none"


def venv_available() -> bool:
    try:
        import ensurepip  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not venv_available(), reason="venv/ensurepip is unavailable")
def test_activating_a_virtualenv_the_readme_created_really_switches_environments(make_repo):
    repo = make_repo({
        "README.md": doc("python3 -m venv .venv", "source .venv/bin/activate", "python3 prefix.py"),
        "prefix.py": "import sys\nassert sys.prefix.endswith('.venv'), sys.prefix\n",
    })  # fmt: skip
    report = run(repo)
    assert [s.status for s in report.steps] == ["passed"] * 3, report.findings


@pytest.mark.skipif(not venv_available(), reason="venv/ensurepip is unavailable")
def test_a_pristine_virtualenv_is_created_on_demand(make_repo):
    repo = make_repo({
        "README.md": doc("python3 prefix.py"),
        "prefix.py": "import os, sys\nassert sys.prefix != sys.base_prefix\nassert 'VIRTUAL_ENV' in os.environ\n",
    })  # fmt: skip
    report = run(repo, config(python="auto"))
    assert [s.status for s in report.steps] == ["passed"], report.findings


def test_running_commands_is_refused_where_it_is_unsupported(make_repo, monkeypatch):
    from repoproof import engine

    monkeypatch.setattr(engine, "_supports_execution", lambda: False)
    repo = make_repo({"README.md": doc("python3 hello.py"), "hello.py": ""})
    report = run(repo)
    assert report.steps == [] and [f.rule_id for f in report.findings] == ["RP104"]
    assert "Linux and macOS" in report.findings[0].message and "WSL" in report.findings[0].hint
    assert run(repo, dry_run=True).findings == []  # planning needs no execution support
