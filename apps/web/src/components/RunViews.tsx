/** Components that render one run's evidence: timeline, retrieval, diff, sandbox. */

import type {
  Attempt,
  RetrievedChunk,
  RunEvent,
  SandboxExecution,
} from "../api/types";
import { EmptyState, formatDuration, StatusBadge } from "./common";

const TERMINAL_OK = new Set(["FINISHED"]);

export function StateTimeline({
  events,
  live,
}: {
  events: RunEvent[];
  live: boolean;
}): JSX.Element {
  if (events.length === 0) {
    return (
      <EmptyState title="No transitions yet">
        <p>
          The run is queued. The first transition appears as soon as a worker
          picks it up.
        </p>
      </EmptyState>
    );
  }
  const lastIndex = events.length - 1;
  return (
    <ol className="timeline">
      {events.map((event, position) => {
        const failed = /rejected|failed|unavailable|invalid|cancelled|error/i.test(
          event.reason,
        );
        const isLast = position === lastIndex;
        let markerClass = "marker";
        if (TERMINAL_OK.has(event.to_state) && !failed) markerClass += " marker-ok";
        else if (failed) markerClass += " marker-danger";
        else if (isLast && live) markerClass += " marker-active";
        return (
          <li key={event.index}>
            <span className={markerClass} aria-hidden="true" />
            <div>
              <div className="state">
                {event.from_state ? `${event.from_state} → ` : ""}
                {event.to_state}
                {event.attempt > 0 ? (
                  <span className="faint"> · attempt {event.attempt}</span>
                ) : null}
              </div>
              <div className="reason">{event.reason}</div>
            </div>
            <div className="duration">{formatDuration(event.duration_ms)}</div>
          </li>
        );
      })}
    </ol>
  );
}

