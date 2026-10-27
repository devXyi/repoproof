from __future__ import annotations

from pathlib import Path

import pytest

from repoproof.cli import CONFIG_TEMPLATE
from repoproof.config import (
    CONFIG_FILENAME,
    AllowRule,
    Config,
    ConfigError,
    RewriteRule,
    load_config,
    parse_config,
)


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def test_no_file_means_defaults(tmp_path: Path):
    config = load_config(tmp_path)
    assert config.path is None and config.docs == [] and config.fail_on == "error"
    assert config.sandbox.network == "install" and config.sandbox.timeout == 300
    assert config.sandbox.timeouts["install"] == 900


def test_a_full_file_is_loaded(tmp_path: Path):
    path = write(
        tmp_path,
        """
version = 1
[repoproof]
docs = ["README.md", "docs/quickstart.md"]
fail_on = "warning"
[sandbox]
python = "none"
network = "deny"
timeout = 60
total_timeout = 600
keep_going = true
strict = true
pass_env = ["PIP_INDEX_URL"]
inherit_proxy = false
[sandbox.timeouts]
install = 1200
[commands]
allow = ["mytool *", { pattern = "make data", phase = "install", network = true, timeout = 30 }]
skip = ["repoproof check *"]
[[commands.rewrite]]
match = "pipx install repoproof"
run = "pip install ."
[links]
enabled = false
ignore = ["docs/generated/*"]
[env]
ignore = ["MY_*"]
include_tests = true
exclude = ["examples/*"]
examples = ["config/.env.sample"]
""",
    )
    config = load_config(tmp_path)
    assert config.path == path
    assert config.docs == ["README.md", "docs/quickstart.md"] and config.fail_on == "warning"
    box = config.sandbox
    assert (box.python, box.network, box.timeout, box.total_timeout) == ("none", "deny", 60, 600)
    assert box.keep_going and box.strict and not box.inherit_proxy
    assert box.pass_env == ["PIP_INDEX_URL"] and box.timeouts["install"] == 1200
    assert box.timeouts["test"] == 600  # untouched defaults survive
    assert config.allow == [
        AllowRule("mytool *"),
        AllowRule("make data", phase="install", network=True, timeout=30),
    ]
    assert config.skip == ["repoproof check *"]
    assert config.rewrite == [RewriteRule("pipx install repoproof", "pip install .")]
    assert not config.links.enabled and config.links.ignore == ["docs/generated/*"]
    assert config.env.ignore == ["MY_*"] and config.env.include_tests
    assert config.env.exclude == ["examples/*"] and config.env.examples == ["config/.env.sample"]


def test_the_generated_template_is_valid():
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib
    config = parse_config(tomllib.loads(CONFIG_TEMPLATE))
    expected = Config()
    expected.docs = ["README.md"]  # the template spells out the default document
    assert config == expected


def test_unknown_keys_are_errors_with_suggestions():
    with pytest.raises(ConfigError, match=r"unknown key 'netwrok' in \[sandbox\] \(did you mean 'network'\?\)"):
        parse_config({"sandbox": {"netwrok": "deny"}})
    with pytest.raises(ConfigError, match="unknown key 'docs' in top level"):
        parse_config({"docs": ["README.md"]})


@pytest.mark.parametrize(
    "data,message",
    [
        ({"repoproof": {"fail_on": "sometimes"}}, "fail_on must be one of"),
        ({"sandbox": {"network": "maybe"}}, "network must be one of"),
        ({"sandbox": {"timeout": 0}}, "timeout must be an integer >= 1"),
        ({"sandbox": {"timeout": "60"}}, "timeout must be an integer"),
        ({"sandbox": {"timeout": True}}, "timeout must be an integer"),
        ({"sandbox": {"keep_going": "yes"}}, "keep_going must be true or false"),
        ({"sandbox": {"timeouts": {"deploy": 5}}}, "unknown key 'deploy'"),
        ({"sandbox": {"pass_env": "PATH"}}, "pass_env must be a list"),
        ({"commands": {"allow": [5]}}, "strings or tables"),
        ({"commands": {"allow": [{"phase": "run"}]}}, "missing 'pattern'"),
        ({"commands": {"allow": [{"pattern": "x", "phase": "party"}]}}, "phase must be one of"),
        ({"commands": {"allow": [{"pattern": "x", "network": "yes"}]}}, "'network' must be true or false"),
        ({"commands": {"rewrite": [{"match": "a"}]}}, "'run' must be a non-empty string"),
        ({"commands": {"rewrite": [{"match": "a", "run": "a | b"}]}}, "not a runnable command"),
        ({"links": []}, "must be a table"),
        ({"version": 2}, "unsupported config version"),
    ],
)
def test_invalid_values_are_rejected(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_file_errors_mention_the_file(tmp_path: Path):
    write(tmp_path, "[sandbox\nnetwork = 'deny'")
    with pytest.raises(ConfigError, match="repoproof.toml: invalid TOML"):
        load_config(tmp_path)
    write(tmp_path, '[sandbox]\nnetwork = "nope"\n')
    with pytest.raises(ConfigError, match="repoproof.toml: .*network must be one of"):
        load_config(tmp_path)
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path, tmp_path / "missing.toml")


def test_an_explicit_config_path_wins(tmp_path: Path):
    other = tmp_path / "ci.toml"
    other.write_text('[repoproof]\nfail_on = "never"\n', encoding="utf-8")
    write(tmp_path, '[repoproof]\nfail_on = "note"\n')
    assert load_config(tmp_path, other).fail_on == "never"
