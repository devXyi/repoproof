# How the checks work

RepoProof runs three deterministic checks. Nothing here uses a language model: given the same
repository and environment, you get the same findings.

## Commands

### Which commands are extracted

Fenced code blocks in each documentation file, with their line numbers:

| Block language | What counts as a command |
| --- | --- |
| `sh`, `bash`, `shell`, `zsh`, `ksh` | Every line except comments and blanks. A leading `$ ` prompt is removed. |
| `console`, `shell-session`, `terminal`, or no language | Only lines that start with `$ `. The rest is treated as output. |
| anything else (`python`, `yaml`, `powershell`, ...) | Ignored. |

Fences inside list items work, backslash continuations and multi-line quotes are joined into
one command, and a heredoc is reported as unsupported with its body skipped. A
`<!-- repoproof:skip -->` comment on the line before a block leaves that block out.

### How a command is understood

Commands are never passed to a shell. A small parser turns each line into an argument list,
and RepoProof executes that list directly, so what was vetted is exactly what runs. It
understands quoting, `VAR=value` prefixes, `$VAR` and `${VAR:-default}`, `~`, globs, `&&` and
`;` (both mean "stop at the first failure"), and redirections to files (`>`, `>>`, `2>`,
`&>`, `2>&1`). It refuses pipes, `||`, background jobs, subshells, command substitution,
heredocs, input redirection, brace expansion and control flow; such lines are reported as
skipped with the reason.

### Which commands may run

Every simple command is classified before anything on its line runs. A line is verified only
if every command on it is approved; otherwise the whole line is skipped.

| Verdict | Meaning |
| --- | --- |
| **allowed** | Matches the built-in rules, a `[commands] allow` pattern, or a command the project ships |
| **skipped by config** | Matches `[commands] skip` |
| **blocked** | Privilege escalation, system package managers and similar. Never runs, whatever the configuration says |
| **unlisted** | Anything else. Skipped until someone approves it |

The built-in rules cover the usual setup, build and test commands:

| Ecosystem | Allowed out of the box |
| --- | --- |
| Python | `python -m venv`, `pip install`, `pip list/show/freeze`, `python -m pip install`, `python -m build`, `pytest`, `python -m pytest`, `tox`, `nox`, `uv venv/sync/pip install/run pytest`, `poetry install/run pytest`, `pdm install`, `python script.py`, and `python -m <your package>` |
| Node.js | `npm install/ci/test/run build/run test`, `yarn install/test/build`, `pnpm install/test/build` |
| Go | `go mod download`, `go build/test/vet/run` |
| Rust | `cargo fetch/build/check/test/run` |
| General | `make`, plus file commands that stay inside the workspace: `cp mv rm mkdir touch cat ls chmod`, and `echo printf pwd true false` |

Commands that the project itself ships (`[project.scripts]` and `[tool.poetry.scripts]`) are
approved too. They still fail with `RP103` if no earlier step installed them.

### What is emulated

State carries from step to step like a terminal session, without a shell:

- `cd` moves within the workspace and never out of it (symlinks are resolved at run time);
- `export`, `unset` and `NAME=value` change the environment of later steps; `set` and
  `deactivate` are accepted and do nothing;
- `source <venv>/bin/activate` activates that virtualenv for later steps. If the file does not
  exist you get `RP101` and, when a near miss exists, a "did you mean" hint;
- `source .env` reads a file made only of assignments and `export` lines;
- `git clone <url> [dir]` is replaced by the local checkout, and a later `cd <dir>` goes to
  the workspace root.

### Journeys, failures and statuses

Each documentation file is one journey in its own fresh workspace. Steps run in order. After
the first failure the remaining steps are `not_run`, unless you set `keep_going`.

