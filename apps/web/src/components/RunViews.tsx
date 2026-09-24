/**
 * Components that render one run's evidence, in the vocabulary of a code-review
 * tool: a review log of transitions, a stage rail, the retrieval trace, a diff
 * with gutters, checks, and one patchset per attempt.
 */

import { type ReactNode, useState } from "react";

import type {
  Attempt,
  RetrievedChunk,
  RunEvent,
  SandboxExecution,
} from "../api/types";
import { EmptyState, formatDuration, StatusBadge, Tag } from "./common";
import {
  ArrowRightIcon,
  CheckIcon,
  CommitIcon,
  CrossIcon,
  FileIcon,
  InfoIcon,
  MinusIcon,
  StopIcon,
} from "./icons";

/* ------------------------------------------------------------------ stages */

export const STAGES = [
  "INGEST",
  "INDEX",
  "RETRIEVE",
  "PLAN",
  "GENERATE_PATCH",
  "VALIDATE_PATCH",
  "SANDBOX_TEST",
  "ANALYZE_RESULT",
  "REPAIR_OR_FINISH",
] as const;

const FAILURE_WORDS = /rejected|failed|unavailable|invalid|cancelled|error|exhausted/i;

/**
 * The nine stages in their fixed order, each with the time spent there and how
 * many times the run entered it. A run that ended without passing shows a stop
 * marker on the stage where it stopped.
 */
