"""GitHub Actions integration: annotations, the job summary and a sticky pull-request comment.

Nothing here can fail a check. A comment that cannot be posted (a fork's read-only token, a
network hiccup) is reported as a warning on stderr and the run carries on, because the
annotations and the job summary already carry the same findings.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Finding, Severity
from .reporters.markdown import MARKER

MAX_ANNOTATIONS = 50
Transport = Callable[[str, str, dict[str, str], "bytes | None"], tuple[int, Any]]


def escape_data(value: str) -> str:
    """Escape a workflow-command message."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """Escape a workflow-command property value."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def format_annotation(finding: Finding) -> str:
    """One ``::error file=...::message`` workflow command."""
    level = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.NOTE: "notice"}[finding.level]
    props = []
    if finding.file:
        props.append(f"file={escape_property(finding.file)}")
    if finding.line:
        props.append(f"line={finding.line}")
    props.append(f"title={escape_property(f'{finding.rule_id} {finding.rule.name}')}")
    message = finding.message + (f" {finding.hint}" if finding.hint else "")
    return f"::{level} {','.join(props)}::{escape_data(message)}"


def format_annotations(findings: list[Finding]) -> list[str]:
    """Annotations for the most serious findings first, capped at GitHub's practical limit."""
    ranked = sorted(findings, key=lambda f: -f.level.rank)
    return [format_annotation(f) for f in ranked[:MAX_ANNOTATIONS]]


def write_step_summary(markdown: str, path: str | None = None) -> bool:
    """Append ``markdown`` to the job summary. Returns False when there is nowhere to write."""
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return False
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class GitHubContext:
    """What RepoProof needs to know about the surrounding GitHub Actions run."""

    repository: str | None = None
    sha: str | None = None
    token: str | None = None
    api_url: str = "https://api.github.com"
    server_url: str = "https://github.com"
    pr_number: int | None = None

    @property
    def repo_url(self) -> str | None:
        return f"{self.server_url}/{self.repository}" if self.repository else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubContext:
        env = os.environ if env is None else env
        sha = env.get("GITHUB_SHA")
        pr_number: int | None = None
        event_path = env.get("GITHUB_EVENT_PATH")
        if event_path:
            try:
                event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                event = {}
            pull = event.get("pull_request") if isinstance(event, dict) else None
            if isinstance(pull, dict):
                number = pull.get("number")
                pr_number = number if isinstance(number, int) else None
                head = pull.get("head")
                if isinstance(head, dict) and isinstance(head.get("sha"), str):
                    sha = head["sha"]  # the PR head is browsable; the merge commit is not
        return cls(
            repository=env.get("GITHUB_REPOSITORY"),
            sha=sha,
            token=env.get("GITHUB_TOKEN"),
            api_url=env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            server_url=env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
            pr_number=pr_number,
        )


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, Any]:
    """Minimal HTTP client; returns ``(status, decoded JSON or message)``."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def upsert_comment(
    context: GitHubContext,
    body: str,
    *,
    create: bool = True,
    transport: Transport = default_transport,
) -> tuple[bool, str]:
    """Create or update the sticky RepoProof comment on the pull request.

    With ``create=False`` an existing comment is updated but a missing one is not created
    (used to flip an old failure to "fixed" without adding noise on a clean run).
    Returns ``(ok, message)``; it never raises.
    """
    if not (context.token and context.repository and context.pr_number):
        return False, "not running on a pull request with a GITHUB_TOKEN"
    headers = {
        "Authorization": f"Bearer {context.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repoproof",
        "Content-Type": "application/json",
    }
    base = f"{context.api_url}/repos/{context.repository}/issues"
    existing: int | None = None
    for page in range(1, 11):
        status, data = transport(
            "GET", f"{base}/{context.pr_number}/comments?per_page=100&page={page}", headers, None
        )
        if status != 200 or not isinstance(data, list):
            return False, f"could not list pull request comments (HTTP {status})"
        for comment in data:
            if isinstance(comment, dict) and MARKER in (comment.get("body") or ""):
                existing = comment.get("id")
        if existing is not None or len(data) < 100:
            break
    payload = json.dumps({"body": body}).encode("utf-8")
    if existing is not None:
        status, _ = transport("PATCH", f"{base}/comments/{existing}", headers, payload)
    elif create:
        status, _ = transport("POST", f"{base}/{context.pr_number}/comments", headers, payload)
    else:
        return True, "no existing comment to update"
    if status in (200, 201):
        return True, "comment updated" if existing is not None else "comment created"
    hint = " (pull requests from forks get a read-only token)" if status == 403 else ""
    return False, f"could not post the comment (HTTP {status}){hint}"
