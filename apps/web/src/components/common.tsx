/** Small presentational building blocks shared across pages. */

import { useState, type MouseEvent, type ReactNode } from "react";

import { getApiKey, setApiKey } from "../api/auth";
import { downloadFile, type PatchPilotApiError } from "../api/client";
import type { RunStatus } from "../api/types";
import { AlertIcon, CheckIcon, ClockIcon, CrossIcon, MinusIcon, StopIcon } from "./icons";

type Tone = "pass" | "fail" | "warn" | "live" | "neutral";

/**
 * Every status maps to one review-label tone. The pass tone (verified green) is
 * reserved for outcomes where validation actually passed; nothing else uses it.
 */
const STATUS_TONE: Record<string, Tone> = {
  fixed: "pass",
  succeeded: "neutral",
  completed: "neutral",
  running: "live",
  queued: "neutral",
  "tests-failed": "fail",
  "budget-exhausted": "fail",
  "patch-invalid": "fail",
  "sandbox-failed": "warn",
  timeout: "warn",
  "setup-failed": "warn",
  unavailable: "warn",
  "internal-error": "warn",
  error: "warn",
  failed: "warn",
  cancelled: "neutral",
};

const STATUS_HELP: Record<string, string> = {
  fixed: "The validation command passed inside the sandbox.",
  "tests-failed": "A valid patch applied, but the suite still failed.",
  "patch-invalid": "No proposed patch survived the safety and apply checks.",
  "sandbox-failed": "The sandbox was unavailable or could not run the commands.",
  "budget-exhausted": "The retry budget ran out before the tests passed.",
  cancelled: "Cancelled by a user.",
  error: "An infrastructure failure outside the sandbox.",
  running: "In progress.",
  queued: "Waiting for a worker.",
};

/** A finished job (not a validation) keeps a check, in ink rather than verdict green. */
const DONE = new Set(["succeeded", "completed"]);

function toneIcon(tone: Tone, status: string): JSX.Element {
  if (DONE.has(status)) return <CheckIcon />;
  switch (tone) {
    case "pass":
      return <CheckIcon />;
    case "fail":
      return <CrossIcon />;
    case "warn":
      return <AlertIcon />;
    case "live":
      return <ClockIcon />;
    default:
      return <MinusIcon />;
  }
}

export function statusTone(status: string): Tone {
  return STATUS_TONE[status] ?? "neutral";
}

export function StatusBadge({ status }: { status: RunStatus | string }): JSX.Element {
  const tone = statusTone(status);
  const live = status === "running" || status === "queued";
  return (
    <span
      className={`label label-${tone}${live ? " badge-pulse" : ""}`}
      title={STATUS_HELP[status] ?? status}
    >
      {toneIcon(tone, status)}
      {status}
    </span>
  );
}

function scoreFor(status: string): string {
  const tone = statusTone(status);
  return tone === "pass" ? "+1" : tone === "fail" || tone === "warn" ? "−1" : "0";
}

/** The verdict score as a small square: +1 verified, −1 did not pass, 0 pending. */
export function ScoreSquare({ status }: { status: string }): JSX.Element {
  const score = scoreFor(status);
  return (
    <span className={`score-square verdict-${statusTone(status)}`} title={`Verified ${score}`}>
      {score}
    </span>
  );
}

/**
 * The change page's score, in the vocabulary of a review tool: validation
 * passing is "Verified +1", a finished run that did not pass is "Verified −1",
 * and a run still going has no score yet.
 */
