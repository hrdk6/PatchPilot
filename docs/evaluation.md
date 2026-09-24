# Evaluation methodology

A pass rate is easy to produce and easy to inflate. This document defines every
metric PatchPilot reports, states what each one does and does not prove, and
explains how to reproduce a benchmark exactly.

## What is being measured

One **task** is: a pinned repository, an issue description, a baseline command
and a validation command. A task **passes** when, after PatchPilot's bounded
repair loop, the validation command exits zero inside the sandbox on a patched
copy of that repository.

That is a narrow, honest definition. It does not claim the patch is *correct*,
only that it makes the declared command pass without violating the patch policy.

## Metrics

All metrics are defined once, in `packages/evals/metrics.py`, so the JSON, the
Markdown report and the dashboard cannot disagree.

### Outcome metrics

| Metric | Definition | What it proves |
| --- | --- | --- |
| **Pass rate** | `fixed` runs ÷ tasks attempted | Headline capability on this task set |
| **95% CI** | Wilson score interval on the pass rate | How little a small sample tells you |
| **Baseline failure confirmation rate** | Tasks whose baseline command failed *before* patching ÷ tasks | Whether the tasks are actually broken |
| **Adjusted pass rate** | `fixed` ÷ tasks that failed at baseline | Pass rate with non-reproducing tasks removed |
| **Patch application rate** | Tasks where ≥1 patch survived validation and applied ÷ tasks | Whether the model can produce a *usable* diff at all |
| **Lint / type / test pass rate** | Per-command outcome on the final attempt | Where in the pipeline the failure sits |
| **Sandbox failure rate** | Tasks ending `sandbox-failed` ÷ tasks | Harness health, **not** model quality |

The **adjusted pass rate** matters. A task that passes before any patch is
applied measures nothing about repair. If a model "passes" such a task, that is
a bug in the dataset, not a capability. The report shows both numbers and the
denominator.

### Efficiency metrics

| Metric | Definition |
| --- | --- |
| **Median / p95 / p99 latency** | End-to-end wall clock per task, linear-interpolation percentiles |
| **Per-state latency** | Wall clock per state-machine node, summed across tasks |
| **Input / output / total tokens** | Provider-reported where available, estimated otherwise |
| **Estimated cost** | Tokens × a static pricing snapshot |
| **Cost per task / per fix** | Total estimated cost ÷ tasks, ÷ successful fixes |
| **Retries per task** | Attempts beyond the first, averaged |

Per-state latency is the number that changes decisions. If `SANDBOX_TEST`
dominates, a faster model buys nothing; if `PLAN` and `GENERATE_PATCH` dominate,
it buys a lot.

### Secondary signals

| Metric | Definition | Standing |
| --- | --- | --- |
| **Patch similarity** | Weighted overlap of normalised changed lines with a reference patch | Diagnostic only |
| **Expected files touched** | How many declared files the patch actually modified | Diagnostic only |

**Similarity is never treated as correctness.** A correct fix can score low (a
different valid approach) and an incorrect one can score high (right lines,
wrong logic). Only the sandboxed validation command decides pass or fail. The
metric is useful for one question: *did the model edit the right place and still
fail?*

## Honesty rules baked into the report

The Markdown report is written to be pasted into a PR without a human first
having to add caveats. It always states:

1. **Sample size and confidence.** Below ten tasks per model the report says so
   explicitly and points at the Wilson interval: three-for-three is consistent
   with a true pass rate near 40%.
2. **Which models are test doubles.** `mock:*` adapters replay a scripted patch.
   Their pass rate measures the harness, not reasoning. They are labelled in
   every table and excluded from any comparison claim.
3. **Which models were skipped and why.** An unconfigured model is recorded with
   its reason rather than silently dropped — a blank column is a finding.
4. **Whether cost is estimated.** It always is: a static pricing table times
   reported token counts. Models with no pricing entry show `n/a` rather than
   zero.
5. **Whether the sandbox was isolated.** Results produced with the `local`
   backend ran without isolation and are meaningful only for trusted fixtures.

