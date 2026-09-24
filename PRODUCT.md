# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The dashboard's primary audience is **reviewers of the author's portfolio**:
recruiters, hiring managers and interviewing engineers who meet PatchPilot
through README screenshots, a short live demo, or a local `patchpilot demo` run.
They skim, often for under a minute, and decide whether the work is serious.

The secondary audience is the engineer actually operating the tool: someone
inspecting a run's state machine, retrieval trace, diffs and sandbox output, or
comparing models in a benchmark. Every screen must remain fully usable for that
person; the showcase is only credible if the tool is real.

## Product Purpose

PatchPilot is an autonomous repository-level bug-fix agent and the evaluation
platform that measures whether it works. Given a repository and an issue it
indexes the code structurally, retrieves the relevant symbols with their import
and call-graph neighbours, asks a model for a plan and then a patch, runs the
patch in a sandbox, and feeds failures back into a bounded repair loop. It
benchmarks models against each other on identical tasks.

Success for the dashboard: a reviewer understands within seconds what the
system did on a run and why, and comes away convinced of engineering depth and
honesty.

## Positioning

Radical inspectability and honesty. Every state transition has a reason and a
duration; every retrieved chunk carries the scored signals that selected it;
benchmark numbers ship with 95% confidence intervals, small-sample warnings,
and "estimated" labels. The UI never presents a mock model, an unisolated
sandbox, or an estimated cost as anything other than what it is.

## Operating Context

- Runs locally (`patchpilot serve` + Vite dev server, or `docker compose`), no
  account or API key; the default model is a deterministic test double.
- Three bundled fixture repositories provide real, reproducible data; demo
  screenshots come from them.
- Screenshots of the dashboard are embedded in the GitHub README
  (`docs/images/`).

## Capabilities and Constraints

- Pages: New run, Runs list, Run detail (state-machine timeline, per-state
  latency, retrieval trace, baseline output, per-attempt plan/diff/sandbox
  output, artifacts), Benchmarks list, Benchmark detail (model comparison with
  CIs, charts, per-task results, JSON/Markdown export).
- React 18 + TypeScript + Vite, plain CSS, no UI library; API is FastAPI under
  `/api/v1`, polled by the client.
- Must keep: every data field, the honesty banners (unisolated sandbox, test
  doubles, small sample, estimated cost), accessible data tables behind charts,
  light and dark themes, phone-width layout.
- Terminology: run, attempt, transition, state (INGEST … FINISHED), retrieval
  trace, chunk, signal/reason, sandbox (docker/local, isolated), benchmark,
  test double.

## Brand Commitments

Name "PatchPilot". No logo asset exists beyond a lettermark. No other binding
visual commitments.

## Evidence on Hand

Real data from the fixture runs and benchmarks. There are no users,
testimonials, adoption numbers or real-model benchmark results; the UI and any
copy must not imply them.

## Product Principles

1. Show the evidence, not a verdict: every number links to how it was produced.
2. Honesty is a feature: caveats are designed in, never hidden.
3. Legible under a skim, rewarding on inspection.
4. The tool must be real: nothing decorative may cost usability.

## Accessibility & Inclusion

Keyboard operable; charts always backed by a data table; status never conveyed
by color alone; respects prefers-color-scheme and reduced motion.
