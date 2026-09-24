/** Benchmark launcher plus the list of previous benchmark runs. */

import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useResource, useSubmit } from "../api/hooks";
import {
  EmptyState,
  ErrorBanner,
  formatTime,
  Loading,
  StatusBadge,
} from "../components/common";

export function BenchmarksPage(): JSX.Element {
  const datasets = useResource(() => api.datasets(), []);
  const models = useResource(() => api.models(), []);
  const benchmarks = useResource(() => api.listBenchmarks(), [], { pollMs: 4000 });
  const create = useSubmit(api.createBenchmark);

  const [dataset, setDataset] = useState("");
  const [selected, setSelected] = useState<string[]>(["mock:deterministic"]);
  const [tags, setTags] = useState<string[]>([]);

  const activeDataset =
    datasets.data?.find((item) => item.name === dataset) ?? datasets.data?.[0];

  function toggle(list: string[], value: string): string[] {
    return list.includes(value)
      ? list.filter((item) => item !== value)
      : [...list, value];
  }

  async function launch(event: React.FormEvent) {
    event.preventDefault();
    if (!activeDataset || selected.length === 0) return;
    await create.submit({ dataset: activeDataset.name, models: selected, tags });
    benchmarks.refresh();
  }

  return (
    <>
      <div className="page-header">
        <h1>Benchmarks</h1>
        <p>
          Run the same task set against several models and compare pass rate,
          latency, retries and estimated cost on identical inputs.
        </p>
      </div>

      {create.error ? <ErrorBanner error={create.error} /> : null}

      <div className="grid grid-2">
        <div className="card">
          <h2>New benchmark</h2>
          {datasets.initialLoading ? (
            <Loading label="Loading datasets" />
          ) : datasets.error ? (
            <ErrorBanner error={datasets.error} onRetry={datasets.refresh} />
          ) : !datasets.data || datasets.data.length === 0 ? (
            <EmptyState title="No datasets found">
              <p>Datasets live in fixtures/datasets as YAML files.</p>
            </EmptyState>
          ) : (
            <form onSubmit={launch}>
              <div className="field">
                <label htmlFor="dataset">Dataset</label>
                <select
                  id="dataset"
                  value={activeDataset?.name ?? ""}
                  onChange={(event) => setDataset(event.target.value)}
                >
                  {datasets.data.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name} ({item.task_count} tasks)
                    </option>
                  ))}
                </select>
                {activeDataset ? (
                  <p className="hint">{activeDataset.description}</p>
                ) : null}
              </div>

              <fieldset>
                <legend>Models</legend>
                {(models.data ?? []).map((model) => (
                  <label
                    key={model.id}
                    style={{ display: "block", fontWeight: 400 }}
                    title={model.reason}
                  >
                    <input
                      type="checkbox"
                      style={{ width: "auto", marginRight: 8 }}
                      checked={selected.includes(model.id)}
                      disabled={!model.available}
                      onChange={() => setSelected((list) => toggle(list, model.id))}
                    />
                    <span className="mono">{model.id}</span>{" "}
                    {model.is_test_double ? (
                      <span className="badge">test double</span>
                    ) : null}
                    {!model.available ? (
                      <span className="faint"> — {model.reason}</span>
                    ) : null}
                  </label>
                ))}
              </fieldset>

              {activeDataset && activeDataset.tags.length > 0 ? (
                <fieldset>
                  <legend>Filter by tag</legend>
                  {activeDataset.tags.map((tag) => (
                    <label key={tag} style={{ display: "inline-block", marginRight: 12, fontWeight: 400 }}>
                      <input
                        type="checkbox"
                        style={{ width: "auto", marginRight: 6 }}
                        checked={tags.includes(tag)}
                        onChange={() => setTags((list) => toggle(list, tag))}
                      />
                      {tag}
                    </label>
                  ))}
                </fieldset>
              ) : null}

              <div className="button-row">
                <button
                  type="submit"
                  className="primary"
                  disabled={create.submitting || selected.length === 0}
                >
                  {create.submitting ? "Queueing…" : "Run benchmark"}
                </button>
                <span className="faint">
                  {selected.length} model(s) × {activeDataset?.task_count ?? 0} task(s)
                </span>
              </div>
            </form>
          )}
        </div>

        <div className="card">
          <h2>Tasks in this dataset</h2>
          {activeDataset ? (
            <div className="table-scroll">
              <table>
                <caption className="visually-hidden">Dataset tasks</caption>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Difficulty</th>
                    <th>Tags</th>
                  </tr>
                </thead>
                <tbody>
                  {activeDataset.tasks.map((task) => (
                    <tr key={task.id}>
                      <td>
                        <div className="mono">{task.id}</div>
                        <div className="faint">{task.title}</div>
                      </td>
                      <td>
                        <span className="badge">{task.difficulty}</span>
                      </td>
                      <td className="faint">{task.tags.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">Select a dataset.</p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h2>Previous benchmark runs</h2>
          <button type="button" onClick={benchmarks.refresh} disabled={benchmarks.loading}>
            Refresh
          </button>
        </div>
        {benchmarks.initialLoading ? (
          <Loading label="Loading benchmarks" />
        ) : benchmarks.error ? (
          <ErrorBanner error={benchmarks.error} onRetry={benchmarks.refresh} />
        ) : !benchmarks.data || benchmarks.data.length === 0 ? (
          <EmptyState title="No benchmark runs yet">
            <p>Launch one above, or run `patchpilot bench` from the command line.</p>
          </EmptyState>
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="visually-hidden">Benchmark runs</caption>
              <thead>
                <tr>
                  <th>Benchmark</th>
                  <th>Status</th>
                  <th>Models</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {benchmarks.data.map((benchmark) => (
                  <tr key={benchmark.id}>
                    <td>
                      <Link to={`/benchmarks/${benchmark.id}`} className="mono">
                        {benchmark.id}
                      </Link>
                      <div className="faint">{benchmark.dataset.split(/[\\/]/).pop()}</div>
                    </td>
                    <td>
                      <StatusBadge status={benchmark.status} />
                    </td>
                    <td className="mono faint">{benchmark.models.join(", ")}</td>
                    <td className="nowrap faint">{formatTime(benchmark.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
