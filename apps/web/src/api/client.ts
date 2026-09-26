/**
 * A small typed fetch client.
 *
 * It exists instead of a data-fetching library because the dashboard needs
 * exactly three behaviours -- request, poll, surface the error envelope -- and a
 * dependency that does much more would be harder to read than these 80 lines.
 */

import { authHeaders } from "./auth";
import type {
  ApiError,
  Benchmark,
  CreateRunRequest,
  Dataset,
  Job,
  ModelInfo,
  RunDetail,
  RunEvent,
  RunListResponse,
  RunSummary,
  SystemInfo,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
export const API_PREFIX = `${BASE_URL}/api/v1`;

export class PatchPilotApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly remediation: string | null;
  readonly correlationId: string | null;

  constructor(status: number, payload: Partial<ApiError>) {
    super(payload.message ?? `request failed with status ${status}`);
    this.name = "PatchPilotApiError";
    this.status = status;
    this.code = payload.code ?? "http_error";
    this.remediation = payload.remediation ?? null;
    this.correlationId = payload.correlation_id ?? null;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    throw new PatchPilotApiError(0, {
      code: "network_error",
      message: "Could not reach the PatchPilot API.",
      remediation:
        "Is the API running? Start it with `patchpilot serve` or `docker compose up`.",
      context: { cause: String(cause) },
    });
  }

  if (!response.ok) throw await errorFrom(response);

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function errorFrom(response: Response): Promise<PatchPilotApiError> {
  let payload: Partial<ApiError> = {};
  try {
    const body = (await response.json()) as Record<string, unknown>;
    payload = (body.detail as Partial<ApiError>) ?? (body as Partial<ApiError>);
  } catch {
    payload = { message: `${response.status} ${response.statusText}` };
  }
  return new PatchPilotApiError(response.status, payload);
}

function filenameFrom(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return match?.[1] ?? fallback;
}

/**
 * Download a file the API serves, sending the API key.
 *
 * A plain link cannot carry an Authorization header, so with a key set the
 * file is fetched and handed to the browser as a blob instead.
 */
export async function downloadFile(url: string, fallbackName: string): Promise<void> {
  const response = await fetch(url, { headers: authHeaders() });
  if (!response.ok) throw await errorFrom(response);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filenameFrom(response, fallbackName);
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export const api = {
  system: () => request<SystemInfo>("/system"),
  models: () => request<ModelInfo[]>("/models"),
  jobs: () => request<Job[]>("/jobs"),

  listRuns: (params: { limit?: number; offset?: number; status?: string; model?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    if (params.status) query.set("status", params.status);
    if (params.model) query.set("model", params.model);
    const suffix = query.toString() ? `?${query}` : "";
    return request<RunListResponse>(`/runs${suffix}`);
  },
  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),
  runEvents: (id: string, after = -1) =>
    request<RunEvent[]>(`/runs/${id}/events?after=${after}`),
  createRun: (payload: CreateRunRequest) =>
    request<RunSummary>("/runs", { method: "POST", body: JSON.stringify(payload) }),
  cancelRun: (id: string) => request<RunSummary>(`/runs/${id}/cancel`, { method: "POST" }),
  patchDownloadUrl: (id: string) => `${API_PREFIX}/runs/${id}/patch.diff`,
  artifactDownloadUrl: (id: string) => `${API_PREFIX}/artifacts/${id}/download`,

  datasets: () => request<Dataset[]>("/datasets"),
  listBenchmarks: () => request<Benchmark[]>("/benchmarks"),
  getBenchmark: (id: string, params: { tag?: string; result?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.tag) query.set("tag", params.tag);
    if (params.result) query.set("result", params.result);
    const suffix = query.toString() ? `?${query}` : "";
    return request<Benchmark>(`/benchmarks/${id}${suffix}`);
  },
  createBenchmark: (payload: { dataset: string; models: string[]; tags: string[] }) =>
    request<Job>("/benchmarks", { method: "POST", body: JSON.stringify(payload) }),
  benchmarkJsonUrl: (id: string) => `${API_PREFIX}/benchmarks/${id}/report.json`,
  benchmarkMarkdownUrl: (id: string) => `${API_PREFIX}/benchmarks/${id}/report.md`,
};

export type Api = typeof api;
