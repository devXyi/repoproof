# Security model

RepoProof executes commands that a repository's documentation tells people to run. That is
useful, and it is also exactly the kind of thing that deserves a clear contract.

## The short version

- Documentation is **untrusted input**. Commands run only if they are explicitly approved.
- RepoProof **never invokes a shell**. It parses a command into arguments and executes them
  directly.
- Approved commands run in a **temporary copy** of the repository with a **scrubbed
  environment**, timeouts, bounded output and secret redaction, and **without network access
  where the platform allows and the step does not need it**.
- None of this makes *hostile code* safe. An approved `pip install .` executes the project's
  build backend, `pytest` executes its tests, and `make` executes its Makefile. If you would
  not run a repository's tests on your machine, do not run RepoProof on it there either:
  use a disposable VM or container. A GitHub-hosted runner is one.

## Threat model

| In scope | How it is handled |
| --- | --- |
| A README that says `sudo ...`, `curl ... \| sh`, `rm -rf ~`, `apt-get install ...` | Blocked or unsupported; never executed |
| A documented command with an unintended reach (`cp x ~/.bashrc`, `rm -rf ../..`, `echo x > /etc/y`) | Path arguments, redirections, `cd` and `source` are confined to the workspace; symlinks are resolved at run time |
| Secrets leaking into logs or pull request comments | Nothing is inherited; output, commands and reports are redacted |
| A pull request that changes the documentation to something nasty | Same as the first row. The allowlist applies to whatever the docs say |
| Runaway or hung commands | Timeouts, whole-process-group termination, bounded output |

| Out of scope | Why |
| --- | --- |
| Hostile code in approved commands | They run the repository's code by design |
| A hostile `repoproof.toml` | It can widen the allowlist, so treat it like CI configuration and protect it (for example with CODEOWNERS) |
| Resource exhaustion (CPU, memory, disk) | Only time and output are bounded |
| Reading files elsewhere on the machine | Repository code can open absolute paths; a temporary `HOME` only hides the usual places |

## Approval

1. **Parse.** The command is parsed by a strict parser (see [checks](checks.md)). Pipes,
   command substitution, heredocs, subshells, background jobs, `||`, brace expansion and other
   unsupported syntax are reported as skipped, never interpreted.
2. **Classify.** Each simple command is *allowed*, *skipped by config*, *blocked* or *unlisted*.
   Blocked commands are privilege escalation (`sudo`, `su`, `doas`, `pkexec`), ownership and
   device changes (`chown`, `dd`, `mount`, `mkfs*`), service and power control (`systemctl`,
   `shutdown`, `kill`, `crontab`), `eval`/`exec`, and system package managers (`apt`,
   `apt-get`, `dpkg`, `yum`, `dnf`, `rpm`, `apk`, `pacman`, `zypper`, `snap`, `brew`). No
   configuration, and no script a project declares, can allow them.
3. **All or nothing.** A line runs only if every command on it is approved.
4. **Confine paths.** `cp`, `mv`, `rm`, `mkdir`, `touch`, `cat`, `ls` and `chmod` accept only
   paths inside the workspace, and so do redirection targets, `cd` and `source`. A command
   invoked by path (`./rm`, `/bin/rm`) never gets the built-in treatment.
5. **Refuse dangerous variables.** `LD_*`, `DYLD_*`, `BASH_ENV`, `ENV`, `PYTHONSTARTUP`,
   `PYTHONHOME`, `IFS`, `SHELLOPTS` and `PS4` cannot be set by documentation.

## Containment

| Control | What it does | Where it applies |
| --- | --- | --- |
| Temporary workspace | The repository is copied; the original is never touched | Everywhere |
| Temporary `HOME`, `TMPDIR`, `XDG_*` | Dotfiles, caches and credentials in your home directory are not seen by default | Everywhere |
| Scrubbed environment | Only `PATH`, locale, a few non-secret tool settings and what you list in `pass_env` | Everywhere |
| Clean `PATH` | Your active virtualenv or conda environment is removed from `PATH` | Everywhere |
| Pristine virtualenv | Python tools run in a new environment | When `python` is not `"none"` |
| Timeouts, process group kill, output cap | A timed-out command and all its children are terminated; only the tail of the output is kept | POSIX |
| Network namespace | Steps that should not use the network run with no network (loopback is brought up when the `ip` tool exists) | Linux with unprivileged user namespaces |

### The network promise, precisely

`[sandbox] network = "install"` (the default) means: package installs and builds that need to
fetch dependencies may use the network; everything else, tests included, may not. RepoProof
enforces this with a Linux network namespace (`unshare --net --map-root-user`).

Where that is not available (macOS, containers or hosts that restrict unprivileged user
namespaces, such as systems where AppArmor blocks them), RepoProof cannot enforce the
restriction. By default it **warns, records `network: "unenforced"` on the step and carries
on**; with `--strict-isolation` (or `strict = true`) it **fails with `RP105`** instead. The JSON
report always says what was possible: `isolation.network_enforceable`.

Inside the namespace commands run as a mapped root user, which can change how some tools
behave (for example a warning from `pip`). It has no extra privileges on the host.

## Secrets

- Nothing from your environment reaches documented commands unless you list it in
  `[sandbox] pass_env`. Proxy and certificate settings are passed through by default so that
  installs work behind a corporate proxy; set `inherit_proxy = false` to stop that.
- Output, command lines and every report format are redacted. Redacted: the values of host
  variables whose names look sensitive (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`, ...)
  and of everything in `pass_env`; GitHub, AWS, Slack, Stripe and Google credentials; JWTs;
  private key blocks; credentials embedded in URLs; and `NAME=value` secret assignments.
- Redaction is a safety net, not a guarantee. The control that matters is not passing secrets
  in the first place.

## Using it safely on GitHub

- Trigger on **`pull_request`**, not `pull_request_target`. The latter runs with a write token
  and secrets in the context of the base repository; never combine it with a checkout of
  pull request code.
- Grant `contents: read`, and `pull-requests: write` only if you want the comment.
- Pull requests from forks receive a read-only token. The comment cannot be posted (RepoProof
  says so and carries on); annotations and the job summary still work.
- Do not pass secrets through `pass_env` for a check that runs on pull requests.
- Pin the action to a release tag or a full commit SHA.
- Hosted runners are ephemeral VMs, which is the right kind of boundary for running
  repository code.

## Reporting a vulnerability

Please do not open a public issue. See [SECURITY.md](../SECURITY.md).
