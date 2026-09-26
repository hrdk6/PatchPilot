/** Run creation: repository, issue, model and execution budget. */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useResource, useSubmit } from "../api/hooks";
import type { CreateRunRequest } from "../api/types";
import { ErrorBanner, Loading } from "../components/common";

const FIXTURES = [
  {
    label: "calc_service — ZeroDivisionError on an empty bucket",
    repository: "fixtures/repos/calc_service",
    issue:
      "## Summary\n\n`calc_service.operations.percentage()` raises `ZeroDivisionError` " +
      "whenever a bucket expected zero items.\n\n## Reproduction\n\n```python\n" +
      "from calc_service.operations import percentage\npercentage(0, 0)  # ZeroDivisionError\n```\n\n" +
      "## Expected\n\nAn empty bucket expected nothing and missed nothing, so it should report 0.0%.",
  },
  {
    label: "text_pipeline — punctuation splits word counts",
    repository: "fixtures/repos/text_pipeline",
    issue:
      "## Summary\n\n`text_pipeline.analytics.top_words()` counts `\"Ship.\"`, `\"Ship,\"` and " +
      "`\"ship\"` as three different tokens, so vocabulary sizes are inflated.\n\n" +
      "## Expected\n\nThey are the same token. The analytics module looks right; the counting " +
      "happens downstream of whatever produces the tokens.",
  },
  {
    label: "task_queue — paginate() skips the first page",
    repository: "fixtures/repos/task_queue",
    issue:
      "## Summary\n\n`task_queue.pagination.paginate()` documents 1-indexed pages, but " +
      "`page=1` returns the second page of results.\n\n## Expected\n\n`page=1` returns the " +
      "first `per_page` items. The 1-indexed contract is correct and should not change.",
  },
];

