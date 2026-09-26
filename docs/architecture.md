# Architecture

PatchPilot turns a bug report into a reviewed patch by making every step
inspectable. This document explains what the components are, how data flows
between them, and why the boundaries fall where they do.

## Component map

```
                       ┌──────────────────────────────────────────┐
   browser ───────────▶│ apps/web        React + TypeScript + Vite│
                       │  run form · run detail · benchmarks      │
                       └────────────────────┬─────────────────────┘
                                            │ REST /api/v1
                       ┌────────────────────▼─────────────────────┐
                       │ apps/api        FastAPI + SQLAlchemy     │
                       │  routers · schemas · store · migrations  │
                       │  ┌────────────────────────────────────┐  │
                       │  │ worker: database-backed job queue  │  │
                       │  └──────────────┬─────────────────────┘  │
                       └─────────────────┼────────────────────────┘
                                         │
             ┌───────────────────────────┼───────────────────────────┐
             ▼                           ▼                           ▼
   ┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
   │ packages/indexer │        │ packages/agent   │        │ packages/evals   │
   │  scanner         │        │  ingest          │        │  dataset         │
   │  python_parser   │◀───────│  prompts         │        │  runner          │
   │  graph           │        │  structured      │        │  metrics         │
   │  embeddings      │        │  patcher         │        │  similarity      │
   │  vectorstore     │        │  graph (LangGraph)│       │  report          │
   │  retrieval       │        │  adapters/*      │        └──────────────────┘
   └──────────────────┘        └────────┬─────────┘
                                        │
                               ┌────────▼─────────┐
                               │ packages/sandbox │
                               │  workspace       │
                               │  docker_sandbox  │
                               │  local_sandbox   │
                               └──────────────────┘

                       ┌──────────────────────────────────────────┐
                       │ packages/core  models · interfaces ·     │
                       │  config · errors · logging · diffutil    │
                       └──────────────────────────────────────────┘
                         (everything depends on core; core on nothing)
```

## Dependency rule

`packages/core` defines the domain models and the `Protocol` interfaces
(`LLMAdapter`, `EmbeddingProvider`, `VectorStore`, `CodeParser`, `Sandbox`). Every
other package depends on core and on nothing else inside PatchPilot. The agent
depends on the *shape* of a sandbox, not on `patchpilot_sandbox`; the API depends
on the agent but the agent knows nothing about HTTP or the database.

That is what makes each package independently testable, and it is what makes the
extension points real rather than aspirational: a tree-sitter TypeScript parser,
an E2B sandbox or a new model provider is one class implementing one protocol.

## The repair state machine

Implemented with LangGraph in `packages/agent/patchpilot_agent/graph.py`.

```
  INGEST ──▶ INDEX ──▶ RETRIEVE ──▶ PLAN ──▶ GENERATE_PATCH ──▶ VALIDATE_PATCH
                          ▲           │                                │
                          │           │ (plan invalid)                 ▼
                          │           │                          SANDBOX_TEST
                          │           │                                │
                          │           │                                ▼
                          │           └───────────────────────▶ ANALYZE_RESULT
                          │                                            │
                          └─────────── REPAIR_OR_FINISH ◀──────────────┘
                                               │
                                               ▼
                                           FINISHED
```

| State | What happens | Can terminate the run |
| --- | --- | --- |
| `INGEST` | Clone or copy the repository, pin it, discover commands, select the sandbox, run the **baseline** suite on the unpatched tree | sandbox unavailable, no test command, clone failure |
| `INDEX` | Scan, parse into symbol chunks, build the import/call graph, embed, upsert vectors | indexing failure |
| `RETRIEVE` | Build the context package with per-chunk scores and reasons | — |
| `PLAN` | Ask the model for a structured `RepairPlan`; validate it | model adapter failure |
| `GENERATE_PATCH` | Ask the model for a unified diff | model adapter failure |
| `VALIDATE_PATCH` | Apply the safety policy and dry-run the patch | — (rejection is data) |
| `SANDBOX_TEST` | Fresh workspace, apply patch, run targeted tests → lint → types → full suite | — |
| `ANALYZE_RESULT` | Classify the outcome by rule and decide whether a retry is justified | every terminal status |
| `REPAIR_OR_FINISH` | Enforce the retry budget; loop to `RETRIEVE` or stop | budget exhausted |

### Why `ANALYZE_RESULT` is deterministic

It classifies with rules, not with another model call. Whether to spend another
attempt is a budget decision. Making it non-deterministic would make runs
unreproducible and would let a model talk itself into an unbounded loop.

### Terminal states

| Status | Meaning |
| --- | --- |
| `fixed` | The validation command passed inside the sandbox |
| `tests-failed` | A valid patch applied; analysis declined a further attempt |
| `patch-invalid` | No proposal ever survived validation (or one was unsafe) |
| `sandbox-failed` | The sandbox was unavailable or could not run the commands |
| `budget-exhausted` | The retry cap was reached with the suite still failing |
| `cancelled` | A user cancelled it |
| `error` | Infrastructure failure outside the sandbox (clone, indexing, adapter) |

`error` is a seventh state beyond the six the specification names. It exists so a
clone failure is never mistaken for a model failure — conflating "we could not
fetch the code" with "the model could not fix it" would silently corrupt every
benchmark that included it.

### Stop conditions

The loop stops early, before the budget is spent, on:

- an **unsafe patch** (protected path, path traversal, binary, new file outside
  the repository) — a policy violation must not buy another attempt;
