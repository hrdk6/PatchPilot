# Security model

PatchPilot downloads code it did not write, asks a language model to modify it,
and then executes the result. This document states plainly what is defended,
what is not, and what you would have to change before running it on anything
that matters.

## Threat model

**Assets**

- The host running PatchPilot, its filesystem and its credentials.
- API keys and tokens in the environment (`PATCHPILOT_*`, `OPENAI_*`,
  `ANTHROPIC_*`, `GITHUB_*`).
- The PatchPilot database and its artifacts.
- Other repositories and workspaces on the same machine.
- The network the host can reach, including internal services.

**Adversaries**

1. **A malicious repository.** Someone points PatchPilot at a repository whose
   test suite, `conftest.py` or build script is hostile. This is the primary
   threat: the code runs by design.
2. **A malicious or manipulated model.** A model — or a prompt injection carried
   inside the repository or the issue text — emits a patch designed to disable
   tests, exfiltrate secrets or escape the workspace.
3. **A resource-exhaustion attack.** A patch introduces an infinite loop, a fork
   bomb, a memory balloon or gigabytes of output.

**Explicitly out of scope for this release:** multi-tenancy, authentication and
authorisation on the API, network policy for the host, and kernel-level
isolation. The API has no auth and must not be exposed beyond localhost.

## Controls

### 1. The repository is copied, never mounted

Every attempt gets a fresh `shutil.copytree` of the pinned checkout into a
disposable workspace, and that copy is what goes into the sandbox via
`docker cp`. No host path is ever bind-mounted. A container therefore cannot see
or modify the host workspace, the pristine checkout, or any sibling run.
`.git` is excluded — it can carry credentials in some setups.

**No symlinks, anywhere.** A symlink in a repository can point at any file on
the host. The pristine checkout itself is symlink-free: links are skipped when a
local directory is copied and deleted from a fresh clone, and sandbox workspaces
skip them again. So neither the indexer, nor the patch dry-run, nor the sandbox
can read through a link into the host — a link to `~/.ssh/id_rsa` cannot become
a "source file" that is indexed, sent to the model and shown in the UI.

**Checkouts are immutable.** Each is staged privately, then published by an
atomic rename under a name derived from its commit SHA or content digest, and is
never modified afterwards. Concurrent runs of the same repository share one
checkout instead of deleting and re-cloning it under each other.

The workspace is deleted after every attempt, pass or fail. Attempt N+1 starts
from the pristine checkout, so one attempt can never contaminate the next.

### 2. Container hardening

Applied to every sandbox container (`packages/sandbox/docker_sandbox.py`):

| Control | Flag | Why |
| --- | --- | --- |
| No network | `--network none` | Generated code cannot exfiltrate or fetch payloads |
| Non-root | `--user 10001:10001` | Untrusted commands never run as root |
| No capabilities | `--cap-drop ALL` | Removes the standard privilege escalation surface |
| No privilege gain | `--security-opt no-new-privileges` | A setuid binary cannot escalate |
| Memory cap | `--memory`, `--memory-swap` equal | Bounds a memory balloon; swap disabled |
| CPU cap | `--cpus` | Bounds a spin loop |
| Process cap | `--pids-limit` | Bounds a fork bomb |
| Bounded `/tmp` | `--tmpfs /tmp:size=…` | Bounds scratch disk usage |
| Wall clock | per-command timeout + container `sleep` budget | Nothing runs forever |
| Disk quota | `--storage-opt size=` when supported | Bounds the writable layer |

Three capabilities are added back — `CHOWN`, `DAC_OVERRIDE`, `FOWNER` — solely so
the copied tree can be chowned to the sandbox user after `docker cp`, which
writes as root. They are usable only by root, and untrusted code never runs as
root: a non-root process has an empty effective capability set.

**Honest gap:** `--storage-opt size=` is only supported on some storage drivers.
When the daemon rejects it the sandbox logs a warning and continues *without* a
disk quota rather than failing the run. On such a host, disk exhaustion of the
container's writable layer is not bounded.

### 3. The sandbox environment carries nothing from the host

