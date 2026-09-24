/** The run list as a review queue: every run is a change with a subject and a verdict. */

import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useResource } from "../api/hooks";
import { TERMINAL_STATUSES } from "../api/types";
import {
  EmptyState,
  ErrorBanner,
  formatCost,
  formatNumber,
  formatTime,
  Loading,
  ScoreSquare,
  StatusBadge,
  Tag,
} from "../components/common";
import { AlertIcon } from "../components/icons";

export function RunsPage(): JSX.Element {
  const [status, setStatus] = useState("");
  const runs = useResource(
    () => api.listRuns({ limit: 50, status: status || undefined }),
    [status],
    { pollMs: 4000 },
  );

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Runs</h1>
          <p>Every repair the agent attempted, newest first, with its verdict and cost.</p>
        </div>
        <div className="page-head-tools">
          <label htmlFor="status-filter">Status</label>
          <select
            id="status-filter"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">all</option>
            <option value="queued">queued</option>
            <option value="running">running</option>
            {TERMINAL_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <span className="faint num">{runs.data ? `${runs.data.total} run(s)` : ""}</span>
          <button type="button" onClick={runs.refresh} disabled={runs.loading}>
            Refresh
          </button>
        </div>
      </header>

      <section className="panel">
        {runs.initialLoading ? (
          <div className="panel-body">
            <Loading label="Loading runs" />
          </div>
        ) : runs.error ? (
          <div className="panel-body">
            <ErrorBanner error={runs.error} onRetry={runs.refresh} />
          </div>
        ) : !runs.data || runs.data.items.length === 0 ? (
          <div className="panel-body">
            <EmptyState title="No runs yet">
              <p>
                <Link to="/new">Start a run</Link> against one of the bundled
                fixture repositories. It needs no API key.
              </p>
            </EmptyState>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="queue">
              <caption className="visually-hidden">Agent runs, newest first</caption>
              <thead>
                <tr>
                  <th className="queue-score-head">
                    <span className="visually-hidden">Verified score</span>
                  </th>
                  <th>Subject</th>
                  <th>Verdict</th>
                  <th>Model</th>
                  <th className="num">Patchsets</th>
                  <th>Sandbox</th>
                  <th className="num">Tokens</th>
                  <th className="num">Est. cost</th>
                  <th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.id}>
                    <td className="queue-score-cell">
                      <ScoreSquare status={run.status} />
                    </td>
                    <td className="queue-subject">
                      <Link to={`/runs/${run.id}`}>
                        {run.issue_title || "(untitled issue)"}
                      </Link>
                      <div className="queue-meta mono">
                        {run.id}
                        {run.repository_slug ? <> · {run.repository_slug}</> : null}
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                      {run.stop_reason && run.stop_reason !== run.status ? (
                        <div className="queue-meta mono">{run.stop_reason}</div>
                      ) : null}
                    </td>
                    <td className="mono">{run.model}</td>
                    <td className="num">{run.attempts_used}</td>
                    <td className="nowrap">
                      {run.sandbox_backend && !run.sandbox_isolated ? (
                        <span className="label label-warn" title="No isolation">
                          <AlertIcon />
                          {run.sandbox_backend} · <span>not isolated</span>
                        </span>
                      ) : (
                        <Tag>{run.sandbox_backend ?? "—"}</Tag>
                      )}
                    </td>
                    <td className="num">{formatNumber(run.input_tokens + run.output_tokens)}</td>
                    <td className="num">{formatCost(run.cost_usd, run.cost_known)}</td>
                    <td className="nowrap faint num" title={formatTime(run.created_at)}>
                      {formatTime(run.created_at, { seconds: false })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
