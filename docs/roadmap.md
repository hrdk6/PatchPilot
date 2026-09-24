# Roadmap

What exists today is a working vertical slice for small-to-medium Python
repositories with an existing test suite. This is what would come next, in the
order the value justifies the work, with the extension point each item plugs
into.

## 1. GitHub pull-request validation

**Why first:** it is the workflow the whole system implies, and everything needed
is already persisted.

- Accept a PR URL as the input instead of an issue: the diff becomes the
  hypothesis, and validation runs against the PR head.
- Post the run summary — plan, diff, sandbox output, cost — as a PR comment via
  a GitHub App, behind a per-repository opt-in.
- "Suggest a fix" on a failing check: read the CI logs as `failure_excerpt`,
  which the retriever already knows how to use, and open a draft PR for a human
  to review.

**Plugs into:** `ingest.py` (a new source alongside clone + issue) and a new
`packages/integrations/github`. The patch policy and sandbox are unchanged.

**Constraint kept:** still no auto-merge. A bot that merges its own patches is a
different product with a different risk profile.

## 2. Remote sandboxes (E2B, Modal, Firecracker)

**Why:** the honest answer to every limit in `docs/security.md`. Docker is
kernel-shared isolation on the API host; a remote microVM is neither.

The `Sandbox` protocol is two methods — `available()` and `run()` — so this is
additive. The work is in the plumbing, not the interface: streaming a workspace
to a remote sandbox, handling partial failure, and accounting for the added
latency in the benchmark (which already breaks latency down per state, so the
cost of the move will be visible immediately).

**Plugs into:** `packages/sandbox/` as `e2b_sandbox.py`, selected by
`PATCHPILOT_SANDBOX_BACKEND=e2b`.

## 3. TypeScript and JavaScript support

**Why:** the second-most-common language for the repositories this targets, and
the indexer was designed for it from the start.

- A tree-sitter backed parser implementing the existing `CodeParser` protocol:
  `parse()` → symbols + imports, `chunk()` → symbol-aligned chunks. Register it
  and the scanner, graph, retriever, prompts and UI work unchanged.
- Module resolution is the real work: `tsconfig` path aliases, barrel files,
  `index.ts` resolution and `package.json` workspaces are all harder than
  Python's import rules.
- Command discovery for `package.json` scripts, vitest/jest/playwright.

**Plugs into:** `packages/indexer/registry.py` — `register_parser(TypeScriptParser())`
is the entire integration.

## 4. A policy engine for patch rules

**Why:** `PROTECTED_GLOBS` is a constant in `patcher.py`. Different repositories
need different rules, and a security team should be able to change them without a
deploy.

- Declarative per-repository policy: protected paths, size budgets, whether
  dotfiles or dependency manifests may be touched, required approvals.
- Policy decisions recorded on the run alongside the rejection reasons, so an
  audit can answer "why was this allowed?" as well as "why was this blocked?".
- Dry-run mode: evaluate a policy change against historical runs before adopting
  it.

**Plugs into:** `PatchPolicy`, which is already a dataclass the validator takes
as a parameter.

## 5. Enterprise CI integration

- Run as a CI job rather than a service: `patchpilot run --ci` emitting JUnit XML
  and a job summary.
- Webhook triggers from Jenkins, GitLab CI, Buildkite.
- Artifact upload to the CI system's own store instead of the local filesystem.
- OIDC-based credentials so no long-lived API keys sit on the runner.

## 6. Retrieval improvements

Measured against the benchmark rather than assumed:

- **Reranking** a larger candidate set with a cross-encoder or a cheap model.
- **Repository-level memory** — persist which files were touched by past
  successful fixes for similar issues and use that as a prior.
- **Call-graph depth beyond one hop**, gated on measured benefit; the current
  decay is a guess that the benchmark should confirm or kill.
- **Chunk-level diff history**: a function that changed three times last month is
  a better suspect than one untouched for two years.

## 7. Evaluation depth

The most valuable unmeasured number in this project is how often "tests pass" and
"actually correct" diverge.

- **Human review of passing patches**, recorded as a label, to quantify that gap.
- **Held-out repositories** outside model training sets.
- **Repeats per task** to separate model variance from task difficulty.
- **Adversarial tasks**: bugs where the obvious fix breaks a caller, to test
  whether graph-aware retrieval actually prevents regressions.
- **SWE-bench-style dataset import**, with the caveat that its tasks are widely
  memorised.

## 8. Operational maturity

- Authentication, authorisation and per-user quotas on the API.
- OpenTelemetry traces spanning the state machine, with the existing correlation
  ids as the trace key.
- Celery or RQ behind the existing `Worker` interface for multi-host scale.
- Retention and cleanup for workspaces, artifacts and vectors.
- A read-only "share this run" link for review without dashboard access.

## Deliberately not planned

- **Auto-merge.** See above.
- **Multi-agent decomposition.** On these tasks the bottleneck is retrieval
  quality and the safety envelope, not planner sophistication. It would be added
  only if the benchmark showed a ceiling that a single planner could not reach.
- **A bespoke fine-tuned model.** The adapter interface exists precisely so the
  model stays a swappable variable.
