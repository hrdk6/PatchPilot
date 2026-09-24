/** Small presentational building blocks shared across pages. */

import type { ReactNode } from "react";

import type { PatchPilotApiError } from "../api/client";
import type { RunStatus } from "../api/types";

const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | "info" | "neutral"> = {
  fixed: "ok",
  running: "info",
  queued: "neutral",
  "tests-failed": "warn",
  "budget-exhausted": "warn",
  "patch-invalid": "danger",
  "sandbox-failed": "danger",
  error: "danger",
  cancelled: "neutral",
  succeeded: "ok",
  failed: "danger",
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

export function StatusBadge({ status }: { status: RunStatus | string }): JSX.Element {
  const tone = STATUS_TONE[status] ?? "neutral";
  const className = tone === "neutral" ? "badge" : `badge badge-${tone}`;
  const live = status === "running" || status === "queued";
  return (
    <span className={className} title={STATUS_HELP[status] ?? status}>
      <span className={live ? "badge-dot badge-pulse" : "badge-dot"} aria-hidden="true" />
      {status}
    </span>
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
  return (
    <div className="banner banner-error" role="alert">
      <strong>{error.message}</strong>
      {remediation ? <p>{remediation}</p> : null}
      {onRetry ? (
        <p>
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </p>
      ) : null}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }): JSX.Element {
  return (
    <div role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      <div className="skeleton" style={{ width: "40%" }} />
      <div className="skeleton" style={{ width: "75%" }} />
      <div className="skeleton" style={{ width: "60%" }} />
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
      <h3>{title}</h3>
      {children}
    </div>
  );
}

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
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
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

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}
