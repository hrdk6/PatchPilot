/** Model comparison for one benchmark run, with charts, filters and exports. */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import { useResource } from "../api/hooks";
import { BarChart } from "../components/BarChart";
import {
  EmptyState,
  ErrorBanner,
  formatDuration,
  formatNumber,
  Loading,
  percent,
  StatusBadge,
} from "../components/common";
import { DownloadIcon } from "../components/icons";

export function BenchmarkDetailPage(): JSX.Element {
  const { benchmarkId = "" } = useParams();
  const [tag, setTag] = useState("");
  const [result, setResult] = useState("");
  const benchmark = useResource(
    () => api.getBenchmark(benchmarkId, { tag: tag || undefined, result: result || undefined }),
    [benchmarkId, tag, result],
    { pollMs: 4000 },
  );

  if (benchmark.initialLoading) return <Loading label="Loading benchmark" />;
  if (benchmark.error) return <ErrorBanner error={benchmark.error} onRetry={benchmark.refresh} />;
  if (!benchmark.data) return <EmptyState title="Benchmark not found" />;

  const data = benchmark.data;
  const summaries = data.report?.summaries ?? [];
  const allTags = Array.from(new Set(data.results.flatMap((item) => item.tags))).sort();

  return (
    <>
      <header className="change-head">
        <div className="change-title">
          <p className="change-id mono">
            <Link to="/benchmarks">Benchmarks</Link> / {data.id}
          </p>
          <h1>{data.dataset.split(/[\\/]/).pop()?.replace(/\.ya?ml$/, "")}</h1>
          <p className="change-meta">
            <span>
              {data.models.length} model(s) on identical tasks
            </span>
            <span className="mono">{data.models.join(" · ")}</span>
          </p>
        </div>
        <div className="change-actions">
          <StatusBadge status={data.status} />
          {data.report ? (
            <div className="button-row">
              <a className="button" href={api.benchmarkJsonUrl(data.id)}>
                <DownloadIcon /> Export JSON
              </a>
              <a className="button" href={api.benchmarkMarkdownUrl(data.id)}>
                <DownloadIcon /> Export Markdown
              </a>
            </div>
          ) : null}
        </div>
      </header>

      {data.error ? (
        <div className="banner banner-error" role="alert">
          <strong>{data.error}</strong>
        </div>
      ) : null}
      {data.status === "queued" || data.status === "running" ? (
        <div className="banner banner-info" role="status">
          <strong>Benchmark in progress</strong>
          <p>Results appear as each task finishes. This page refreshes automatically.</p>
        </div>
      ) : null}
      {data.report && Object.keys(data.report.skipped_models).length > 0 ? (
        <div className="banner banner-warn">
          <strong>Some models were skipped</strong>
          {Object.entries(data.report.skipped_models).map(([model, reason]) => (
            <p key={model}>
              <span className="mono">{model}</span>: {reason}
            </p>
          ))}
        </div>
      ) : null}
      {summaries.some((summary) => summary.small_sample) ? (
        <div className="banner banner-warn">
          <strong>Small sample</strong>
          <p>
            These rates come from very few tasks. Read the confidence interval
            rather than the point estimate.
          </p>
        </div>
      ) : null}

      {summaries.length > 0 ? (
        <>
          <div className="card">
            <h2>Model comparison</h2>
            <div className="table-scroll">
              <table>
                <caption className="visually-hidden">
                  Per-model benchmark metrics on identical tasks
                </caption>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="num">Pass rate</th>
                    <th className="num">95% CI</th>
                    <th className="num">Adjusted</th>
                    <th className="num">Patch applied</th>
                    <th className="num">Median</th>
                    <th className="num">p95</th>
                    <th className="num">p99</th>
                    <th className="num">Retries/task</th>
                    <th className="num">Tokens</th>
                    <th className="num">Cost/task</th>
                    <th className="num">Sandbox fail</th>
                  </tr>
                </thead>
                <tbody>
                  {summaries.map((summary) => (
                    <tr key={summary.model}>
                      <td>
                        <span className="mono">{summary.model}</span>
                        {summary.is_test_double ? (
                          <div>
                            <span className="badge">test double</span>
                          </div>
                        ) : null}
                      </td>
                      <td className="num">
                        {percent(summary.pass_rate)}{" "}
                        <span className="faint">
                          ({summary.passed}/{summary.tasks})
                        </span>
                      </td>
                      <td className="num faint">
                        {summary.pass_rate_ci95
                          ? `${percent(summary.pass_rate_ci95[0])}–${percent(
                              summary.pass_rate_ci95[1],
                            )}`
                          : "—"}
                      </td>
                      <td className="num">
                        {percent(summary.adjusted_pass_rate)}{" "}
                        <span className="faint">({summary.adjusted_denominator})</span>
                      </td>
                      <td className="num">{percent(summary.patch_application_rate)}</td>
                      <td className="num">{formatDuration(summary.median_latency_ms)}</td>
                      <td className="num">{formatDuration(summary.p95_latency_ms)}</td>
                      <td className="num">{formatDuration(summary.p99_latency_ms)}</td>
                      <td className="num">{summary.retries_per_task.toFixed(2)}</td>
                      <td className="num">{formatNumber(summary.total_tokens)}</td>
                      <td className="num">
                        {summary.cost_pricing_known
                          ? `$${summary.cost_per_task_usd.toFixed(4)}`
                          : "n/a"}
                      </td>
                      <td className="num">{percent(summary.sandbox_failure_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid grid-charts">
            <div className="card">
              <h2>Pass rate</h2>
              <BarChart
                title="Pass rate by model"
                max={1}
                data={summaries.map((summary) => ({
                  label: summary.model,
                  value: summary.pass_rate,
                  display: `${percent(summary.pass_rate)} (${summary.passed}/${summary.tasks})`,
                  tone: summary.pass_rate >= 0.75 ? "ok" : summary.pass_rate > 0 ? "warn" : "danger",
                }))}
              />
            </div>
            <div className="card">
              <h2>Latency</h2>
              <BarChart
                title="Median and p99 latency by model"
                legend={[
                  { label: "median", tone: "accent" },
                  { label: "p99", tone: "muted" },
                ]}
                data={summaries.flatMap((summary) => [
                  {
                    label: summary.model,
                    series: "median",
                    value: summary.median_latency_ms,
                    display: formatDuration(summary.median_latency_ms),
                  },
                  {
                    label: summary.model,
                    series: "p99",
                    continued: true,
                    value: summary.p99_latency_ms,
                    display: formatDuration(summary.p99_latency_ms),
                    // A second series, not a warning: status colours stay reserved.
                    tone: "muted" as const,
                  },
                ])}
              />
            </div>
            <div className="card">
              <h2>Estimated cost per task</h2>
              <BarChart
                title="Estimated cost per task by model"
                data={summaries.map((summary) => ({
                  label: summary.model,
                  value: summary.cost_per_task_usd,
                  display: summary.cost_pricing_known
                    ? `$${summary.cost_per_task_usd.toFixed(4)}`
                    : "n/a",
                }))}
              />
              <p className="faint">
                Estimated from a static pricing snapshot and reported token counts.
              </p>
            </div>
            <div className="card">
              <h2>Retries per task</h2>
              <BarChart
                title="Average retries per task by model"
                data={summaries.map((summary) => ({
                  label: summary.model,
                  value: summary.retries_per_task,
                  display: summary.retries_per_task.toFixed(2),
                  tone: summary.retries_per_task > 1 ? "warn" : "accent",
                }))}
              />
            </div>
          </div>
        </>
      ) : null}

      <div className="card">
        <div className="card-header">
          <h2>Per-task results</h2>
          <div className="row">
            <label htmlFor="tag-filter" className="muted">
              Tag
            </label>
            <select
              id="tag-filter"
              value={tag}
              onChange={(event) => setTag(event.target.value)}
              style={{ width: "auto" }}
            >
              <option value="">all</option>
              {allTags.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <label htmlFor="result-filter" className="muted">
              Result
            </label>
            <select
              id="result-filter"
              value={result}
              onChange={(event) => setResult(event.target.value)}
              style={{ width: "auto" }}
            >
              <option value="">all</option>
              <option value="pass">pass</option>
              <option value="fail">fail</option>
            </select>
          </div>
        </div>

        {data.results.length === 0 ? (
          <EmptyState title="No results match this filter" />
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="visually-hidden">Individual task results</caption>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Model</th>
                  <th>Status</th>
                  <th className="num">Attempts</th>
                  <th className="num">Latency</th>
                  <th className="num">Tokens</th>
                  <th className="num">Similarity</th>
                  <th>Run</th>
                </tr>
              </thead>
              <tbody>
                {data.results.map((item) => (
                  <tr key={`${item.model}-${item.task_id}`}>
                    <td className="mono">{item.task_id}</td>
                    <td className="mono">{item.model}</td>
                    <td>
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="num">{item.attempts_used}</td>
                    <td className="num">{formatDuration(item.latency_ms)}</td>
                    <td className="num">
                      {formatNumber(item.input_tokens + item.output_tokens)}
                    </td>
                    <td className="num faint" title="Secondary signal, not correctness">
                      {item.similarity === null ? "—" : item.similarity.toFixed(2)}
                    </td>
                    <td>
                      {item.run_id ? (
                        <Link to={`/runs/${item.run_id}`} className="mono">
                          {item.run_id.slice(0, 12)}…
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="faint" style={{ marginTop: 10 }}>
          Similarity measures textual overlap with a reference patch. Only the
          sandboxed validation command decides pass or fail.
        </p>
      </div>
    </>
  );
}