export function RetrievalTable({
  chunks,
  truncated,
}: {
  chunks: RetrievedChunk[];
  truncated: boolean;
}): JSX.Element {
  if (chunks.length === 0) {
    return (
      <EmptyState title="Nothing retrieved yet">
        <p>The retrieval trace appears once the run reaches the RETRIEVE state.</p>
      </EmptyState>
    );
  }
  return (
    <>
      {truncated ? (
        <div className="banner banner-warn">
          <strong>Context budget reached</strong>
          <p>
            Some ranked chunks were dropped to stay inside the character budget.
            They are listed below the cut in the API response.
          </p>
        </div>
      ) : null}
      <div className="table-scroll">
        <table>
          <caption className="visually-hidden">
            Retrieved code chunks with their score and the reason each was selected
          </caption>
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Symbol</th>
              <th className="num">Score</th>
              <th>Why it was included</th>
            </tr>
          </thead>
          <tbody>
            {chunks.map((chunk) => (
              <tr key={`${chunk.path}:${chunk.symbol}:${chunk.rank}`}>
                <td className="num faint">{chunk.rank}</td>
                <td>
                  <div className="mono">{chunk.path}</div>
                  <div className="faint mono">
                    {chunk.symbol} · {chunk.symbol_type} · lines {chunk.start_line}–
                    {chunk.end_line}
                    {chunk.is_test ? " · test" : ""}
                  </div>
                </td>
                <td className="num mono">{chunk.score.toFixed(3)}</td>
                <td>
                  {chunk.reasons.map((reason) => (
                    <span
                      key={reason.reason}
                      className="reason-pill"
                      title={reason.detail || reason.reason}
                    >
                      {reason.reason} +{reason.weight.toFixed(2)}
                    </span>
                  ))}
                  {chunk.reasons[0]?.detail ? (
                    <div className="faint" style={{ marginTop: 2 }}>
                      {chunk.reasons[0].detail}
                    </div>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function DiffView({ diff }: { diff: string }): JSX.Element {
  const lines = diff.split("\n");
  return (
    <pre className="diff" aria-label="unified diff">
      <code>
        {lines.map((line, index) => {
          let className = "diff-line";
          if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) {
            className += " diff-meta";
          } else if (line.startsWith("@@")) {
            className += " diff-hunk";
          } else if (line.startsWith("+")) {
            className += " diff-add";
          } else if (line.startsWith("-")) {
            className += " diff-del";
          }
          return (
            <span key={index} className={className}>
              {line === "" ? " " : line}
            </span>
          );
        })}
      </code>
    </pre>
  );
}

export function SandboxOutput({
  execution,
  title = "Sandbox execution",
}: {
  execution: SandboxExecution;
  title?: string;
}): JSX.Element {
  return (
    <div className="stack">
      <div className="row">
        <h3 style={{ margin: 0 }}>{title}</h3>
        <StatusBadge status={execution.status} />
        <span className="badge">{execution.backend}</span>
        {execution.image ? <span className="badge">{execution.image}</span> : null}
        <span className="faint">{formatDuration(execution.duration_ms)}</span>
      </div>
      <div className="faint mono">
        limits: {execution.limits.cpus} cpu · {execution.limits.memory_mb}MB ·{" "}
        {execution.limits.pids} pids · {execution.limits.timeout_seconds}s · network{" "}
        {execution.limits.network} · user {execution.limits.user}
      </div>
      {execution.error ? (
        <div className="banner banner-error" role="alert">
          <strong>{execution.error}</strong>
        </div>
      ) : null}
      {execution.results.length === 0 ? (
        <p className="muted">No commands were executed.</p>
      ) : (
        execution.results.map((result, index) => (
          <details key={`${result.kind}-${index}`} open={result.exit_code !== 0}>
            <summary>
              <span className="mono">{result.command}</span>{" "}
              <span className={result.exit_code === 0 ? "badge badge-ok" : "badge badge-danger"}>
                {result.kind} · exit {result.exit_code ?? "—"}
              </span>{" "}
              <span className="faint">{formatDuration(result.duration_ms)}</span>
              {result.truncated ? (
                <span className="badge badge-warn" title={`${result.bytes_dropped} bytes dropped`}>
                  truncated
                </span>
              ) : null}
            </summary>
            <pre className="output">
              {result.stdout || result.stderr
                ? `${result.stdout}${result.stderr ? `\n--- stderr ---\n${result.stderr}` : ""}`
                : "(no output)"}
            </pre>
          </details>
        ))
      )}
    </div>
  );
}

export function AttemptCard({ attempt }: { attempt: Attempt }): JSX.Element {
  return (
    <div className="card">
      <div className="card-header">
        <h2>Attempt {attempt.attempt}</h2>
        <div className="row">
          {attempt.succeeded ? (
            <span className="badge badge-ok">validation passed</span>
          ) : (
            <span className="badge badge-warn">did not pass</span>
          )}
          <span className="faint">
            {formatDuration(attempt.duration_ms)} · {attempt.input_tokens.toLocaleString()} in /{" "}
            {attempt.output_tokens.toLocaleString()} out
          </span>
        </div>
      </div>

      {attempt.plan_error ? (
        <div className="banner banner-error">
          <strong>The model returned an invalid plan</strong>
          <p>{attempt.plan_error}</p>
        </div>
      ) : null}

      {attempt.plan ? (
        <details open>
          <summary>Plan (confidence {attempt.plan.confidence.toFixed(2)})</summary>
          <dl className="kv">
            <dt>Root cause</dt>
            <dd>{attempt.plan.root_cause}</dd>
            <dt>Strategy</dt>
            <dd>{attempt.plan.patch_strategy}</dd>
            <dt>Files</dt>
            <dd>{attempt.plan.files_to_change.join(", ") || "—"}</dd>
            <dt>Tests</dt>
            <dd>{attempt.plan.tests_to_run.join(", ") || "—"}</dd>
            <dt>Assumptions</dt>
            <dd>{attempt.plan.assumptions.join("; ") || "none stated"}</dd>
            <dt>Uncertainties</dt>
            <dd>{attempt.plan.uncertainties.join("; ") || "none stated"}</dd>
          </dl>
        </details>
      ) : null}

      {attempt.validation && !attempt.validation.valid ? (
        <div className="banner banner-error">
          <strong>Patch rejected: {attempt.validation.rejections.join(", ")}</strong>
          {attempt.validation.messages.map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      ) : null}

      {attempt.diff ? (
        <details open={!attempt.validation?.valid ? false : true}>
          <summary>
            Diff{" "}
            {attempt.validation ? (
              <span className="faint">
                ({attempt.validation.files_changed} file(s), +
                {attempt.validation.lines_added}/−{attempt.validation.lines_removed})
              </span>
            ) : null}
          </summary>
          <DiffView diff={attempt.diff} />
        </details>
      ) : null}

      {attempt.execution ? <SandboxOutput execution={attempt.execution} /> : null}

      {attempt.analysis ? (
        <div className="banner banner-info" style={{ marginTop: 12, marginBottom: 0 }}>
          <strong>Analysis</strong>
          <p>{attempt.analysis}</p>
        </div>
      ) : null}
    </div>
  );
}