## Token counting

Providers that report usage are trusted verbatim. Where a provider omits usage —
common with local OpenAI-compatible servers — usage is estimated at roughly four
characters per token and the response is flagged `usage_estimated: true`. The
flag propagates, so a cost derived from estimated tokens is never presented as
measured.

## Reproducibility

A benchmark run pins:

- **The code.** A git remote is pinned to a resolved commit SHA. A local
  directory is pinned to a `sha256:` content digest, which fixes the exact bytes
  just as firmly. A local git checkout with uncommitted changes is pinned to
  `<HEAD>+dirty.<digest>`, so two different working trees never share a pin —
  or a cached index.
- **The task.** Issue text, setup, baseline and validation commands live in the
  dataset file, not in code.
- **The environment.** Embedding provider and dimension, vector store, sandbox
  backend and image, network mode, retry budget and retrieval `top_k` are
  recorded in the report's environment block.
- **The retrieval.** The local hash embedder is deterministic, so identical
  inputs produce identical vectors across runs and machines.

What is *not* pinned is the model itself. Hosted models change under a fixed
name, and temperature-zero sampling is not a guarantee of determinism. Two runs
of the same benchmark against the same hosted model on different days are not
strictly comparable, and the report's timestamp is what tells you that.

To reproduce a run: take the `dataset_path` and `environment` block from the JSON
report, restore those settings, and re-run the same models.

## Running a benchmark

```bash
# Offline, no API key: the harness against itself.
patchpilot bench patchpilot-fixtures -m mock:deterministic -m mock:stubborn

# Compare a hosted model against a local one on identical tasks.
export PATCHPILOT_OPENAI_API_KEY=sk-...
patchpilot bench patchpilot-fixtures \
  -m openai:gpt-4o-mini \
  -m ollama:qwen2.5-coder \
  --output benchmark-output
```

Both a `.json` and a `.md` report are written. The same run is available over
HTTP at `/api/v1/benchmarks/{id}/report.json` and `report.md`, and in the
dashboard with per-tag and pass/fail filters.

## The bundled dataset

`fixtures/datasets/patchpilot-fixtures.yaml` has three tasks against the fixture
repositories in this project:

| Task | Difficulty | What it probes |
| --- | --- | --- |
| `calc-service-zero-division` | easy | Single-file guard clause; can the loop close at all |
| `text-pipeline-punctuation` | medium | The failing test is one import hop from the defect — retrieval must follow the graph |
| `task-queue-pagination` | medium | Reasoning about a documented 1-indexed contract |

Three tasks is **not** a benchmark of model capability, and the report says so.
It is a regression test for the harness and a demonstration that the pipeline
works end to end without network access. For real comparison you want tens of
tasks across repositories the models have not memorised.

## Adding tasks

```yaml
tasks:
  - id: my-task
    title: Short description of the symptom
    repository_url: https://github.com/owner/repo
    commit_sha: 3f2a1c...          # pin it
    difficulty: medium
    tags: [python, async]
    issue: |
      Describe the symptom and the expected behaviour.
      Do not name the file to change or the fix.
    baseline_command: python -m pytest -q
    validation_command: python -m pytest -q tests/test_thing.py
    expected_files: [pkg/thing.py]
    reference_patch: ../reference_patches/my-task.diff   # optional
```

Rules the loader enforces: `id`, `repository_url`, `issue` and
`validation_command` are required, ids are unique, difficulty is one of
`easy`/`medium`/`hard`, and unknown fields are an error rather than silently
ignored. `tests/test_fixtures.py` additionally asserts that an issue body never
names its own `expected_files` — an issue that names the fix tests reading
comprehension, not repair.

## What would make this a real benchmark

1. **More tasks** — tens, not three; the CI width is the argument.
2. **Repositories outside the training set**, or at least held-out commits.
3. **Repeats per task** to separate model variance from task difficulty.
4. **Human review of passing patches**, to measure how often "tests pass" and
   "actually correct" diverge. That gap is the most interesting unmeasured number
   in this project.
