/** The run list: everything that has been attempted, newest first. */

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
  StatusBadge,
} from "../components/common";

export function RunsPage(): JSX.Element {
  const [status, setStatus] = useState("");
  const runs = useResource(
    () => api.listRuns({ limit: 50, status: status || undefined }),
    [status],
    { pollMs: 4000 },
  );

  return (
    <>
      <div className="page-header">
        <h1>Runs</h1>
        <p>Every repair attempt, with the model, outcome and what it cost.</p>
      </div>

      <div className="card">
        <div className="card-header">
          <div className="row">
            <label htmlFor="status-filter" className="muted">
              Status
            </label>
            <select
              id="status-filter"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              style={{ width: "auto" }}
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
          </div>
          <div className="row">
            <span className="faint">
              {runs.data ? `${runs.data.total} run(s)` : ""}
            </span>
            <button type="button" onClick={runs.refresh} disabled={runs.loading}>
              Refresh
            </button>
          </div>
        </div>

        {runs.initialLoading ? (
          <Loading label="Loading runs" />
        ) : runs.error ? (
          <ErrorBanner error={runs.error} onRetry={runs.refresh} />
        ) : !runs.data || runs.data.items.length === 0 ? (
          <EmptyState title="No runs yet">
            <p>
              <Link to="/new">Start a run</Link> against one of the bundled
              fixture repositories — it needs no API key.
            </p>
          </EmptyState>
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="visually-hidden">Agent runs, newest first</caption>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Status</th>
                  <th>Model</th>
                  <th className="num">Attempts</th>
                  <th>Sandbox</th>
                  <th className="num">Tokens</th>
                  <th className="num">Est. cost</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/runs/${run.id}`} className="mono">
                        {run.id}
                      </Link>
                      {run.stop_reason ? (
                        <div className="faint">{run.stop_reason}</div>
                      ) : null}
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td className="mono">{run.model}</td>
                    <td className="num">{run.attempts_used}</td>
                    <td>
                      <span className="badge">{run.sandbox_backend ?? "—"}</span>
                      {run.sandbox_backend && !run.sandbox_isolated ? (
                        <span className="badge badge-warn" title="No isolation">
                          not isolated
                        </span>
                      ) : null}
                    </td>
                    <td className="num">
                      {formatNumber(run.input_tokens + run.output_tokens)}
                    </td>
                    <td className="num">{formatCost(run.cost_usd, run.cost_known)}</td>
                    <td className="nowrap faint">{formatTime(run.created_at)}</td>
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
