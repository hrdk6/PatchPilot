/**
 * Types mirroring the `/api/v1` schemas.
 *
 * Hand-written rather than generated so the dashboard has a small, readable
 * surface; `npm run build` fails if a response stops matching what a component
 * reads. The API is versioned, so drift is a deliberate change, not a surprise.
 */

export type RunStatus =
  | "queued"
  | "running"
  | "fixed"
  | "tests-failed"
  | "patch-invalid"
  | "sandbox-failed"
  | "budget-exhausted"
  | "cancelled"
  | "error";

export const TERMINAL_STATUSES: RunStatus[] = [
  "fixed",
  "tests-failed",
  "patch-invalid",
  "sandbox-failed",
  "budget-exhausted",
  "cancelled",
  "error",
];

export interface ApiError {
  code: string;
  message: string;
  remediation?: string | null;
  context?: Record<string, unknown>;
  correlation_id?: string | null;
}

export interface RunSummary {
  id: string;
  repository_id: string;
  issue_id: string;
  model: string;
  status: RunStatus;
  state: string;
  stop_reason: string | null;
  repo_sha: string | null;
  attempts_used: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_known: boolean;
  sandbox_backend: string | null;
  sandbox_isolated: boolean;
  baseline_reproduced: boolean | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunListResponse {
  items: RunSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface RunEvent {
  index: number;
  from_state: string | null;
  to_state: string;
  reason: string;
  attempt: number;
  duration_ms: number;
  detail: Record<string, unknown>;
  at: string;
}

export interface RepairPlan {
  root_cause: string;
  files_to_change: string[];
  tests_to_run: string[];
  patch_strategy: string;
  assumptions: string[];
  uncertainties: string[];
  confidence: number;
}

export interface CommandResult {
  kind: string;
  command: string;
  exit_code: number | null;
  status: string;
  stdout: string;
  stderr: string;
  duration_ms: number;
  truncated: boolean;
  bytes_dropped: number;
}

export interface SandboxExecution {
  backend: string;
  image: string | null;
  status: string;
  limits: {
    cpus: number;
    memory_mb: number;
    pids: number;
    timeout_seconds: number;
    max_output_bytes: number;
    network: string;
    user: string;
  };
  results: CommandResult[];
  duration_ms: number;
  error: string | null;
  workspace_digest: string | null;
}

export interface PatchValidation {
  valid: boolean;
  rejections: string[];
  messages: string[];
  files_changed: number;
  lines_added: number;
  lines_removed: number;
  changes: {
    path: string;
    change_type: string;
    lines_added: number;
    lines_removed: number;
  }[];
}

export interface Attempt {
  attempt: number;
  plan: RepairPlan | null;
  plan_error: string | null;
  diff: string | null;
  diff_hash: string | null;
  validation: PatchValidation | null;
  execution: SandboxExecution | null;
  analysis: string;
  should_retry: boolean;
  succeeded: boolean;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
}

export interface RetrievedChunk {
  rank: number;
  path: string;
  symbol: string;
  symbol_type: string;
  start_line: number;
  end_line: number;
  score: number;
  reasons: { reason: string; weight: number; detail: string }[];
  is_test: boolean;
  content: string;
}

export interface Artifact {
  id: string;
  run_id: string;
  attempt: number;
  kind: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export interface Repository {
  id: string;
  url: string;
  slug: string;
  branch: string | null;
  commit_sha: string | null;
  source: string;
  local_path: string | null;
  created_at: string;
}

export interface Issue {
  id: string;
  repository_id: string;
  source: string;
  number: number | null;
  title: string;
  body: string;
  url: string | null;
  labels: string[];
  created_at: string;
}

export interface RunDetail {
  run: RunSummary;
  repository: Repository;
  issue: Issue;
  config: Record<string, unknown>;
  commands: Record<string, unknown>;
  latency: { per_state_ms?: Record<string, number>; total_ms?: number };
  baseline: SandboxExecution | null;
  final_patch: string | null;
  events: RunEvent[];
  attempts: Attempt[];
  artifacts: Artifact[];
  retrieval: RetrievedChunk[];
  retrieval_truncated: boolean;
  conventions: Record<string, string>;
}

export interface ModelInfo {
  id: string;
  provider: string;
  model: string;
  available: boolean;
  reason: string;
  is_test_double: boolean;
  pricing_known: boolean;
  input_per_mtok: number | null;
  output_per_mtok: number | null;
}

export interface SandboxStatus {
  backend: string;
  available: boolean;
  isolated: boolean;
  reason: string;
  controls: Record<string, unknown>;
}

export interface SystemInfo {
  version: string;
  environment: string;
  database: string;
  vector_store: string;
  embedding_provider: string;
  default_model: string;
  max_repair_attempts: number;
  sandbox: SandboxStatus;
  worker_running: boolean;
  queued_jobs: number;
}

export interface DatasetTask {
  id: string;
  repository_url: string;
  commit_sha: string | null;
  title: string;
  difficulty: string;
  tags: string[];
  validation_command: string;
  baseline_command: string | null;
  expected_files: string[];
}

export interface Dataset {
  name: string;
  path: string;
  description: string;
  task_count: number;
  tags: string[];
  tasks: DatasetTask[];
}

export interface BenchmarkResult {
  task_id: string;
  model: string;
  run_id: string | null;
  status: string;
  passed: boolean;
  baseline_failed: boolean | null;
  patch_applied: boolean;
  attempts_used: number;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  sandbox_failed: boolean;
  similarity: number | null;
  tags: string[];
  detail: Record<string, unknown>;
}

export interface ModelSummary {
  model: string;
  tasks: number;
  passed: number;
  pass_rate: number;
  adjusted_pass_rate: number;
  adjusted_denominator: number;
  baseline_confirmation_rate: number;
  patch_application_rate: number;
  median_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  per_state_latency_ms: Record<string, number>;
  total_tokens: number;
  estimated_cost_usd: number;
  cost_pricing_known: boolean;
  cost_per_task_usd: number;
  cost_per_fix_usd: number | null;
  retries_per_task: number;
  sandbox_failure_rate: number;
  status_counts: Record<string, number>;
  small_sample: boolean;
  is_test_double: boolean;
  pass_rate_ci95?: [number, number];
}

export interface BenchmarkReport {
  benchmark_id: string;
  dataset: string;
  models: string[];
  skipped_models: Record<string, string>;
  environment: Record<string, unknown>;
  summaries: ModelSummary[];
}

export interface Benchmark {
  id: string;
  dataset: string;
  models: string[];
  tags: string[];
  status: string;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  report: BenchmarkReport | null;
  results: BenchmarkResult[];
}

export interface Job {
  id: string;
  type: string;
  status: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreateRunRequest {
  repository_url: string;
  branch?: string | null;
  commit_sha?: string | null;
  issue_number?: number | null;
  issue_title?: string | null;
  issue_text?: string | null;
  model: string;
  max_repair_attempts: number;
  validation_command?: string | null;
  setup_command?: string | null;
  lint_command?: string | null;
  typecheck_command?: string | null;
  reproduction_command?: string | null;
  sandbox_backend: "auto" | "docker" | "local";
  timeout_seconds: number;
  retrieval_top_k: number;
}