- a **repeated equivalent patch**, detected by a whitespace- and
  context-insensitive hash of the changed lines;
- an **unavailable sandbox** or an unrecoverable setup failure;
- **cancellation**, which is checked between every node.

## Data flow for one run

1. `POST /api/v1/runs` authenticates the caller, validates the request, resolves
   it against the server's run policy (defaults, caps, refused overrides — see
   `docs/security.md`), writes `repositories`, `issues` and `runs` rows,
   enqueues an `agent_run` job, and returns `202` immediately.
2. The worker claims the job with a conditional `UPDATE` and calls
   `services.execute_run`.
3. The graph runs. After **every** transition the observer writes a `run_events`
   row and updates the `runs` row in its own short transaction.
4. Each completed attempt writes an `attempts` row plus two artifacts: the diff
   and the sanitised sandbox log.
5. On completion the run is finalised and the index is persisted
   (`repository_indexes`, `code_chunks`, `symbols`, `import_edges`).
6. The dashboard polls `/runs/{id}/events?after=N` for new transitions and
   `/runs/{id}` for the full picture.

Because persistence happens per transition rather than at the end, a run that
crashes still has an accurate history up to the moment it stopped.

## Retrieval

Four signal families combine, each leaving a `ScoreComponent` behind:

| Signal | Weight | What it catches |
| --- | --- | --- |
| Semantic similarity | 1.00 | Code that *reads* like the issue |
| Lexical overlap (idf-weighted) | 0.85 | Exact identifiers and error strings |
| Import-graph neighbours | 0.35 (decayed) | The module that imports the suspect |
| Call-graph neighbours | 0.45 | Callers whose expectations the fix must preserve |
| Tests exercising a seed symbol | 0.55 | The test that will judge the patch |
| Paths named in the failure output | 0.90 | The exact traceback frame |
| Paths named in the issue | 0.70 | What the reporter pointed at |

The weights are module constants in `retrieval.py`, deliberately visible and
tunable rather than buried in a prompt. The `text_pipeline` fixture exists
specifically to prove the graph signal earns its place: the failing tests are in
`analytics.py` but the defect is one import hop away in `tokenizer.py`.

## Chunking

Chunks are symbol-aligned, not token-windowed. The unit of retrieval is a
function, method, class header or module preamble with its decorators, signature
and docstring intact. That is what makes a retrieved chunk something a model can
patch, and what lets the UI say "we included `calc.py::percentage`" instead of
"we included bytes 4000–6000".

## Persistence

SQLite locally, PostgreSQL for anything shared; no dialect-specific types. Queried
columns are real columns and the rest of each document is JSON, which keeps the
schema small enough to migrate confidently while persisting everything the UI
needs. Schema changes go through Alembic, never `create_all`.

## Background work

Indexing and agent runs take minutes; HTTP handlers must not. Jobs go into a
`jobs` table drained by a thread pool. A database-backed queue needs no broker,
survives a restart, is observable with one SQL query, and behaves identically in
a test, in Compose and in a single container. The claim is a conditional `UPDATE`
guarded on `status = 'queued'`, so several workers — or several API processes —
are safe.

**Leases.** Claiming a job stamps it with the worker's id and a heartbeat, which
a maintenance thread renews every `PATCHPILOT_WORKER_HEARTBEAT_SECONDS`. A
`running` job whose heartbeat is older than `PATCHPILOT_WORKER_LEASE_SECONDS`
belongs to a worker that died; any live worker requeues it — at startup and on a
periodic sweep — with a conditional `UPDATE` that re-checks the stale lease, so
two sweepers cannot both act on it. After three interrupted attempts the job is
abandoned and its run closed as `error`. A worker records a job's outcome only
while it still holds the lease, so one that stalled cannot overwrite the job's
new owner. (Recovery used to requeue *every* `running` job at startup, which with
a second process meant re-running jobs a live worker was executing.)

**Resumption.** A requeued run continues its append-only transition log from the
next index rather than colliding with its own history. `POST /runs/{id}/resume`
refuses a run that still has a queued or running job.

**Topology.** By default the worker runs inside the API process. With
`PATCHPILOT_WORKER_ENABLED=false` on the API, `patchpilot worker` runs it as its
own process (graceful on `SIGTERM`), and any number of those can share the
database. Workers register in a `workers` table and refresh it with each
heartbeat, so the API reports liveness and the sandbox the workers actually have,
not what its own container happens to see.

**Runs never stay "running".** If a job fails outside the state machine — while
persisting the outcome, say — or is abandoned, its run is closed as `error` (a
benchmark as `failed`).

**Retention.** Workers sweep workspaces left by crashed attempts at startup.
`patchpilot cleanup --older-than DAYS`, or `PATCHPILOT_RETENTION_DAYS` on a
schedule, deletes finished runs, benchmarks, jobs and idle checkouts.

Swapping in Celery or RQ means reimplementing `Worker` against the same `jobs`
rows. Nothing else changes.

## What is deliberately not here

- **No streaming model output.** Runs are minutes long and judged by a test
  command, not by tokens arriving; streaming would add failure modes for no gain.
- **No multi-agent decomposition.** One model, two prompts, a rule-based
  controller. The bottleneck on these tasks is retrieval quality and the safety
  envelope, not planner sophistication.
- **No automatic merging.** Human review before export or merge is a product
  boundary, not a missing feature.
