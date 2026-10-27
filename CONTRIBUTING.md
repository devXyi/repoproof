# Contributing to RepoProof

Thank you for helping. RepoProof is small enough to understand in an afternoon, and
contributions of every size are welcome: a typo fix, a new test, a new allowlisted command, a
new rule. If this is your first open-source contribution, you are in the right place.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Set up in five minutes

```bash
git clone https://github.com/devXyi/repoproof
cd repoproof
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

You need Python 3.10 or newer on Linux or macOS. The tests take about twenty seconds. Some
skip themselves when your machine cannot do something (for example network namespaces).

## A map of the code

```text
src/repoproof/
  cli.py            command-line interface (argparse): check, init, rules
  engine.py         runs the checks and builds the Report
  config.py         repoproof.toml loading and validation
  models.py         Finding, StepResult, Report and the catalogue of rules
  markdown.py       tiny Markdown scanner: code blocks, links, headings, anchors
  shellparse.py     strict shell-command parser (no shell is ever invoked)
  policy.py         allowlist, blocklist and path vetting
  journey.py        extracts commands from docs and runs them step by step
  sandbox.py        temporary workspace, scrubbed environment, process runner
  redact.py         secret redaction
  checks/links.py   link and anchor check
  checks/env.py     environment-variable consistency check
  reporters/        text, JSON, SARIF and Markdown output
  github.py         annotations, job summary, sticky pull request comment
tests/              one test module per source module, plus end-to-end journeys
docs/               user documentation (rules.md is generated)
examples/           the broken-project demo, also used as a test fixture
```

Two design rules matter more than any style guide:

1. **Deterministic first.** Checks must give the same answer for the same input. No network
   calls, no model calls, no timing-dependent behaviour in a check.
2. **Never run what was not approved.** Anything that executes a command must go through
   `policy.py`. If you are unsure whether a change widens what can run, say so in the pull
   request and we will go through it together.

## Recipes

### Allow another well-known command

1. Add it to `_builtin_rules()` in `src/repoproof/policy.py` with the right phase and whether
   it needs the network.
2. Add a row to the `test_builtin_rules` table in `tests/test_policy.py`.
3. Mention it in the allowlist table in `docs/checks.md`.

### Add a rule

1. Add a `RuleInfo` to `RULES` in `src/repoproof/models.py` (pick the next free ID in the right
   hundred: 1xx commands, 2xx links, 3xx environment).
2. Emit a `Finding` with that ID from the check.
3. Add tests that show the finding and, just as important, show it does *not* fire on valid
   input.
4. Regenerate the reference: `repoproof rules --format markdown > docs/rules.md`
   (a test fails if you forget).

### Teach the environment check a new language

Add a pattern to `checks/env.py`, decide what counts as "required" for that language, and add
a case to `tests/test_env.py`. Keep it conservative: a missed read is better than a false alarm.

### Add an ecosystem adapter (a clean environment for Node.js, Go or Rust)

This is the biggest open area. The Python virtualenv is created lazily in
`Sandbox.ensure_venv` and activated in `journey.py`; an adapter would do the equivalent for
another ecosystem (for example, a private npm cache and prefix). Open an issue first so we
can agree on the shape.

## Good first issues

Look for the `good first issue` label. Ideas that are always welcome:

- more cases in the shell parser tests, especially real-world README one-liners that are
  skipped as unsupported but could be supported safely;
- allowlist rules for another package manager or test runner;
- better "did you mean" hints for common documentation mistakes;
- a README of your own project that RepoProof reports something surprising about. Open an
  issue with the finding (redact anything private); real-world false positives are gold.

## Pull requests

- Keep changes focused, and include tests for behaviour changes.
- Run `pytest` and `ruff check .` before pushing.
- Update the documentation and `CHANGELOG.md` (under "Unreleased") for anything users will see.
- The repository verifies its own README in CI. If you change a command in the README, make
  sure it still works.

## Releasing (maintainers)

1. Move the "Unreleased" notes in `CHANGELOG.md` under a new version heading and bump
   `__version__` in `src/repoproof/__init__.py`.
2. Merge, then push a tag such as `v0.1.0`. The release workflow publishes to PyPI with
   trusted publishing, creates the GitHub release and moves the floating major tag
   (`v0`, later `v1`) that `uses: devXyi/repoproof@v0` follows.
3. After the first release, delete the "Pre-release" note in the README and the matching line in
   `docs/quickstart.md`.