The container receives a fixed environment (`PATH`, `HOME`, `TMPDIR`, `LANG`,
`CI`, a few Python settings) and nothing else. The host environment is never
forwarded. Any extra variable a caller passes is dropped if its name contains
`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `CREDENTIAL`, `PRIVATE`, `SSH`, `AWS_`,
`GITHUB_`, `OPENAI_`, `ANTHROPIC_` or `DOCKER_`. The Docker socket is never
mounted.

### 4. Patch policy

A diff is inspected before anything executes (`packages/agent/patcher.py`). It is
rejected if it:

- does not parse as a unified diff, or is a binary patch;
- contains `..`, an absolute path or a drive letter;
- touches a protected path — `.github/**`, CI configs, `Dockerfile*`,
  `docker-compose*`, any `*.lock`, `.env*`, `*.pem`, `*.key`, `.git/**`,
  `.npmrc`, `.netrc`, `.pre-commit-config.yaml`;
- touches a dotfile (disabled by default);
- exceeds the file-count or line-count budget;
- fails to apply cleanly to the pristine checkout;
- changes nothing, or repeats a patch already tried in this run.

Patches are applied by a **pure-Python** unified-diff applier
(`packages/core/diffutil.py`), not by shelling out to `git apply` or `patch`.
Shelling out would mean running a binary over attacker-influenced input on the
host *before* the sandbox exists. Application is all-or-nothing: every file is
computed in memory first, so a failure halfway through cannot leave a
half-patched tree.

An unsafe patch ends the run immediately. It does not cost a retry, because a
policy violation is not a hypothesis worth another attempt.

The plan's `tests_to_run` is model output that ends up in a shell command: it
narrows the targeted test run. An entry is used only if it has the shape of a
pytest node id — no whitespace, no shell metacharacters, no leading `-` — so a
plan can neither inject a shell command nor smuggle in a pytest option such as
`--collect-only`. Anything else is dropped and the full suite runs instead. The
targeted run is a fast signal only; success is always decided by the full
validation command.

### 5. Output handling

Sandbox stdout and stderr are untrusted and can be enormous. Before anything is
stored, logged or put into a prompt it is stripped of ANSI control sequences and
NUL bytes, has host paths masked to `<workspace>`, is passed through
credential-shaped redaction (OpenAI, Anthropic, GitHub, AWS keys, bearer headers,
`*_SECRET=` assignments, PEM private-key blocks), and is truncated head-and-tail
to a byte budget with the dropped count recorded.

Redaction is defence in depth, not a guarantee. It is pattern matching; a secret
in an unusual format will pass through. Do not put real credentials on a machine
running untrusted repositories.

### 6. Ingestion

Clones are non-recursive, so submodule payloads are never fetched. Credential
prompting is disabled (`GIT_TERMINAL_PROMPT=0`). Only `http(s)`, `file://`, ssh
remotes and local paths are accepted. No repository code is executed during
ingest or indexing — indexing uses `ast.parse`, which does not execute the module.

### 7. Human review

PatchPilot never merges, pushes or opens a pull request. A patch is an artifact
you download and review. The UI labels the final diff "review before merging" and
the API exposes it as a file, not an action.

## The local sandbox: no isolation

`LocalSubprocessSandbox` runs commands as the current user on the host, with
access to the whole filesystem. **It isolates nothing.** It exists so CI and
contributors can exercise the entire pipeline against the fixture repositories in
this repo without a Docker daemon, and so the Docker backend has something to be
differentially tested against.

It provides a disposable copied workspace, a scrubbed environment, wall-clock
timeouts, output truncation and — on POSIX only — address-space, CPU-time,
process-count and file-size rlimits. On Windows no resource limits apply beyond
the timeout.

A timeout kills the whole process tree, not just the direct child: the process
group on POSIX, `taskkill /T` on Windows. The direct child is only the shell (and
on Windows the venv `python.exe` is itself a launcher), so killing it alone
would leave an infinite loop introduced by a patch running on the host.

Guards:

- refused outright when `PATCHPILOT_ENVIRONMENT=production`;
- disabled by `PATCHPILOT_ALLOW_LOCAL_SANDBOX=false`;
- every run records `sandbox_backend` and `sandbox_isolated`, and the UI shows a
  persistent warning banner on both the run form and any run that used it;
- benchmark reports state the backend in the environment block and warn that
  results from the `local` backend are only meaningful for trusted fixtures.

Never point it at a repository you did not write.

## Known limits

1. **Docker is kernel-shared isolation.** A container escape via a kernel or
   runtime vulnerability defeats every control above. Docker is a strong boundary
   against careless code and a moderate one against a determined attacker.
2. **The daemon is trusted.** PatchPilot talks to a local Docker daemon. Anyone
   who can reach that daemon already has root-equivalent access to the host. In
   `docker compose` the API container mounts the host socket for this reason and
   runs as root; dropping to its unprivileged user would not reduce what the
   socket grants.
3. **Disk is not always bounded** (see `--storage-opt` above).
4. **No API authentication.** Anyone who can reach the API can run arbitrary
   commands inside the sandbox. Bind to localhost.
5. **Prompt injection is not solved.** A repository can contain text aimed at the
   model. The patch policy is what contains the blast radius: a model that is
   talked into disabling CI still cannot, because the diff is rejected.
6. **Redaction is best-effort** pattern matching.
7. **The retry budget bounds cost, not correctness.** A patch that passes the
   tests is not necessarily correct. That is why review is mandatory.

## Production hardening roadmap

In rough order of value:

1. **Replace Docker with a stronger boundary** — gVisor, Kata Containers,
   Firecracker, or a hosted sandbox such as E2B or Modal. The `Sandbox` protocol
   is two methods; this is an additive change.
2. **Move execution off the API host entirely.** Remote sandbox workers with no
   access to the PatchPilot database or its secrets.
3. **Authentication and authorisation** on the API, with per-user run quotas.
4. **Egress policy** for the cases that genuinely need network (dependency
   installation): an allow-listed proxy to a package mirror, never open egress.
5. **A policy engine** for patch rules, so per-repository policy is configuration
   rather than a constant in `patcher.py`.
6. **Secret scanning on artifacts** before they are persisted, in addition to
   redaction at capture time.
7. **Signed, immutable audit trail** for the event log.
8. **Resource accounting per tenant**, so one run cannot starve others.

## Reporting

This is a portfolio project, not a maintained product. If you find a
vulnerability, open an issue describing it. Do not run it on untrusted
repositories on a machine you care about.