export function Verdict({ status, stopReason }: { status: string; stopReason: string | null }) {
  const tone = statusTone(status);
  const score = scoreFor(status);
  return (
    <div className={`verdict verdict-${tone}`}>
      <div className="verdict-score" aria-hidden="true">
        {score}
      </div>
      <div>
        <div className="verdict-label">
          Verified <span className="visually-hidden">{score}</span>
        </div>
        <div className="verdict-status">
          <StatusBadge status={status} />
          {stopReason && stopReason !== status ? (
            <span className="verdict-reason mono">{stopReason}</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="tag" title={title}>
      {children}
    </span>
  );
}

/**
 * Asks for the API key when the server refused a request for the lack of one.
 * Saving reloads the page, so every view re-fetches with the key.
 */
export function ApiKeyForm(): JSX.Element {
  const [value, setValue] = useState("");
  const saved = getApiKey() !== null;
  return (
    <form
      className="api-key-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!value.trim()) return;
        setApiKey(value.trim());
        window.location.reload();
      }}
    >
      <label htmlFor="api-key">API key</label>
      <div className="row">
        <input
          id="api-key"
          type="password"
          autoComplete="off"
          value={value}
          placeholder={saved ? "The saved key was refused; enter another" : "One of PATCHPILOT_API_KEYS"}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" className="primary" disabled={!value.trim()}>
          Save key
        </button>
        {saved ? (
          <button
            type="button"
            onClick={() => {
              setApiKey(null);
              window.location.reload();
            }}
          >
            Forget saved key
          </button>
        ) : null}
      </div>
      <p className="hint">Stored in this browser only, and sent with every API request.</p>
    </form>
  );
}

export function ErrorBanner({
  error,
  onRetry,
}: {
  error: PatchPilotApiError | { message: string; remediation?: string | null };
  onRetry?: () => void;
}): JSX.Element {
  const remediation = "remediation" in error ? error.remediation : null;
  const needsKey = "status" in error && error.status === 401;
  return (
    <div className="banner banner-error" role="alert">
      <strong>{error.message}</strong>
      {remediation && !needsKey ? <p>{remediation}</p> : null}
      {needsKey ? <ApiKeyForm /> : null}
      {onRetry && !needsKey ? (
        <p>
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </p>
      ) : null}
    </div>
  );
}

/**
 * A link to a file the API serves. It stays a plain link (so it can be opened
 * in a new tab or copied) and, when an API key is set, downloads through an
 * authenticated fetch instead, since a link cannot carry the key.
 */
export function DownloadLink({
  href,
  filename,
  className,
  children,
}: {
  href: string;
  filename: string;
  className?: string;
  children: ReactNode;
}): JSX.Element {
  const [failed, setFailed] = useState<string | null>(null);
  async function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    if (getApiKey() === null) return;
    event.preventDefault();
    setFailed(null);
    try {
      await downloadFile(href, filename);
    } catch (cause) {
      setFailed(cause instanceof Error ? cause.message : String(cause));
    }
  }
  return (
    <>
      <a href={href} className={className} onClick={(event) => void handleClick(event)}>
        {children}
      </a>
      {failed ? (
        <span className="faint" role="alert">
          {" "}
          Download failed: {failed}
        </span>
      ) : null}
    </>
  );
}

export function Loading({ label = "Loading" }: { label?: string }): JSX.Element {
  return (
    <div role="status" aria-live="polite" className="loading">
      <span className="visually-hidden">{label}</span>
      <div className="skeleton" style={{ width: "32%" }} />
      <div className="skeleton skeleton-title" style={{ width: "68%" }} />
      <div className="skeleton" style={{ width: "54%" }} />
    </div>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}): JSX.Element {
  return (
    <div className="empty">
      <StopIcon />
      <div>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

/** A labelled value in a change-info panel. */
export function Metric({
  label,
  value,
  sub,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
}): JSX.Element {
  return (
    <div className="info-row">
      <dt>{label}</dt>
      <dd>
        <span className="info-value">{value}</span>
        {sub ? <span className="info-sub">{sub}</span> : null}
      </dd>
    </div>
  );
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

export function formatCost(usd: number, known: boolean): string {
  if (!known) return "n/a";
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(5)}`;
  return `$${usd.toFixed(3)}`;
}

export function formatTime(iso: string | null, { seconds = true } = {}): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  });
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}
