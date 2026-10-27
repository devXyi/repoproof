from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoproof.config import AllowRule
from repoproof.policy import Policy, canonical_name, matches, path_inside

WS = "/ws"


def decide(command: str, policy: Policy | None = None, cwd: str = WS):
    return (policy or Policy()).decide(command.split(), cwd=cwd, workspace=WS)


@pytest.mark.parametrize(
    "command,phase,network",
    [
        ("python3 -m venv .venv", "setup", False),
        ("pip install -e .", "install", True),
        ("pip3 install -r requirements.txt", "install", True),
        ("python3.12 -m pip install .", "install", True),
        ("pytest", "test", False),
        ("pytest -q tests/", "test", False),
        ("python -m pytest -x", "test", False),
        ("uv sync", "install", True),
        ("npm ci", "install", True),
        ("npm test", "test", False),
        ("go test ./...", "test", True),
        ("cargo build --release", "build", True),
        ("make", "build", False),
        ("make test", "build", False),
        ("python examples/demo.py --fast", "run", False),
        ("./.venv/bin/python run.py", "run", False),
    ],
)
def test_builtin_rules(command, phase, network):
    decision = decide(command)
    assert decision.verdict == "allow", decision.reason
    assert (decision.phase, decision.network) == (phase, network)


@pytest.mark.parametrize(
    "command",
    ["sudo make install", "/usr/bin/sudo ls", "apt-get install libpq-dev", "brew install x",
     "mkfs.ext4 /dev/sda1", "systemctl restart x", "kill -9 1", "eval ls", "chown root x"],
)  # fmt: skip
def test_dangerous_commands_are_blocked(command):
    decision = decide(command)
    assert decision.verdict == "blocked"
    assert "never run" in decision.reason


def test_configuration_cannot_allow_a_blocked_command():
    policy = Policy(allow=[AllowRule("sudo *"), AllowRule("apt-get *")])
    assert decide("sudo make install", policy).verdict == "blocked"
    assert decide("apt-get update", policy).verdict == "blocked"


@pytest.mark.parametrize(
    "command",
    ["curl https://example.com", "bash install.sh", "./install.sh", "mytool --help",
     "python -c print(1)", "python -m http.server", "pip", "pipx install thing", "npx create-app"],
)  # fmt: skip
def test_unknown_commands_are_unlisted(command):
    decision = decide(command)
    assert decision.verdict == "unlisted"
    assert "allowlist" in decision.reason


def test_pip_family_needs_a_subcommand_on_the_allowlist():
    assert decide("pip uninstall requests").verdict == "unlisted"
    assert decide("pip download requests").verdict == "unlisted"


def test_user_allow_rules_and_tables():
    policy = Policy(
        allow=[AllowRule("mytool *"), AllowRule("make fetch-data", phase="install", network=True, timeout=99)]
    )
    assert decide("mytool run --fast", policy).verdict == "allow"
    fetch = decide("make fetch-data", policy)
    assert (fetch.verdict, fetch.phase, fetch.network, fetch.timeout) == ("allow", "install", True, 99)
    assert decide("mytool", policy).verdict == "unlisted"  # "mytool *" needs an argument


def test_skip_patterns_beat_allow_rules():
    policy = Policy(allow=[AllowRule("repoproof *")], skip=["repoproof check *"])
    assert decide("repoproof check .", policy).verdict == "skip"
    assert decide("repoproof --version", policy).verdict == "allow"


def test_project_scripts_and_modules_are_trusted():
    policy = Policy(project_scripts=frozenset({"mytool"}), project_modules=frozenset({"my_pkg"}))
    assert decide("mytool --help", policy).verdict == "allow"
    assert decide("python -m my_pkg.cli serve", policy).verdict == "allow"
    assert decide("python -m other_pkg", policy).verdict == "unlisted"


def test_a_project_script_cannot_shadow_a_blocked_command():
    policy = Policy(project_scripts=frozenset({"sudo"}))
    assert decide("sudo ls", policy).verdict == "blocked"


@pytest.mark.parametrize(
    "command,verdict",
    [
        ("cp .env.example .env", "allow"),
        ("cp a.txt /etc/passwd", "blocked"),
        ("cp -t /etc a.txt", "blocked"),
        ("rm -rf build", "allow"),
        ("rm -rf ..", "blocked"),
        ("rm -rf /", "blocked"),
        ("rm -rf -- /tmp/x", "blocked"),
        ("mkdir -p a/b/c", "allow"),
        ("chmod +x scripts/run.sh", "allow"),
        ("chmod 755 /usr/bin/x", "blocked"),
        ("cat README.md", "allow"),
        ("cat /etc/passwd", "blocked"),
        ("ls", "allow"),
        ("touch ../outside", "blocked"),
        ("echo hello", "allow"),
    ],
)
def test_file_commands_stay_inside_the_workspace(command, verdict):
    assert decide(command).verdict == verdict


def test_file_commands_resolve_relative_to_the_current_directory():
    assert decide("rm -rf ..", cwd="/ws/sub").verdict == "allow"
    assert decide("rm -rf ../..", cwd="/ws/sub").verdict == "blocked"


def test_path_commands_invoked_by_path_do_not_get_the_builtin_pass():
    assert decide("./rm -rf x").verdict == "unlisted"
    assert decide("/bin/rm -rf x").verdict == "unlisted"


@pytest.mark.parametrize(
    "name,expected",
    [("python3.12", "python"), ("python", "python"), ("pip3", "pip"), ("./venv/bin/python3", "python"),
     (".venv/bin/pip", "pip"), ("pytest", "pytest"), ("./rm", "./rm"), ("/bin/ls", "/bin/ls")],
)  # fmt: skip
def test_canonical_name(name, expected):
    assert canonical_name(name) == expected


@pytest.mark.parametrize(
    "pattern,text,expected",
    [
        ("pip install *", "pip install -e .[dev]", True),
        ("pip install *", "pip install", False),
        ("pytest", "pytest-cov", False),
        ("pip install -e .[dev]", "pip install -e .[dev]", True),
        ("pip install -e .[dev]", "pip install -e .d", False),
        ("*", "anything at all", True),
        ("make *", "make a\nb", True),
        ("a.b", "axb", False),
    ],
)
def test_matches_treats_only_star_as_a_wildcard(pattern, text, expected):
    assert matches(pattern, text) is expected


def test_network_policy():
    allowed = decide("pip install .")
    offline = decide("pytest")
    assert Policy.network_allowed(allowed, "install") is True
    assert Policy.network_allowed(offline, "install") is False
    assert Policy.network_allowed(allowed, "deny") is False
    assert Policy.network_allowed(offline, "allow") is True


def test_path_inside_follows_symlinks_only_when_asked(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    assert path_inside("link/file", str(workspace), str(workspace), resolve=False)
    assert not path_inside("link/file", str(workspace), str(workspace), resolve=True)
    assert path_inside("a/../b", str(workspace), str(workspace), resolve=False)
    assert not path_inside("a/../../b", str(workspace), str(workspace), resolve=False)
    assert not path_inside("bad\x00name", str(workspace), str(workspace), resolve=False)
    assert not path_inside(str(tmp_path / "ws-sibling"), str(workspace), str(workspace), resolve=False)