| Status | Meaning |
| --- | --- |
| `passed` | Every command on the line exited with status 0 |
| `failed` | A command failed (`RP101`), was not found (`RP103`) or the environment could not be prepared (`RP104`, `RP105`) |
| `timeout` | A command ran out of time (`RP102`) |
| `skipped` | Not approved, blocked, unsupported, skipped by you, or replaced; the reason is recorded |
| `not_run` | An earlier step failed |
| `planned` | Dry run: would run |

Skipped steps are information, not findings. When a later step fails and something was skipped
before it, the finding says so, because the skipped step is a likely cause.

## Links

Every relative link, image and reference definition in the documentation files is resolved
against the repository: Markdown `[text](target)`, `![alt](target)`, `[ref]: target` and HTML
`<a href>` / `<img src>`.

- Paths are matched case-sensitively, like GitHub does. A link that only works on a
  case-insensitive filesystem is an `RP201`, with a hint naming the real path.
- `/path` is relative to the repository root; other paths are relative to the document.
- Targets may be files or directories. Percent-encoding and `?query` suffixes are handled.
- `#anchor` is checked against the headings of the target Markdown file using GitHub's slug
  rules (lowercase, punctuation removed, spaces to hyphens, repeats numbered `-1`, `-2`), plus
  explicit `id=` and `name=` anchors. Matching ignores case. Missing anchors are `RP202`.
- `#L10`-style line anchors are not checked.
- External URLs (`https:`, `mailto:` and so on) and templated targets (`{{ ... }}`) are never
  fetched or checked. Links inside code spans, code blocks and HTML comments are not links.

## Environment variables

The check answers one question: would a newcomer know which variables to set?

**What counts as a read.**

| Language | Recognised | Counts as *required* |
| --- | --- | --- |
| Python (via `ast`) | `os.environ["X"]`, `os.environ.get("X")`, `os.getenv("X")`, and `environ` / `getenv` imported from `os` | Only the subscript form |
| JavaScript / TypeScript | `process.env.X`, `process.env["X"]`, `import.meta.env.X` | Unless `\|\|` or `??` follows |
| Go | `os.Getenv("X")`, `os.LookupEnv("X")` | `Getenv`, unless compared with `==` or `!=`; never `LookupEnv` |
| Rust | `env::var("X")`, `env!("X")`, `option_env!("X")` | `env!`, and `var` unless followed by `unwrap_or*`, `.ok()`, `is_ok` or `is_err`; never `option_env!` |

Assignments (`os.environ["X"] = ...`) and dynamic names are not reads. Test code, vendored code,
virtualenvs and build output are not scanned (see `[env]` to change that).

**Where it should be documented.** A variable is *documented* if its name appears as a word in
a documentation file, or is listed (even commented out) in `.env.example`, `.env.sample`,
`.env.template`, `.env.dist` or a file named in `[env] examples`.

**Deployment configuration.** Variables set by `docker-compose*.yml` / `compose*.yml`
(`environment:` lists and maps, `${VAR}` interpolation, `env_file:`), Dockerfiles (`ENV`,
`ARG`) and Kubernetes-style manifests (`env:` entries with `name:`) under `k8s`, `kubernetes`,
`deploy`, `deployment`, `charts`, `helm` or `manifests`. Names must be `UPPER_CASE`.

| Rule | Condition | Severity |
| --- | --- | --- |
| `RP301` | Read by the code, documented nowhere | warning if any read is required, else note |
| `RP302` | Required, documented in the docs, but missing from an existing `.env.example` | warning |
| `RP303` | Listed in `.env.example` but never read by the code or deployment config | note |
| `RP304` | Required by the code but not set in deployment config that does set variables | note |

The check is heuristic. Variables read by a framework or library on its own (for example a
settings class with an `env_prefix`) are invisible to it, which is why `RP303` is only a note
and why `[env] ignore` exists. Ubiquitous variables (`PATH`, `HOME`, `CI`, `GITHUB_*`,
`PYTHON*`, `NODE_ENV` and similar) are ignored by default.