export function StageRail({
  events,
  perStateMs,
  live,
  passed,
}: {
  events: RunEvent[];
  perStateMs: Record<string, number>;
  live: boolean;
  passed: boolean;
}): JSX.Element {
  const visits = new Map<string, number>();
  for (const event of events) {
    visits.set(event.to_state, (visits.get(event.to_state) ?? 0) + 1);
  }
  const finishing = events.find((event) => event.to_state === "FINISHED");
  const stoppedAt = !passed && finishing ? finishing.from_state : null;
  const current = live && events.length > 0 ? events[events.length - 1]?.to_state : null;
  const slowest = Math.max(1, ...STAGES.map((stage) => perStateMs[stage] ?? 0));

  return (
    <figure className="stage-rail">
      <figcaption className="visually-hidden">
        Time spent in each state-machine stage, in order
      </figcaption>
      <ol>
        {STAGES.map((stage) => {
          const ms = perStateMs[stage] ?? 0;
          const count = visits.get(stage) ?? 0;
          const reached = count > 0;
          let state = reached ? "stage-done" : "stage-idle";
          if (stage === current) state = "stage-current";
          if (stage === stoppedAt) state = "stage-stopped";
          return (
            <li key={stage} className={state}>
              <div className="stage-head">
                <span className="stage-name">{stage.replace(/_/g, " ")}</span>
                {count > 1 ? <span className="stage-visits">×{count}</span> : null}
              </div>
              <div className="stage-bar" aria-hidden="true">
                <span style={{ width: reached ? `max(${(ms / slowest) * 100}%, 3px)` : 0 }} />
              </div>
              <div className="stage-time">
                {stage === stoppedAt ? (
                  <span className="stage-stop">
                    <StopIcon /> stopped
                  </span>
                ) : null}
                <span className="num">{reached ? formatDuration(ms) : "—"}</span>
              </div>
            </li>
          );
        })}
      </ol>
      <table className="visually-hidden">
        <caption>Latency by state machine node</caption>
        <tbody>
          {STAGES.map((stage) => (
            <tr key={stage}>
              <th scope="row">{stage}</th>
              <td>{formatDuration(perStateMs[stage] ?? 0)}</td>
              <td>{visits.get(stage) ?? 0} visit(s)</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/* -------------------------------------------------------------- review log */

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
  const last = events[events.length - 1];

  function entry(event: RunEvent): JSX.Element {
    const failed = FAILURE_WORDS.test(event.reason);
    let tone = "log-step";
    let icon: JSX.Element | null = null;
    if (event.to_state === "FINISHED" && !failed && /fixed/.test(event.reason)) {
      tone = "log-pass";
      icon = <CheckIcon />;
    } else if (failed) {
      tone = "log-fail";
      icon = <CrossIcon />;
    } else if (live && event === last) {
      tone = "log-live";
      icon = <MinusIcon />;
    }
    return (
      <li key={event.index} className={`log-entry ${tone}`}>
        <span className="log-marker">{icon}</span>
        <div className="log-body">
          <div className="log-transition">
            {event.from_state ? (
              <>
                <span className="mono">{event.from_state}</span>
                <ArrowRightIcon className="icon log-arrow" title="to" />
              </>
            ) : null}
            <span className="mono log-to">{event.to_state}</span>
            {event.attempt > 0 ? <span className="log-attempt">attempt {event.attempt}</span> : null}
          </div>
          <p className="log-reason">{event.reason}</p>
        </div>
        <span className="log-duration num">{formatDuration(event.duration_ms)}</span>
      </li>
    );
  }

  // Transitions before the first plan, then one group per patchset. Earlier
  // patchsets collapse to a one-line outcome so a long run stays skimmable.
  const prelude = events.filter((event) => event.attempt === 0);
  const attempts = Array.from(new Set(events.map((event) => event.attempt).filter((n) => n > 0)));
  const latest = attempts[attempts.length - 1];

  return (
    <ol className="review-log">
      {prelude.map(entry)}
      {attempts.map((attempt) => {
        const group = events.filter((event) => event.attempt === attempt);
        const outcome = group.find((event) => event.to_state === "ANALYZE_RESULT")?.reason;
        return (
          <li key={`ps-${attempt}`} className="log-group">
            <details open={attempt === latest}>
              <summary className="log-patchset">
                <CommitIcon /> Patchset {attempt}
                <span className="log-patchset-meta">
                  {group.length} transitions{outcome ? ` · ${outcome}` : ""}
                </span>
              </summary>
              <ol>{group.map(entry)}</ol>
            </details>
          </li>
        );
      })}
    </ol>
  );
}

/* --------------------------------------------------------- retrieval trace */

/**
 * One key per retrieval signal, drawn from the validated categorical palette.
 * The two graph signals share a hue and differ by fill (solid versus hatched);
 * the two rare signals share the neutral key. The label always travels with it.
 */
export const SIGNALS: { reason: string; key: string; hatch?: boolean; help: string }[] = [
  { reason: "semantic-similarity", key: "sig-blue", help: "Embedding similarity to the issue and failure output" },
  { reason: "named-in-failure-output", key: "sig-orange", help: "Named or pointed at by the failing test output" },
  { reason: "import-graph-neighbor", key: "sig-aqua", help: "Imports, or is imported by, a top-ranked file" },
  { reason: "call-graph-neighbor", key: "sig-aqua", hatch: true, help: "Calls, or is called by, a top-ranked symbol" },
  { reason: "lexical-overlap", key: "sig-yellow", help: "Shares rare identifiers with the issue" },
  { reason: "test-referencing-symbol", key: "sig-magenta", help: "A test that exercises a top-ranked symbol" },
  { reason: "path-named-in-issue", key: "sig-neutral", help: "Its path appears in the issue text" },
  { reason: "repository-convention-file", key: "sig-neutral", hatch: true, help: "A repository convention file" },
];

const SIGNAL_BY_REASON = new Map(SIGNALS.map((signal) => [signal.reason, signal]));

export function SignalKey({ reason }: { reason: string }): JSX.Element {
  const signal = SIGNAL_BY_REASON.get(reason);
  const className = `sig ${signal?.key ?? "sig-neutral"}${signal?.hatch ? " sig-hatch" : ""}`;
  return <span className={className} aria-hidden="true" />;
}

export function SignalLegend({
  present,
  focus,
  onFocus,
}: {
  present: Set<string>;
  focus: string | null;
  onFocus: (reason: string | null) => void;
}): JSX.Element {
  return (
    <ul className="signal-legend" aria-label="Retrieval signals: select one to highlight the chunks it selected">
      {SIGNALS.filter((signal) => present.has(signal.reason)).map((signal) => (
        <li key={signal.reason}>
          <button
            type="button"
            title={signal.help}
            aria-pressed={focus === signal.reason}
            onClick={() => onFocus(focus === signal.reason ? null : signal.reason)}
          >
            <SignalKey reason={signal.reason} />
            {signal.reason}
          </button>
        </li>
      ))}
    </ul>
  );
}

export function RetrievalTable({
  chunks,
  truncated,
}: {
  chunks: RetrievedChunk[];
  truncated: boolean;
}): JSX.Element {
  const [focus, setFocus] = useState<string | null>(null);
  if (chunks.length === 0) {
    return (
      <EmptyState title="Nothing retrieved yet">
        <p>The retrieval trace appears once the run reaches the RETRIEVE state.</p>
      </EmptyState>
    );
  }
  const present = new Set(chunks.flatMap((chunk) => chunk.reasons.map((reason) => reason.reason)));
  const active = focus && present.has(focus) ? focus : null;
  const top = Math.max(...chunks.map((chunk) => chunk.score), 0.001);
  return (
    <>
      <SignalLegend present={present} focus={active} onFocus={setFocus} />
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
        <table className="trace" data-focus={active ?? undefined}>
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
              <tr
                key={`${chunk.path}:${chunk.symbol}:${chunk.rank}`}
                data-match={active && chunk.reasons.some((reason) => reason.reason === active) ? "" : undefined}
              >
                <td className="num trace-rank">{chunk.rank}</td>
                <td className="trace-symbol">
                  <div className="mono trace-path">
                    <FileIcon /> {chunk.path}
                  </div>
                  <div className="mono trace-meta">
                    {chunk.symbol} · {chunk.symbol_type} · L{chunk.start_line}–{chunk.end_line}
                    {chunk.is_test ? <Tag>test</Tag> : null}
                  </div>
                </td>
                <td className="num trace-score">
                  <span className="mono">{chunk.score.toFixed(3)}</span>
                  <span className="score-bar" aria-hidden="true">
                    <span style={{ width: `${(chunk.score / top) * 100}%` }} />
                  </span>
                </td>
                <td className="trace-reasons">
                  <ul>
                    {chunk.reasons.map((reason) => (
                      <li
                        key={reason.reason}
                        title={reason.detail || reason.reason}
                        data-match={reason.reason === active ? "" : undefined}
                      >
                        <SignalKey reason={reason.reason} />
                        {reason.reason} <span className="num">+{reason.weight.toFixed(2)}</span>
                      </li>
                    ))}
                  </ul>
                  {chunk.reasons[0]?.detail ? (
                    <div className="trace-detail mono">{chunk.reasons[0].detail}</div>
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

/* -------------------------------------------------------------------- diff */

interface DiffRow {
  kind: "meta" | "hunk" | "add" | "del" | "ctx";
  text: string;
  oldLine: number | null;
  newLine: number | null;
}

/** Parse a unified diff into rows with old/new line numbers for the gutters. */
export function diffRows(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const line of diff.replace(/\n$/, "").split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) {
      rows.push({ kind: "meta", text: line, oldLine: null, newLine: null });
    } else if (line.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      oldLine = match ? Number(match[1]) : 0;
      newLine = match ? Number(match[2]) : 0;
      rows.push({ kind: "hunk", text: line, oldLine: null, newLine: null });
    } else if (line.startsWith("+")) {
      rows.push({ kind: "add", text: line.slice(1), oldLine: null, newLine: newLine++ });
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", text: line.slice(1), oldLine: oldLine++, newLine: null });
    } else {
      rows.push({ kind: "ctx", text: line.slice(1), oldLine: oldLine++, newLine: newLine++ });
    }
  }
  return rows;
}

export function DiffView({ diff }: { diff: string }): JSX.Element {
  const rows = diffRows(diff);
  return (
    <div className="diff" role="region" aria-label="unified diff" tabIndex={0}>
      {rows.map((row, index) => {
        if (row.kind === "meta") {
          return (
            <div key={index} className="diff-line diff-meta">
              <span className="mono">{row.text}</span>
            </div>
          );
        }
        if (row.kind === "hunk") {
          return (
            <div key={index} className="diff-line diff-hunk">
              <span className="mono">{row.text}</span>
            </div>
          );
        }
        const sign = row.kind === "add" ? "+" : row.kind === "del" ? "−" : " ";
        return (
          <div key={index} className={`diff-line diff-${row.kind === "ctx" ? "ctx" : row.kind}`}>
            <span className="diff-gutter num">{row.oldLine ?? ""}</span>
            <span className="diff-gutter num">{row.newLine ?? ""}</span>
            <span className="diff-sign" aria-hidden="true">
              {sign}
            </span>
            <code className="diff-code">{row.text === "" ? " " : row.text}</code>
          </div>
        );
      })}
    </div>
  );
}

/** A diff with a file header bar per file, in the style of a review tool. */
export function FileDiff({
  diff,
  added,
  removed,
  note,
}: {
  diff: string;
  added?: number;
  removed?: number;
  note?: ReactNode;
}): JSX.Element {
  const target = /^\+\+\+ (?:b\/)?(.+)$/m.exec(diff)?.[1] ?? "patch";
  const adds = added ?? diffRows(diff).filter((row) => row.kind === "add").length;
  const dels = removed ?? diffRows(diff).filter((row) => row.kind === "del").length;
  return (
    <div className="file-diff">
      <div className="file-diff-head">
        <FileIcon />
        <span className="mono">{target}</span>
        <span className="diff-stat">
          <span className="diff-stat-add">+{adds}</span>
          <span className="diff-stat-del">−{dels}</span>
        </span>
        {note ? <span className="file-diff-note">{note}</span> : null}
      </div>
      <DiffView diff={diff} />
    </div>
  );
}

/* ------------------------------------------------------------------ checks */

export function SandboxOutput({
  execution,
  title = "Sandbox execution",
}: {
  execution: SandboxExecution;
  title?: string;
}): JSX.Element {
  return (
    <div className="checks">
      <div className="checks-head">
        <h4>{title}</h4>
        <StatusBadge status={execution.status} />
        <Tag>{execution.backend}</Tag>
        {execution.image ? <Tag>{execution.image}</Tag> : null}
        <span className="num checks-time">{formatDuration(execution.duration_ms)}</span>
      </div>
      <p className="checks-limits mono">
        limits: {execution.limits.cpus} cpu · {execution.limits.memory_mb}MB ·{" "}
        {execution.limits.pids} pids · {execution.limits.timeout_seconds}s · network{" "}
        {execution.limits.network} · user {execution.limits.user}
      </p>
      {execution.error ? (
        <div className="banner banner-error" role="alert">
          <strong>{execution.error}</strong>
        </div>
      ) : null}
      {execution.results.length === 0 ? (
        <p className="muted">No commands were executed.</p>
      ) : (
        <ul className="check-list">
          {execution.results.map((result, index) => {
            const ok = result.exit_code === 0;
            return (
              <li key={`${result.kind}-${index}`}>
                <details open={!ok}>
                  <summary>
                    <span className={ok ? "check-icon check-ok" : "check-icon check-bad"}>
                      {ok ? <CheckIcon /> : <CrossIcon />}
                    </span>
                    <span className="check-kind">
                      {result.kind} · exit {result.exit_code ?? "—"}
                    </span>
                    <span className="mono check-command">{result.command}</span>
                    {result.truncated ? (
                      <Tag title={`${result.bytes_dropped} bytes dropped`}>truncated</Tag>
                    ) : null}
                    <span className="num check-time">{formatDuration(result.duration_ms)}</span>
                  </summary>
                  <pre className="output">
                    {result.stdout || result.stderr
                      ? `${result.stdout}${result.stderr ? `\n--- stderr ---\n${result.stderr}` : ""}`
                      : "(no output)"}
                  </pre>
                </details>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- patchsets */

export function AttemptCard({
  attempt,
  collapsed = false,
  diffShownAbove = false,
}: {
  attempt: Attempt;
  /** Earlier patchsets render closed, as a one-line summary that opens in place. */
  collapsed?: boolean;
  /** This patchset's diff is already on the page as the proposed change. */
  diffShownAbove?: boolean;
}): JSX.Element {
  const rejected = attempt.validation && !attempt.validation.valid;
  const head = (
    <>
        <h2 id={`patchset-${attempt.attempt}`}>
          <CommitIcon /> Patchset {attempt.attempt}
        </h2>
        <div className="panel-head-meta">
          {attempt.succeeded ? (
            <span className="label label-pass">
              <CheckIcon />
              validation passed
            </span>
          ) : (
            <span className="label label-fail">
              <CrossIcon />
              did not pass
            </span>
          )}
          <span className="num faint">
            {formatDuration(attempt.duration_ms)} · {attempt.input_tokens.toLocaleString()} in /{" "}
            {attempt.output_tokens.toLocaleString()} out
          </span>
        </div>
    </>
  );

  const body = (
      <div className="panel-body stack">
        {attempt.plan_error ? (
          <div className="banner banner-error">
            <strong>The model returned an invalid plan</strong>
            <p>{attempt.plan_error}</p>
          </div>
        ) : null}

        {attempt.plan ? (
          <div className="commit-message">
            <p className="commit-subject">{attempt.plan.root_cause}</p>
            <p>{attempt.plan.patch_strategy}</p>
            <dl className="trailers mono">
              <dt>Confidence</dt>
              <dd>{attempt.plan.confidence.toFixed(2)}</dd>
              <dt>Files</dt>
              <dd>{attempt.plan.files_to_change.join(", ") || "—"}</dd>
              <dt>Tests</dt>
              <dd>{attempt.plan.tests_to_run.join(", ") || "—"}</dd>
              <dt>Assumptions</dt>
              <dd>{attempt.plan.assumptions.join("; ") || "none stated"}</dd>
              <dt>Uncertainties</dt>
              <dd>{attempt.plan.uncertainties.join("; ") || "none stated"}</dd>
            </dl>
          </div>
        ) : null}

        {rejected && attempt.validation ? (
          <div className="banner banner-error">
            <strong>Patch rejected: {attempt.validation.rejections.join(", ")}</strong>
            {attempt.validation.messages.map((message) => (
              <p key={message}>{message}</p>
            ))}
          </div>
        ) : null}

        {attempt.diff ? (
          <details open={!rejected && !diffShownAbove}>
            <summary>
              Diff
              {diffShownAbove ? <span className="faint"> · same as Proposed change</span> : null}
              {attempt.validation ? (
                <span className="faint">
                  {" · "}
                  {attempt.validation.files_changed} file(s), +{attempt.validation.lines_added}/−
                  {attempt.validation.lines_removed}
                </span>
              ) : null}
            </summary>
            <FileDiff
              diff={attempt.diff}
              added={attempt.validation?.lines_added}
              removed={attempt.validation?.lines_removed}
            />
          </details>
        ) : null}

        {attempt.execution ? <SandboxOutput execution={attempt.execution} title="Checks" /> : null}

        {attempt.analysis ? (
          <div className="agent-comment">
            <div className="agent-comment-head">
              <InfoIcon /> Analysis
            </div>
            <p>{attempt.analysis}</p>
          </div>
        ) : null}
      </div>
  );

  if (collapsed) {
    return (
      <details className="panel patchset">
        <summary className="panel-head">{head}</summary>
        {body}
      </details>
    );
  }
  return (
    <section className="panel patchset" aria-labelledby={`patchset-${attempt.attempt}`}>
      <header className="panel-head">{head}</header>
      {body}
    </section>
  );
}
