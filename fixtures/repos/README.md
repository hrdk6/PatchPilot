# Fixture repositories

Self-contained Python projects used to exercise PatchPilot end to end without
touching the network. Each one has a real, reproducible bug and a pytest suite
that fails on a clean checkout.

| Fixture | Bug shape | What it exercises |
| --- | --- | --- |
| `calc_service` | Unguarded division by zero | Single-file, single-hunk repair |
| `text_pipeline` | Failing test is one import hop from the defect | Import-graph-aware retrieval |
| `task_queue` | Off-by-one in 1-indexed pagination | Reasoning about an explicit contract |

The reference fixes and the scripted patches used by the mock model live
**outside** these directories, in `fixtures/solutions/`, so that nothing inside a
fixture repository tells a model where the bug is. Task definitions live in
`fixtures/datasets/`.

These are plain directories, not nested git repositories. PatchPilot ingests them
by content digest (`sha256:...`), which pins a run to exact file contents just as
a commit SHA would.
