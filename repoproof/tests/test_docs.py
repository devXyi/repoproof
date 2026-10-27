"""The repository's own documentation must stay true. These tests are RepoProof eating its own food."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from repoproof.checks.links import check_links
from repoproof.config import (
    Config,
    EnvConfig,
    LinksConfig,
    SandboxConfig,
    load_config,
)
from repoproof.engine import CheckOptions, run_check
from repoproof.markdown import parse_markdown

REPO_ROOT = Path(__file__).resolve().parents[1]


def markdown_files() -> list[Path]:
    # examples/ holds deliberately broken documentation, so it is not part of this check.
    return sorted([*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/*.md"), *REPO_ROOT.glob(".github/*.md")])


def test_every_markdown_file_has_valid_links_and_anchors():
    docs = {}
    for path in markdown_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        docs[relative] = parse_markdown(path.read_text(encoding="utf-8"), relative)
    assert len(docs) >= 12  # README, CONTRIBUTING, SECURITY, ... and every file under docs/
    findings = check_links(REPO_ROOT, docs, LinksConfig())
    assert [(f.file, f.line, f.message) for f in findings] == []


def test_every_configuration_key_is_documented():
    documented = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    keys = set()
    for section in (Config, SandboxConfig, LinksConfig, EnvConfig):
        keys.update(field.name for field in dataclasses.fields(section))
    keys -= {"path", "sandbox", "links", "env"}  # internal or documented as tables
    missing = sorted(key for key in keys if f"`{key}`" not in documented)
    assert missing == [], f"undocumented configuration keys: {missing}"


def test_the_self_check_configuration_loads():
    config = load_config(REPO_ROOT)
    assert config.docs == ["README.md"]
    assert [rule.match for rule in config.rewrite] == ["pipx install repoproof", "repoproof check ."]


def test_the_readme_journey_is_fully_approved_under_the_self_check_configuration():
    """If this fails, the self-check workflow would skip or reject a command in the README."""
    report = run_check(
        REPO_ROOT,
        load_config(REPO_ROOT),
        CheckOptions(only=frozenset({"commands"}), dry_run=True),
    )
    steps = {step.command: step for step in report.steps}
    assert report.findings == []
    planned = [
        "pipx install repoproof",
        "repoproof check .",
        "python3 -m venv .venv",
        "source .venv/bin/activate",
        'pip install -e ".[dev]"',
        "pytest",
    ]
    for command in planned:
        assert steps[command].status == "planned", (command, steps[command].reason)
    clone = steps["git clone https://github.com/devXyi/repoproof && cd repoproof"]
    assert clone.status == "planned"  # the clone is replaced and the `cd` goes to the checkout
