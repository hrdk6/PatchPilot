/** One run, in full: timeline, retrieval trace, plans, diffs, sandbox, cost. */

import { useParams } from "react-router-dom";

import { api } from "../api/client";
import { useResource, useSubmit } from "../api/hooks";
import { TERMINAL_STATUSES } from "../api/types";
import { BarChart } from "../components/BarChart";
import {
  EmptyState,
  ErrorBanner,
  formatCost,
  formatDuration,
  formatNumber,
  formatTime,
  Loading,
  Metric,
  StatusBadge,
} from "../components/common";
import {
  AttemptCard,
  DiffView,
  RetrievalTable,
  SandboxOutput,
  StateTimeline,
} from "../components/RunViews";

export function RunDetailPage(): JSX.Element {
  const { runId = "" } = useParams();
  const run = useResource(() => api.getRun(runId), [runId], { pollMs: 2500 });
  const cancel = useSubmit(() => api.cancelRun(runId));

  if (run.initialLoading) return <Loading label="Loading run" />;
  if (run.error) return <ErrorBanner error={run.error} onRetry={run.refresh} />;
  if (!run.data) return <EmptyState title="Run not found" />;

  const detail = run.data;
  const live = !TERMINAL_STATUSES.includes(detail.run.status);
  const perState = detail.latency.per_state_ms ?? {};
  const latencyData = Object.entries(perState)
    .sort((a, b) => b[1] - a[1])
    .map(([state, ms]) => ({
      label: state,
      value: ms,
      display: formatDuration(ms),
    }));

  return (
    <>
      <div className="page-header">
        <div className="row">
          <h1 className="mono">{detail.run.id}</h1>
          <StatusBadge status={detail.run.status} />
          {detail.run.stop_reason ? (
            <span className="badge">{detail.run.stop_reason}</span>
          ) : null}
          <div className="spacer" />
          {live ? (
            <button
              type="button"
              className="danger"
              onClick={() => void cancel.submit(undefined).then(run.refresh)}
              disabled={cancel.submitting}
            >
              {cancel.submitting ? "Cancelling…" : "Cancel run"}
            </button>
          ) : null}
          {detail.final_patch ? (
            <a className="badge badge-info" href={api.patchDownloadUrl(detail.run.id)}>
              Download final diff
            </a>
          ) : null}
        </div>
        <p>
          {detail.issue.title || "(untitled issue)"} ·{" "}
          <span className="mono">{detail.repository.url}</span> ·{" "}
          <span className="mono">{detail.run.repo_sha ?? "unpinned"}</span>
        </p>
      </div>

      {detail.run.error ? (
        <div className="banner banner-error" role="alert">
          <strong>{detail.run.error}</strong>
        </div>
      ) : null}
      {detail.run.sandbox_backend && !detail.run.sandbox_isolated ? (
        <div className="banner banner-warn">
          <strong>This run used the unisolated local sandbox</strong>
          <p>
            Commands ran as the PatchPilot user on the host. Treat the result as
            valid only because the fixture repositories are trusted.
          </p>
        </div>
      ) : null}
      {detail.run.baseline_reproduced === false ? (
        <div className="banner banner-warn">
          <strong>The baseline command passed before any patch was applied</strong>
          <p>
            The bug did not reproduce, so a passing run here does not demonstrate
            a repair. Check the validation command and the commit.
          </p>
        </div>
      ) : null}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Metric label="Model" value={<span className="mono">{detail.run.model}</span>} />
        <Metric
          label="Attempts"
          value={detail.run.attempts_used}
          sub={`budget ${String(detail.config.max_repair_attempts ?? "—")}`}
        />
        <Metric
          label="Tokens"
          value={formatNumber(detail.run.input_tokens + detail.run.output_tokens)}
          sub={`${formatNumber(detail.run.input_tokens)} in · ${formatNumber(
            detail.run.output_tokens,
          )} out`}
        />
        <Metric
          label="Estimated cost"
          value={formatCost(detail.run.cost_usd, detail.run.cost_known)}
          sub={detail.run.cost_known ? "static pricing snapshot" : "no pricing entry"}
        />
        <Metric
          label="Wall clock"
          value={formatDuration(detail.latency.total_ms ?? 0)}
          sub={`started ${formatTime(detail.run.started_at)}`}
        />
        <Metric
          label="Sandbox"
          value={detail.run.sandbox_backend ?? "—"}
          sub={detail.run.sandbox_isolated ? "isolated" : "NOT isolated"}
        />
        <Metric
          label="Baseline"
          value={
            detail.run.baseline_reproduced === null
              ? "—"
              : detail.run.baseline_reproduced
                ? "failed"
                : "passed"
          }
          sub="before any patch"
        />
        <Metric
          label="Retrieved"
          value={detail.retrieval.length}
          sub={`${new Set(detail.retrieval.map((chunk) => chunk.path)).size} file(s)`}
        />
      </div>

      <div className="grid grid-2">
        <div className="card">
          <div className="card-header">
            <h2>State machine</h2>
            {live ? <span className="badge badge-info">live</span> : null}
          </div>
          <StateTimeline events={detail.events} live={live} />
        </div>

        <div className="card">
          <h2>Latency by state</h2>
          {latencyData.length === 0 ? (
            <p className="muted">No timing recorded yet.</p>
          ) : (
            <BarChart title="Latency by state machine node" data={latencyData} />
          )}
          <h3 style={{ marginTop: 16 }}>Commands</h3>
          <dl className="kv">
            {Object.entries(detail.commands)
              .filter(([key]) => key !== "provenance")
              .map(([key, value]) => (
                <span key={key} style={{ display: "contents" }}>
                  <dt>{key}</dt>
                  <dd>{value ? String(value) : "—"}</dd>
                </span>
              ))}
          </dl>
          {detail.commands.provenance ? (
            <p className="faint" style={{ marginTop: 6 }}>
              {Object.entries(detail.commands.provenance as Record<string, string>)
                .map(([key, why]) => `${key}: ${why}`)
                .join(" · ")}
            </p>
          ) : null}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h2>Retrieval trace</h2>
          <span className="faint">
            why each symbol was put in front of the model
          </span>
        </div>
        <RetrievalTable
          chunks={detail.retrieval}
          truncated={detail.retrieval_truncated}
        />
      </div>

      {detail.baseline ? (
        <div className="card">
          <h2>Baseline (before patching)</h2>
          <SandboxOutput execution={detail.baseline} title="Baseline execution" />
        </div>
      ) : null}

      {detail.attempts.length === 0 ? (
        <div className="card">
          <EmptyState title="No attempts yet">
            <p>Attempts appear once the model has produced a plan.</p>
          </EmptyState>
        </div>
      ) : (
        detail.attempts.map((attempt) => (
          <AttemptCard key={attempt.attempt} attempt={attempt} />
        ))
      )}

      {detail.final_patch ? (
        <div className="card">
          <div className="card-header">
            <h2>Final patch</h2>
            <span className="badge badge-warn">review before merging</span>
          </div>
          <DiffView diff={detail.final_patch} />
        </div>
      ) : null}

      <div className="card">
        <h2>Artifacts</h2>
        {detail.artifacts.length === 0 ? (
          <p className="muted">No artifacts were preserved for this run.</p>
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="visually-hidden">
                Files preserved after the sandbox was destroyed
              </caption>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Kind</th>
                  <th className="num">Attempt</th>
                  <th className="num">Size</th>
                  <th>SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {detail.artifacts.map((artifact) => (
                  <tr key={artifact.id}>
                    <td>
                      <a href={api.artifactDownloadUrl(artifact.id)} className="mono">
                        {artifact.filename}
                      </a>
                    </td>
                    <td>
                      <span className="badge">{artifact.kind}</span>
                    </td>
                    <td className="num">{artifact.attempt}</td>
                    <td className="num">{formatNumber(artifact.size_bytes)} B</td>
                    <td className="mono faint">{artifact.sha256.slice(0, 16)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <details>
        <summary>Issue text</summary>
        <pre className="output">{detail.issue.body || "(empty)"}</pre>
      </details>
      <details>
        <summary>Run configuration</summary>
        <pre className="output">{JSON.stringify(detail.config, null, 2)}</pre>
      </details>
    </>
  );
}
