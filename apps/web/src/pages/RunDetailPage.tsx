/** One run as a change under review: verdict, stages, review log, patchsets. */

import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import { useResource, useSubmit } from "../api/hooks";
import { TERMINAL_STATUSES } from "../api/types";
import {
  DownloadLink,
  EmptyState,
  ErrorBanner,
  formatCost,
  formatDuration,
  formatNumber,
  formatTime,
  Loading,
  Metric,
  Tag,
  Verdict,
} from "../components/common";
import { AlertIcon, CrossIcon, DownloadIcon, ShieldIcon } from "../components/icons";
import {
  AttemptCard,
  FileDiff,
  RetrievalTable,
  SandboxOutput,
  StageRail,
  StateTimeline,
} from "../components/RunViews";

function BudgetTrack({ used, budget }: { used: number; budget: number }): JSX.Element {
  const slots = Math.max(budget, used, 1);
  return (
    <span className="budget" aria-label={`${used} of ${budget} attempts used`}>
      {Array.from({ length: slots }, (_, index) => (
        <span key={index} className={index < used ? "budget-slot budget-used" : "budget-slot"} />
      ))}
    </span>
  );
}

export function RunDetailPage(): JSX.Element {
  const { runId = "" } = useParams();
  const run = useResource(() => api.getRun(runId), [runId], { pollMs: 2500 });
  const cancel = useSubmit(() => api.cancelRun(runId));

  if (run.initialLoading) return <Loading label="Loading run" />;
  if (run.error) return <ErrorBanner error={run.error} onRetry={run.refresh} />;
  if (!run.data) return <EmptyState title="Run not found" />;

  const detail = run.data;
  const summary = detail.run;
  const live = !TERMINAL_STATUSES.includes(summary.status);
  const budget = Number(detail.config.max_repair_attempts ?? summary.attempts_used);
  const files = new Set(detail.retrieval.map((chunk) => chunk.path)).size;
  const finalAttempt = detail.attempts[detail.attempts.length - 1];

  return (
    <article className="change">
      <header className="change-head">
        <div className="change-title">
          <p className="change-id mono">
            <Link to="/runs">Runs</Link> / {summary.id}
          </p>
          <h1>{detail.issue.title || "(untitled issue)"}</h1>
          <p className="change-meta">
            <span className="mono">{detail.repository.url}</span>
            <span className="mono" title="Pinned revision">
              {summary.repo_sha ?? "unpinned"}
            </span>
            <span className="mono">{summary.model}</span>
            <span>opened {formatTime(summary.created_at)}</span>
          </p>
        </div>
        <div className="change-actions">
          <Verdict status={summary.status} stopReason={summary.stop_reason} />
          <div className="button-row">
            {detail.final_patch ? (
              <DownloadLink
                className="button primary"
                href={api.patchDownloadUrl(summary.id)}
                filename={`${summary.id}.diff`}
              >
                <DownloadIcon /> Download diff
              </DownloadLink>
            ) : null}
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
          </div>
        </div>
      </header>

      {summary.error ? (
        <div className="banner banner-error" role="alert">
          <strong>{summary.error}</strong>
        </div>
      ) : null}
      {summary.sandbox_backend && !summary.sandbox_isolated ? (
        <div className="banner banner-warn">
          <strong>This run used the unisolated local sandbox</strong>
          <p>
            Commands ran as the PatchPilot user on the host. Treat the result as
            valid only because the fixture repositories are trusted.
          </p>
        </div>
      ) : null}
      {summary.baseline_reproduced === false ? (
        <div className="banner banner-warn">
          <strong>The baseline command passed before any patch was applied</strong>
          <p>
            The bug did not reproduce, so a passing run here does not demonstrate
            a repair. Check the validation command and the commit.
          </p>
        </div>
      ) : null}

      <StageRail
        events={detail.events}
        perStateMs={detail.latency.per_state_ms ?? {}}
        live={live}
        passed={summary.status === "fixed"}
      />

      <div className="change-split">
        <section className="panel" aria-labelledby="change-info">
          <header className="panel-head">
            <h2 id="change-info">Change info</h2>
          </header>
          <dl className="info">
            <Metric label="Model" value={<span className="mono">{summary.model}</span>} />
            <Metric
              label="Patchsets"
              value={
                <>
                  <span className="num">
                    {summary.attempts_used} / {budget}
                  </span>
                  <BudgetTrack used={summary.attempts_used} budget={budget} />
                </>
              }
              sub="retry budget"
            />
            <Metric
              label="Baseline"
              value={
                summary.baseline_reproduced === null
                  ? "—"
                  : summary.baseline_reproduced
                    ? "failed before the patch"
                    : "passed before the patch"
              }
            />
            <Metric
              label="Sandbox"
              value={
                <>
                  <ShieldIcon /> {summary.sandbox_backend ?? "—"}
                </>
              }
              sub={summary.sandbox_isolated ? "isolated" : "NOT isolated"}
            />
            <Metric
              label="Retrieved"
              value={<span className="num">{detail.retrieval.length} chunks</span>}
              sub={`${files} file(s)`}
            />
            <Metric
              label="Tokens"
              value={
                <span className="num">
                  {formatNumber(summary.input_tokens + summary.output_tokens)}
                </span>
              }
              sub={`${formatNumber(summary.input_tokens)} in · ${formatNumber(summary.output_tokens)} out`}
            />
            <Metric
              label="Est. cost"
              value={<span className="num">{formatCost(summary.cost_usd, summary.cost_known)}</span>}
              sub={summary.cost_known ? "static pricing snapshot" : "no pricing entry"}
            />
            <Metric
              label="Wall clock"
              value={<span className="num">{formatDuration(detail.latency.total_ms ?? 0)}</span>}
              sub={`started ${formatTime(summary.started_at)}`}
            />
          </dl>
          <div className="info-commands">
            <h3>Commands</h3>
            <dl className="trailers mono">
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
              <p className="faint">
                {Object.entries(detail.commands.provenance as Record<string, string>)
                  .map(([key, why]) => `${key}: ${why}`)
                  .join(" · ")}
              </p>
            ) : null}
          </div>
        </section>

        <section className="panel" aria-labelledby="review-log">
          <header className="panel-head">
            <h2 id="review-log">Review log</h2>
            {live ? (
              <span className="label label-live badge-pulse">
                <AlertIcon /> live
              </span>
            ) : (
              <span className="faint num">{detail.events.length} transitions</span>
            )}
          </header>
          <div className="panel-body">
            <StateTimeline events={detail.events} live={live} />
          </div>
        </section>
      </div>

      {detail.final_patch ? (
        <section className="panel" aria-labelledby="final-patch">
          <header className="panel-head">
            <h2 id="final-patch">Proposed change</h2>
            <div className="panel-head-meta">
              {summary.status === "fixed" ? null : (
                <span className="label label-fail">
                  <CrossIcon /> did not pass validation
                </span>
              )}
              <span className="label label-warn">
                <AlertIcon /> review before merging
              </span>
            </div>
          </header>
          <div className="panel-body">
            <FileDiff
              diff={detail.final_patch}
              note={finalAttempt ? `from patchset ${finalAttempt.attempt}` : undefined}
            />
          </div>
        </section>
      ) : null}

      {detail.attempts.length === 0 ? (
        <section className="panel">
          <div className="panel-body">
            <EmptyState title="No patchsets yet">
              <p>A patchset appears once the model has produced a plan.</p>
            </EmptyState>
          </div>
        </section>
      ) : (
        detail.attempts.map((attempt) => (
          <AttemptCard
            key={attempt.attempt}
            attempt={attempt}
            collapsed={attempt.attempt !== finalAttempt?.attempt}
            diffShownAbove={Boolean(attempt.diff) && attempt.diff === detail.final_patch}
          />
        ))
      )}

      <section className="panel" aria-labelledby="retrieval">
        <header className="panel-head">
          <h2 id="retrieval">Retrieval trace</h2>
          <span className="faint">why each symbol was put in front of the model</span>
        </header>
        <div className="panel-body">
          <RetrievalTable chunks={detail.retrieval} truncated={detail.retrieval_truncated} />
        </div>
      </section>

      {detail.baseline ? (
        <section className="panel" aria-labelledby="baseline">
          <header className="panel-head">
            <h2 id="baseline">Baseline, before patching</h2>
          </header>
          <div className="panel-body">
            <SandboxOutput execution={detail.baseline} title="Baseline execution" />
          </div>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="artifacts">
        <header className="panel-head">
          <h2 id="artifacts">Artifacts</h2>
          <span className="faint">kept after the sandbox was destroyed</span>
        </header>
        <div className="panel-body">
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
                    <th className="num">Patchset</th>
                    <th className="num">Size</th>
                    <th>SHA-256</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.artifacts.map((artifact) => (
                    <tr key={artifact.id}>
                      <td>
                        <DownloadLink
                          href={api.artifactDownloadUrl(artifact.id)}
                          filename={artifact.filename}
                          className="mono"
                        >
                          {artifact.filename}
                        </DownloadLink>
                      </td>
                      <td>
                        <Tag>{artifact.kind}</Tag>
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
      </section>

      <details className="panel panel-disclosure">
        <summary>Issue text</summary>
        <pre className="output">{detail.issue.body || "(empty)"}</pre>
      </details>
      <details className="panel panel-disclosure">
        <summary>Run configuration</summary>
        <pre className="output">{JSON.stringify(detail.config, null, 2)}</pre>
      </details>
    </article>
  );
}
