# Reports and exit codes

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | No finding at or above `--fail-on` (default: `error`) |
| `1` | At least one finding at or above it |
| `2` | Usage or configuration error: a bad flag, an unknown config key, a path that is not a directory, or a nested run that was refused |
| `3` | Internal error. This is a bug; please report it |
| `130` | Interrupted |

`--fail-on` takes `error`, `warning`, `note` or `never`. Skipped commands are information and
never affect the exit code on their own.

## Choosing output

- By default a readable report goes to stdout and progress goes to stderr.
- `--format json|sarif|markdown` replaces the stdout report.
- `--json FILE`, `--sarif FILE` and `--markdown FILE` additionally write files. Parent
  directories are created and `-` means stdout.
- `-q` removes progress and the text report. Use it together with the file options.

## JSON

`schema_version` is `1`. New fields may be added in a minor release; existing ones keep their
meaning. This is a real report for the demo project, restricted to the link check with
`--only links`:

```json
{
  "schema_version": 1,
  "tool": {
    "name": "repoproof",
    "version": "0.1.0"
  },
  "root": "broken-project",
  "started_at": "2026-10-24T12:00:00+00:00",
  "duration_seconds": 0.004,
  "dry_run": false,
  "docs": [
    "README.md"
  ],
  "checks": [
    "links"
  ],
  "summary": {
    "errors": 1,
    "warnings": 1,
    "notes": 0,
    "steps": {
      "total": 0,
      "passed": 0,
      "failed": 0,
      "timeout": 0,
      "skipped": 0,
      "not_run": 0,
      "planned": 0
    }
  },
  "isolation": {},
  "findings": [
    {
      "rule_id": "RP201",
      "rule_name": "broken-link",
      "severity": "error",
      "message": "The link target 'docs/setup.md' does not exist",
      "file": "README.md",
      "line": 20,
      "command": null,
      "exit_code": null,
      "output": null,
      "hint": null
    },
    {
      "rule_id": "RP202",
      "rule_name": "broken-anchor",
      "severity": "warning",
      "message": "The anchor '#installation' was not found in README.md",
      "file": "README.md",
      "line": 21,
      "command": null,
      "exit_code": null,
      "output": null,
      "hint": "Anchors come from headings; renaming a heading breaks links to it."
    }
  ],
  "steps": []
}
```

| Field | Meaning |
| --- | --- |
| `summary` | Counts of findings by severity and of steps by status |
| `isolation` | What the sandbox could enforce: `backend`, `network_policy`, `network_enforceable`, `python_env`, `strict` and any `warnings` |
| `findings[]` | `rule_id`, `rule_name`, `severity`, `message`, `file`, `line`, and for commands `command`, `exit_code`, `output` (redacted tail) and `hint` |
| `steps[]` | One entry per documented command line: `doc`, `line`, `command`, `status`, `reason`, `phase`, `exit_code`, `duration_seconds`, `network` (`denied`, `allowed` or `unenforced`) and `output` for failures |

Step `status` is one of `passed`, `failed`, `timeout`, `skipped`, `not_run` or `planned`.

## SARIF

`--sarif FILE` writes a SARIF 2.1.0 log with one run. Every rule is described in
`tool.driver.rules` with a help link into the [rule reference](rules.md); results carry the
file and line, the severity as `level`, and a stable fingerprint so GitHub code scanning can
track a finding as lines move. Upload it with:

```yaml
- uses: devXyi/repoproof@v0
  with:
    sarif-file: repoproof.sarif
    fail-on: never # let code scanning decide
- uses: github/codeql-action/upload-sarif@v4
  if: always()
  with:
    sarif_file: repoproof.sarif
    category: repoproof
```

The upload needs the `security-events: write` permission.

## Markdown and pull request comments

`--markdown FILE` (or `--format markdown`) renders what the action posts: a headline, each
error with the exact command, its output and a hint, and a collapsed list of warnings and
notes. The first line is the hidden marker `<!-- repoproof:report -->`, which is how the action
finds and updates its own comment instead of adding a new one on every push. Output is
shortened to fit GitHub's comment size limit.

`--github-comment on-failure` posts or updates the comment when there are findings and
only updates an existing comment on a clean run (so it flips to "the documented setup works"
without adding noise). `--github-comment always` also creates the comment on a clean run. It
needs `GITHUB_TOKEN` and a pull request context, and a failure to post is a warning on
stderr, never a failed check.

## GitHub annotations and job summary

When `GITHUB_ACTIONS=true` RepoProof prints a workflow command for each finding (most serious
first, at most 50), for example:

```text
::error file=README.md,line=17,title=RP101 command-failed::`python3 app.py` exited with status 1
```

and appends the Markdown report to the job summary. `--no-github` turns both off. With
`--format json`, `sarif` or `markdown` the annotations go to stderr so stdout stays machine
readable.
