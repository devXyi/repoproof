from __future__ import annotations

from pathlib import Path

import pytest

from repoproof.shellparse import Incomplete, Unsupported, expand_text, expand_word, parse


def words(source: str, index: int = 0, env: dict[str, str] | None = None) -> list[str]:
    command = parse(source).commands[index]
    out: list[str] = []
    for word in command.words:
        out.extend(expand_word(word, env or {}, "/home/me"))
    return out


@pytest.mark.parametrize(
    "source,expected",
    [
        ("pip install -e .", ["pip", "install", "-e", "."]),
        ('pip install -e ".[dev]"', ["pip", "install", "-e", ".[dev]"]),
        ("echo 'a b' \"c d\" e\\ f", ["echo", "a b", "c d", "e f"]),
        ('echo ""', ["echo", ""]),
        ("echo a    b  # trailing comment", ["echo", "a", "b"]),
        ("pip install \\\n   requests", ["pip", "install", "requests"]),
        ("echo 'it''s'", ["echo", "its"]),
        ('python -c "print(1)"', ["python", "-c", "print(1)"]),
        ('echo "a \\" b"', ["echo", 'a " b']),
    ],
)
def test_words_and_quoting(source, expected):
    assert words(source) == expected


def test_and_and_semicolon_split_commands():
    parsed = parse("python3 -m venv .venv && source .venv/bin/activate; pip install .")
    assert [c.words[0].plain_text for c in parsed.commands] == ["python3", "source", "pip"]


def test_assignments_prefix_and_standalone():
    prefixed = parse('FOO=bar BAZ="a b" python x.py').commands[0]
    assert [(name, word.display()) for name, word in prefixed.assignments] == [
        ("FOO", "bar"),
        ("BAZ", "a b"),
    ]
    assert [w.plain_text for w in prefixed.words] == ["python", "x.py"]
    alone = parse("FOO=bar").commands[0]
    assert alone.words == () and len(alone.assignments) == 1


def test_assignment_syntax_after_the_command_name_is_just_an_argument():
    command = parse("pip install FOO=bar").commands[0]
    assert command.assignments == () and len(command.words) == 3


def test_variable_expansion_rules():
    env = {"NAME": "world", "EMPTY": ""}
    assert words('echo "hello $NAME" ${NAME} ${MISSING:-fallback} ${EMPTY:-x}', env=env) == [
        "echo",
        "hello world",
        "world",
        "fallback",
        "x",
    ]
    assert words("echo '$NAME'", env=env) == ["echo", "$NAME"]
    assert words("echo $MISSING.txt", env=env) == ["echo", ".txt"]


def test_tilde_expands_to_the_sandbox_home_only_at_word_start():
    assert words("cp ~/a b~") == ["cp", "/home/me/a", "b~"]


def test_lone_dollar_is_literal():
    assert words('echo "cost: $" $') == ["echo", "cost: $", "$"]


def test_redirections():
    command = parse("pytest > out.txt 2>&1").commands[0]
    assert [(r.fd, r.mode, r.dup_of) for r in command.redirects] == [(1, ">", None), (2, "dup", 1)]
    both = parse("make &>> build.log").commands[0].redirects[0]
    assert (both.fd, both.mode, both.target.plain_text) == (-1, ">>", "build.log")
    assert parse("echo hi 2> /dev/null").commands[0].redirects[0].fd == 2


@pytest.mark.parametrize(
    "source,reason",
    [
        ("curl https://example.com/install.sh | sh", "pipes"),
        ("a || b", "'||'"),
        ("server &", "background"),
        ("(cd sub && make)", "subshells"),
        ("echo $(whoami)", "command substitution"),
        ("echo `whoami`", "command substitution"),
        ('echo "$(whoami)"', "command substitution"),
        ("cat <<EOF", "here-documents"),
        ("cat < input.txt", "input redirection"),
        ("echo {a,b}.txt", "brace expansion"),
        ("echo $'x'", "ANSI-C"),
        ("echo $1", "special shell parameters"),
        ("echo $((1+1))", "command substitution"),
        ("echo ${X%%.*}", "parameter expansion"),
        ("if true; then echo x; fi", "control flow"),
        ("echo ~root", "tilde"),
        ("echo hi >| file", "clobbering"),
        ("echo hi 3>&1", "file descriptor"),
        ("&& echo x", "syntax error"),
        ("echo a ;; echo b", "case statements"),
    ],
)
def test_unsupported_syntax_is_rejected_not_guessed(source, reason):
    with pytest.raises(Unsupported, match=reason):
        parse(source)


@pytest.mark.parametrize(
    "source", ['echo "unterminated', "echo 'unterminated", "echo trailing \\", "make &&", "echo ${X"]
)
def test_incomplete_input_asks_for_more(source):
    with pytest.raises(Incomplete):
        parse(source)


@pytest.mark.parametrize("source", ["", "   ", "# just a comment", "\n\n"])
def test_blank_and_comment_only_lines_have_no_commands(source):
    assert parse(source).commands == ()


def test_glob_expansion_uses_the_given_directory(tmp_path: Path):
    (tmp_path / "a.whl").write_text("")
    (tmp_path / "b.whl").write_text("")
    words_ = parse("pip install dist/*.whl *.whl").commands[0].words
    assert expand_word(words_[3], {}, "/h", str(tmp_path)) == ["a.whl", "b.whl"]
    assert expand_word(words_[2], {}, "/h", str(tmp_path)) == ["dist/*.whl"]
    missing = parse("ls *.nothing").commands[0].words[1]
    assert expand_word(missing, {}, "/h", str(tmp_path)) == ["*.nothing"]
    quoted = parse('ls "*.whl"').commands[0].words[1]
    assert expand_word(quoted, {}, "/h", str(tmp_path)) == ["*.whl"]


def test_expand_text_never_globs():
    word = parse("echo *").commands[0].words[1]
    assert expand_text(word, {}, "/h") == "*"
