from __future__ import annotations

import json
from pathlib import Path

from repoproof.github import (
    GitHubContext,
    escape_data,
    escape_property,
    format_annotation,
    format_annotations,
    upsert_comment,
    write_step_summary,
)
from repoproof.models import Finding
from repoproof.reporters.markdown import MARKER


def test_workflow_command_escaping():
    assert escape_data("50% done\r\nnext") == "50%25 done%0D%0Anext"
    assert escape_property("a:b,c%d\n") == "a%3Ab%2Cc%25d%0A"


def test_annotations_use_github_levels_and_escape_everything():
    error = Finding("RP101", "failed: 100%\nsecond line", file="docs/a,b.md", line=7, hint="Try again.")
    assert format_annotation(error) == (
        "::error file=docs/a%2Cb.md,line=7,title=RP101 command-failed::failed: 100%25%0Asecond line Try again."
    )
    warning = Finding("RP202", "anchor", file="README.md", line=3)
    note = Finding("RP303", "stale")
    assert format_annotation(warning).startswith("::warning file=README.md,line=3,title=RP202")
    assert format_annotation(note) == "::notice title=RP303 env-stale-example::stale"


def test_annotations_are_ranked_and_capped():
    findings = [Finding("RP303", f"n{n}") for n in range(60)] + [Finding("RP101", "boom", file="README.md", line=1)]
    lines = format_annotations(findings)
    assert len(lines) == 50 and lines[0].startswith("::error ")


def test_step_summary(tmp_path: Path, monkeypatch):
    target = tmp_path / "summary.md"
    assert write_step_summary("first", str(target)) and write_step_summary("second", str(target))
    assert target.read_text() == "first\nsecond\n"
    assert write_step_summary("nowhere") is False
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    assert write_step_summary("third") and target.read_text().endswith("third\n")
    assert write_step_summary("x", str(tmp_path / "missing" / "dir" / "f")) is False


def test_context_from_a_pull_request_event(tmp_path: Path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42, "head": {"sha": "headsha"}}}))
    context = GitHubContext.from_env({
        "GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "mergesha", "GITHUB_TOKEN": "t",
        "GITHUB_EVENT_PATH": str(event), "GITHUB_API_URL": "https://ghe.example/api/v3/",
        "GITHUB_SERVER_URL": "https://ghe.example/",
    })  # fmt: skip
    assert (context.pr_number, context.sha, context.token) == (42, "headsha", "t")
    assert context.api_url == "https://ghe.example/api/v3" and context.repo_url == "https://ghe.example/o/r"


def test_context_outside_pull_requests(tmp_path: Path):
    push = tmp_path / "push.json"
    push.write_text(json.dumps({"ref": "refs/heads/main"}))
    context = GitHubContext.from_env({"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc", "GITHUB_EVENT_PATH": str(push)})
    assert context.pr_number is None and context.sha == "abc" and context.api_url == "https://api.github.com"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert GitHubContext.from_env({"GITHUB_EVENT_PATH": str(broken)}).pr_number is None
    assert GitHubContext.from_env({}).repo_url is None


class FakeGitHub:
    """A transport that records calls and replays canned responses."""

    def __init__(self, comments: list[list[dict]] | None = None, post_status: int = 201, list_status: int = 200):
        self.pages = comments or [[]]
        self.post_status = post_status
        self.list_status = list_status
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            page = int(url.rsplit("page=", 1)[1])
            data = self.pages[page - 1] if page <= len(self.pages) else []
            return self.list_status, data
        return (200 if method == "PATCH" else self.post_status), {}


CONTEXT = GitHubContext(repository="o/r", token="secret-token", pr_number=7)


def test_a_new_comment_is_created_when_none_exists():
    fake = FakeGitHub(comments=[[{"id": 1, "body": "unrelated"}]])
    ok, message = upsert_comment(CONTEXT, f"{MARKER}\nbody", transport=fake)
    assert (ok, message) == (True, "comment created")
    method, url, headers, body = fake.calls[-1]
    assert (method, url) == ("POST", "https://api.github.com/repos/o/r/issues/7/comments")
    assert json.loads(body) == {"body": f"{MARKER}\nbody"}
    assert headers["Authorization"] == "Bearer secret-token" and headers["User-Agent"] == "repoproof"


def test_the_existing_comment_is_updated_in_place():
    fake = FakeGitHub(comments=[[{"id": 5, "body": "x"}, {"id": 9, "body": f"old {MARKER}"}]])
    ok, message = upsert_comment(CONTEXT, f"{MARKER}\nnew", transport=fake)
    assert (ok, message) == (True, "comment updated")
    assert [(m, u.rsplit("/", 2)[-2:]) for m, u, *_ in fake.calls if m != "GET"] == [("PATCH", ["comments", "9"])]


def test_comments_are_found_on_later_pages():
    first = [{"id": n, "body": "x"} for n in range(100)]
    fake = FakeGitHub(comments=[first, [{"id": 500, "body": MARKER}]])
    assert upsert_comment(CONTEXT, MARKER, transport=fake) == (True, "comment updated")
    assert fake.calls[-1][1].endswith("/comments/500")


def test_clean_runs_only_update_an_existing_comment():
    fake = FakeGitHub()
    assert upsert_comment(CONTEXT, MARKER, create=False, transport=fake) == (True, "no existing comment to update")
    assert all(call[0] == "GET" for call in fake.calls)


def test_failures_are_reported_never_raised():
    forbidden = FakeGitHub(post_status=403)
    ok, message = upsert_comment(CONTEXT, MARKER, transport=forbidden)
    assert not ok and "HTTP 403" in message and "read-only token" in message
    assert upsert_comment(CONTEXT, MARKER, transport=FakeGitHub(list_status=500))[0] is False
    no_token = FakeGitHub()
    assert upsert_comment(GitHubContext(repository="o/r", pr_number=1), MARKER, transport=no_token)[0] is False
    assert upsert_comment(GitHubContext(repository="o/r", token="t"), MARKER, transport=no_token)[0] is False
    assert no_token.calls == []
