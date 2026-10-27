from __future__ import annotations

from pathlib import Path

from repoproof.checks.links import check_links
from repoproof.config import LinksConfig
from repoproof.markdown import parse_markdown


def run(root: Path, *docs: str, ignore: list[str] | None = None):
    parsed = {name: parse_markdown((root / name).read_text(encoding="utf-8"), name) for name in docs}
    return check_links(root, parsed, LinksConfig(ignore=ignore or []))


def messages(findings):
    return [(f.rule_id, f.file, f.line, f.message) for f in findings]


def test_valid_links_produce_no_findings(make_repo):
    root = make_repo(
        {
            "README.md": (
                "# Title\n[guide](docs/guide.md) [dir](docs/) [img](img/logo.png) [top](#title)\n"
                "[abs](/docs/guide.md#usage) [ext](https://example.com/x) [mail](mailto:a@b.c)\n"
                "[line](src/app.py#L10-L20) [tpl]({{ url }}) [q](docs/guide.md?plain=1)\n"
            ),
            "docs/guide.md": "# Guide\n## Usage\n[back](../README.md#title)\n",
            "img/logo.png": "",
            "src/app.py": "",
        }
    )
    assert run(root, "README.md", "docs/guide.md") == []


def test_missing_targets_are_errors_with_locations(make_repo):
    root = make_repo({"README.md": "x\n\n[a](docs/missing.md)\n![b](img/none.png)\n[c](../../outside.md)\n"})
    found = messages(run(root, "README.md"))
    assert [(rule, line) for rule, _, line, _ in found] == [("RP201", 3), ("RP201", 4), ("RP201", 5)]
    assert "does not exist" in found[0][3] and "image" in found[1][3]
    assert "outside the repository" in found[2][3]


def test_links_are_relative_to_the_document(make_repo):
    root = make_repo({"docs/a.md": "[b](b.md) [up](../README.md) [bad](README.md)\n", "docs/b.md": "", "README.md": ""})
    found = run(root, "docs/a.md")
    assert [f.message for f in found] == ["The link target 'README.md' does not exist (looked for 'docs/README.md')"]


def test_paths_are_case_sensitive_like_github(make_repo):
    root = make_repo({"README.md": "[x](Docs/Guide.md)\n", "docs/Guide.md": ""})
    (finding,) = run(root, "README.md")
    assert finding.rule_id == "RP201"
    assert "differs only in case" in (finding.hint or "") and "docs/Guide.md" in finding.hint


def test_anchors_in_the_same_file_and_other_markdown_files(make_repo):
    root = make_repo(
        {
            "README.md": (
                "# Intro\n## Setup\n[ok](#setup) [bad](#install) [other](docs/a.md#nope) "
                "[ok2](docs/a.md#real-heading) [not-md](src/x.py#whatever)\n"
            ),
            "docs/a.md": "# Real heading\n",
            "src/x.py": "",
        }
    )
    found = run(root, "README.md")
    assert [(f.rule_id, f.message) for f in found] == [
        ("RP202", "The anchor '#install' was not found in README.md"),
        ("RP202", "The anchor '#nope' was not found in docs/a.md"),
    ]


def test_anchor_matching_ignores_case_and_percent_encoding(make_repo):
    root = make_repo({"README.md": "# Quick Start\n[a](#QUICK-START) [b](#quick%2Dstart)\n"})
    assert run(root, "README.md") == []


def test_links_in_code_are_not_links(make_repo):
    root = make_repo({"README.md": "`[a](missing.md)`\n\n```\n[b](missing.md)\n```\n"})
    assert run(root, "README.md") == []


def test_ignore_patterns(make_repo):
    root = make_repo({"README.md": "[a](generated/api.md) [b](other/missing.md)\n"})
    found = run(root, "README.md", ignore=["generated/*"])
    assert [f.line for f in found] == [1] and "other/missing.md" in found[0].message


def test_reference_definitions_and_html_links_are_checked(make_repo):
    root = make_repo({"README.md": '<a href="nope.md">x</a>\n\n[ref]: gone.md\n'})
    assert [f.line for f in run(root, "README.md")] == [1, 3]
