from __future__ import annotations

import pytest

from repoproof.markdown import github_slug, parse_markdown


def blocks(text: str):
    return parse_markdown(text).code_blocks


def test_fenced_blocks_with_lines_and_language():
    doc = "intro\n\n```bash\npip install foo\npytest\n```\n\n~~~console title=x\n$ ls\n~~~\n"
    first, second = blocks(doc)
    assert (first.lang, first.start_line) == ("bash", 3)
    assert first.lines == ((4, "pip install foo"), (5, "pytest"))
    assert (second.lang, second.start_line, second.lines) == ("console", 8, ((9, "$ ls"),))


def test_indented_fences_inside_lists_are_dedented():
    doc = "1. Install:\n\n   ```sh\n   pip install foo\n     --upgrade\n   ```\n"
    (block,) = blocks(doc)
    assert block.lines == ((4, "pip install foo"), (5, "  --upgrade"))


def test_closing_fence_must_be_at_least_as_long_and_same_character():
    doc = "````md\n```bash\nnot a block\n```\n````\n"
    (block,) = blocks(doc)
    assert block.lang == "md"
    assert [text for _, text in block.lines] == ["```bash", "not a block", "```"]
    tilde = "```text\n~~~\nstill inside\n```\n"
    assert len(blocks(tilde)) == 1


def test_unterminated_fence_runs_to_the_end():
    (block,) = blocks("```bash\npip install foo\n")
    assert block.lines == ((2, "pip install foo"),)


def test_inline_triple_backticks_are_not_a_fence():
    assert blocks("use ```code``` inline\n") == []


@pytest.mark.parametrize(
    "info,lang",
    [("bash", "bash"), ("{.bash}", "bash"), ("sh copy", "sh"), ("Shell-Session", "shell-session"), ("", "")],
)
def test_language_detection(info, lang):
    assert blocks(f"```{info}\nx\n```\n")[0].lang == lang


def test_directive_applies_only_to_the_next_block():
    doc = "<!-- repoproof:skip -->\n```sh\na\n```\n\n```sh\nb\n```\n"
    first, second = blocks(doc)
    assert first.directives == {"skip"} and second.directives == frozenset()


def test_directive_is_dropped_when_text_intervenes():
    doc = "<!-- repoproof:skip -->\nSome text.\n```sh\na\n```\n"
    assert blocks(doc)[0].directives == frozenset()


def test_directive_survives_blank_lines_and_other_comments():
    doc = "<!-- repoproof:skip -->\n\n<!-- note -->\n```sh\na\n```\n"
    assert blocks(doc)[0].directives == {"skip"}


def test_html_comments_hide_blocks_and_links():
    doc = "<!-- [x](hidden.md)\n```sh\nhidden\n```\n-->\n[y](shown.md)\n"
    parsed = parse_markdown(doc)
    assert parsed.code_blocks == []
    assert [link.target for link in parsed.links] == ["shown.md"]


def test_links_of_every_kind_with_line_numbers():
    doc = (
        "[a](docs/a.md) and ![img](img/b.png)\n"
        "[ref][r] <a href=\"c.md\">c</a> <img src='d.svg'>\n"
        "[nested](weird_(name).md) [angle](<has space.md>)\n"
        "[![badge](badge.svg)](https://ci.example)\n"
        "\n[r]: e.md \"Title\"\n"
    )
    found = [(x.target, x.line, x.is_image, x.kind) for x in parse_markdown(doc).links]
    assert ("docs/a.md", 1, False, "inline") in found
    assert ("img/b.png", 1, True, "inline") in found
    assert ("c.md", 2, False, "html") in found
    assert ("d.svg", 2, True, "html") in found
    assert ("weird_(name).md", 3, False, "inline") in found
    assert ("has space.md", 3, False, "inline") in found
    assert ("badge.svg", 4, True, "inline") in found
    assert ("https://ci.example", 4, False, "inline") in found
    assert ("e.md", 6, False, "reference") in found


def test_links_in_code_spans_and_fences_are_ignored():
    doc = "`[no](code-span.md)` text\n\n```\n[no](fenced.md)\n```\n"
    assert parse_markdown(doc).links == []


def test_headings_atx_setext_and_front_matter():
    doc = "---\ntitle: x\n---\n# One #\n\nTwo\n===\n\nThree\n---\n\n### Four\n"
    headings = parse_markdown(doc).headings
    assert [(h.level, h.text, h.line) for h in headings] == [
        (1, "One", 4),
        (1, "Two", 6),
        (2, "Three", 9),
        (3, "Four", 12),
    ]


def test_duplicate_headings_get_numbered_slugs():
    doc = "## Usage\n\n## Usage\n\n## Usage\n"
    assert [h.slug for h in parse_markdown(doc).headings] == ["usage", "usage-1", "usage-2"]


def test_html_anchors_count():
    doc = '<a name="legacy"></a>\n<h2 id="custom">X</h2>\n'
    assert {"legacy", "custom"} <= parse_markdown(doc).anchors


@pytest.mark.parametrize(
    "heading,slug",
    [
        ("Hello, World!", "hello-world"),
        ("Foo & Bar", "foo--bar"),
        ("🚀 Quick start", "-quick-start"),
        ("Done ✅", "done-"),
        ("`__init__` method", "__init__-method"),
        ("C++ / Rust", "c--rust"),
        ("**Bold** and _italic_", "bold-and-italic"),
        ("A [link](http://x.y) here", "a-link-here"),
        ("snake_case stays", "snake_case-stays"),
        ("Über uns", "über-uns"),
    ],
)
def test_github_slugs(heading, slug):
    assert github_slug(heading) == slug
