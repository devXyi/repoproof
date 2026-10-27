# Five-minute quickstart

This walkthrough takes a repository from "I hope the README still works" to "verified on every
pull request".

## 1. Install

```bash
pipx install repoproof
```

`pip install repoproof` works too. RepoProof needs Python 3.10 or newer on Linux or macOS (on
Windows, use WSL). Until the first release is on PyPI, install from source with
`pip install git+https://github.com/devXyi/repoproof`.

## 2. Preview what would run

From the root of your repository:

```bash
repoproof check . --dry-run
```

Nothing is executed. RepoProof lists every command it found in your README and what it would
do with it:

- `▸ README.md:12  pip install -e .  would run [install]` is approved and would run.
- `○ README.md:15  mytool --help  (...)` is skipped, and the reason says why: not on the
  allowlist, blocked (`sudo`, system package managers), unsupported shell syntax (pipes,
  command substitution), replaced by the local checkout (`git clone`), or skipped by you.

## 3. Run it

```bash
repoproof check .
```

RepoProof copies your repository to a temporary directory, runs the approved commands in order
and prints progress to stderr as each step starts. A real install can take a few minutes, so
the output is not buffered. The final report shows each step, the first failure with the end
of its output, and the link and configuration findings. The exit status is `1` when anything
at or above `--fail-on` (default `error`) was found.

## 4. Approve what you trust

Only well-known setup, build and test commands run by default. If your README says
`mytool --help` you will see it skipped as "not on the allowlist". Approve it in
`repoproof.toml` (`repoproof init` writes a commented starter file):

```toml
[commands]
allow = ["mytool *"]
```

Commands that your own package ships (`[project.scripts]` in `pyproject.toml`) are approved
automatically, as soon as an earlier step has installed them.

## 5. Fix what it finds

| You see | Usually means | Fix |
| --- | --- | --- |
| `RP101` on `source venv/bin/activate` | The README and the real virtualenv name disagree | Use the name in the hint |
| `RP103` command not found | A tool is needed but never installed | Add the install step or list the prerequisite |
| `RP101` with a traceback | The documented example is out of date | Update the example or the code |
| A skipped step before a failure | The failure may be caused by it | Approve it, or document why it is optional |
| `RP201` / `RP202` | A file moved or a heading was renamed | Update the link |
| `RP301` / `RP302` | The code reads a variable nobody documented | Document it in the README and `.env.example` |

To verify the checkout instead of the published package, add a rewrite so that a documented
`pip install mypackage` runs `pip install .`:

```toml
[[commands.rewrite]]
match = "pip install mypackage"
run = "pip install ."
```

## 6. Run it on every pull request

```yaml
- uses: actions/checkout@v7
- uses: devXyi/repoproof@v0
```

The action annotates the documentation lines that broke and keeps one pull request comment up
to date. See [github-action.md](github-action.md) for the inputs and permissions.

## Handy flags

| Flag | Purpose |
| --- | --- |
| `--keep-going` | Report every failing step instead of stopping at the first |
| `--only links,env` | Run just the fast, side-effect-free checks |
| `--format json` | Machine-readable output (`--sarif FILE` and `--markdown FILE` also exist) |
| `--keep-workspace` | Keep the temporary directory to inspect what the commands did |
| `--network deny` | Refuse network access for every documented command |

To leave a code block out of verification, put `<!-- repoproof:skip -->` on the line before it.