export function NewRunPage(): JSX.Element {
  const navigate = useNavigate();
  const models = useResource(() => api.models(), []);
  const system = useResource(() => api.system(), []);

  const [form, setForm] = useState<CreateRunRequest>({
    repository_url: "",
    branch: null,
    commit_sha: null,
    issue_number: null,
    issue_title: null,
    issue_text: "",
    model: "mock:deterministic",
    max_repair_attempts: 3,
    validation_command: null,
    setup_command: null,
    lint_command: null,
    typecheck_command: null,
    sandbox_backend: "auto",
    timeout_seconds: 180,
    retrieval_top_k: 12,
  });

  const { submit, submitting, error } = useSubmit(api.createRun);
  const policy = system.data?.policy;
  const timeoutTouched = useRef(false);

  // The server decides the budget: start from its defaults and stay within its
  // caps, so the form never offers a value the API would refuse.
  useEffect(() => {
    if (!policy) return;
    setForm((previous) => ({
      ...previous,
      max_repair_attempts: Math.min(previous.max_repair_attempts, policy.max_repair_attempts),
      timeout_seconds: timeoutTouched.current
        ? Math.min(previous.timeout_seconds, policy.max_timeout_seconds)
        : policy.default_timeout_seconds,
      sandbox_backend:
        previous.sandbox_backend === "local" && !policy.local_sandbox_allowed
          ? "auto"
          : previous.sandbox_backend,
    }));
  }, [policy]);

  function update<K extends keyof CreateRunRequest>(key: K, value: CreateRunRequest[K]) {
    setForm((previous) => ({ ...previous, [key]: value }));
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const payload: CreateRunRequest = {
      ...form,
      issue_text: form.issue_text?.trim() || null,
      validation_command: form.validation_command?.trim() || null,
      setup_command: form.setup_command?.trim() || null,
      branch: form.branch?.trim() || null,
      commit_sha: form.commit_sha?.trim() || null,
    };
    const created = await submit(payload);
    if (created) navigate(`/runs/${created.id}`);
  }

  const sandbox = system.data?.sandbox;

  return (
    <>
      <div className="page-header">
        <h1>New run</h1>
        <p>
          Point PatchPilot at a repository and an issue. Indexing and sandboxed
          execution run in the background; you will land on the run page and can
          watch the state machine as it goes.
        </p>
      </div>

      {sandbox && !sandbox.isolated && sandbox.available ? (
        <div className="banner banner-warn" role="status">
          <strong>The sandbox is not isolated on this machine</strong>
          <p>
            {sandbox.reason} Generated code will run as your user. Only use
            repositories you trust until Docker is available.
          </p>
        </div>
      ) : null}
      {sandbox && !sandbox.available ? (
        <div className="banner banner-error" role="alert">
          <strong>No usable sandbox backend</strong>
          <p>{sandbox.reason}</p>
        </div>
      ) : null}

      {error ? <ErrorBanner error={error} /> : null}

      <form onSubmit={handleSubmit}>
        <div className="grid grid-2">
          <div className="card">
            <h2>Repository and issue</h2>

            <div className="field">
              <label htmlFor="repository">Repository URL or local path</label>
              <input
                id="repository"
                name="repository"
                required
                value={form.repository_url}
                placeholder="https://github.com/owner/repo or fixtures/repos/calc_service"
                onChange={(event) => update("repository_url", event.target.value)}
              />
              <p className="hint">
                Public HTTPS URLs are cloned and pinned to a commit SHA. Local
                directories are copied and pinned by content digest.
              </p>
            </div>

            <div className="row">
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="branch">Branch (optional)</label>
                <input
                  id="branch"
                  value={form.branch ?? ""}
                  onChange={(event) => update("branch", event.target.value)}
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="commit">Commit SHA (optional)</label>
                <input
                  id="commit"
                  value={form.commit_sha ?? ""}
                  onChange={(event) => update("commit_sha", event.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label htmlFor="issue-number">GitHub issue number (optional)</label>
              <input
                id="issue-number"
                type="number"
                min={1}
                value={form.issue_number ?? ""}
                onChange={(event) =>
                  update(
                    "issue_number",
                    event.target.value ? Number(event.target.value) : null,
                  )
                }
              />
              <p className="hint">
                Only used when the issue text below is empty. Works without a
                token for public repositories.
              </p>
            </div>

            <div className="field">
              <label htmlFor="issue-text">Issue text</label>
              <textarea
                id="issue-text"
                value={form.issue_text ?? ""}
                placeholder="Paste the bug report here. Steps to reproduce and expected behaviour help a lot."
                onChange={(event) => update("issue_text", event.target.value)}
              />
            </div>

            <fieldset disabled={policy ? !policy.local_repositories_allowed : false}>
              <legend>Load a bundled fixture</legend>
              <div className="button-row">
                {FIXTURES.map((fixture) => (
                  <button
                    key={fixture.repository}
                    type="button"
                    onClick={() => {
                      update("repository_url", fixture.repository);
                      update("issue_text", fixture.issue);
                    }}
                  >
                    {fixture.label.split(" — ")[0]}
                  </button>
                ))}
              </div>
              <p className="hint">
                {policy && !policy.local_repositories_allowed
                  ? "This server accepts git URLs only, so the bundled fixtures (local paths) are unavailable."
                  : "Three offline bug-fix tasks that need no API key and no network."}
              </p>
            </fieldset>
          </div>

          <div className="card">
            <h2>Model and budget</h2>

            <div className="field">
              <label htmlFor="model">Model</label>
              {models.initialLoading ? (
                <Loading label="Loading models" />
              ) : models.error ? (
                <ErrorBanner error={models.error} onRetry={models.refresh} />
              ) : (
                <>
                  <select
                    id="model"
                    value={form.model}
                    onChange={(event) => update("model", event.target.value)}
                  >
                    {(models.data ?? []).map((model) => (
                      <option key={model.id} value={model.id} disabled={!model.available}>
                        {model.id}
                        {model.available ? "" : " — not configured"}
                      </option>
                    ))}
                  </select>
                  <p className="hint">
                    {models.data?.find((model) => model.id === form.model)?.reason}
                  </p>
                </>
              )}
            </div>

            <div className="row">
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="attempts">Repair attempts</label>
                <input
                  id="attempts"
                  type="number"
                  min={1}
                  max={policy?.max_repair_attempts ?? 10}
                  value={form.max_repair_attempts}
                  onChange={(event) =>
                    update("max_repair_attempts", Number(event.target.value))
                  }
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="topk">Retrieved chunks</label>
                <input
                  id="topk"
                  type="number"
                  min={1}
                  max={60}
                  value={form.retrieval_top_k}
                  onChange={(event) => update("retrieval_top_k", Number(event.target.value))}
                />
              </div>
            </div>

            <div className="field">
              <label htmlFor="validation">Validation command</label>
              <input
                id="validation"
                value={form.validation_command ?? ""}
                placeholder="python -m pytest -q  (discovered automatically when empty)"
                onChange={(event) => update("validation_command", event.target.value)}
              />
              <p className="hint">
                This is the command that decides pass or fail. Left empty,
                PatchPilot discovers it from the project configuration.
              </p>
            </div>

            <div className="field">
              <label htmlFor="setup">Setup command (optional)</label>
              <input
                id="setup"
                value={form.setup_command ?? ""}
                placeholder="pip install -e ."
                onChange={(event) => update("setup_command", event.target.value)}
              />
              <p className="hint">
                The sandbox has no network by default, so dependency installation
                will fail unless you also allow it.
              </p>
            </div>

            <div className="row">
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="backend">Sandbox backend</label>
                <select
                  id="backend"
                  value={form.sandbox_backend}
                  onChange={(event) =>
                    update(
                      "sandbox_backend",
                      event.target.value as CreateRunRequest["sandbox_backend"],
                    )
                  }
                >
                  <option value="auto">auto (prefer Docker)</option>
                  <option value="docker">docker</option>
                  <option
                    value="local"
                    disabled={policy ? !policy.local_sandbox_allowed : false}
                  >
                    local (no isolation)
                    {policy && !policy.local_sandbox_allowed ? " — disabled on this server" : ""}
                  </option>
                </select>
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label htmlFor="timeout">Command timeout (s)</label>
                <input
                  id="timeout"
                  type="number"
                  min={10}
                  max={policy?.max_timeout_seconds ?? 3600}
                  value={form.timeout_seconds}
                  onChange={(event) => {
                    timeoutTouched.current = true;
                    update("timeout_seconds", Number(event.target.value));
                  }}
                />
              </div>
            </div>

            <div className="button-row">
              <button
                type="submit"
                className="primary"
                disabled={submitting || !form.repository_url.trim()}
              >
                {submitting ? "Queueing…" : "Start run"}
              </button>
              <span className="faint">
                Every patch needs human review before you merge it.
              </span>
            </div>
          </div>
        </div>
      </form>
    </>
  );
}
