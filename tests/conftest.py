"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Tests must not depend on, or be affected by, the CI environment they run in."""
    for name in (
        "REPOPROOF_ACTIVE",
        "REPOPROOF_DEBUG",
        "GITHUB_ACTIONS",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_PATH",
        "GITHUB_SHA",
        "NO_COLOR",
        "FORCE_COLOR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """Create a repository directory from a ``{relative path: text}`` mapping."""

    def make(files: dict[str, str], name: str = "repo") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return root

    return make
