import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { api, PatchPilotApiError } from "../api/client";
import type { RunSummary } from "../api/types";
import { ROUTER_FUTURE } from "../router";
import { RunsPage } from "./RunsPage";

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run_01abc",
    repository_id: "repo_1",
    issue_id: "task_1",
    issue_title: "percentage() raises ZeroDivisionError",
    repository_slug: "calc_service",
    model: "mock:deterministic",
    status: "fixed",
    state: "FINISHED",
    stop_reason: "validation-passed",
    repo_sha: "sha256:abc",
    attempts_used: 1,
    input_tokens: 5000,
    output_tokens: 400,
    cost_usd: 0,
    cost_known: true,
    sandbox_backend: "docker",
    sandbox_isolated: true,
    baseline_reproduced: true,
    error: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: "2026-01-01T00:00:09Z",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <RunsPage />
    </MemoryRouter>,
  );
}

describe("RunsPage", () => {
  it("lists each run under its issue as the subject, with verdict, model and tokens", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue({
      items: [run()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    renderPage();
    const subject = await screen.findByRole("link", {
      name: "percentage() raises ZeroDivisionError",
    });
    expect(subject).toHaveAttribute("href", "/runs/run_01abc");
    const row = screen.getByRole("row", { name: /run_01abc/ });
    expect(within(row).getByText(/calc_service/)).toBeInTheDocument();
    expect(within(row).getByText("fixed")).toBeInTheDocument();
    expect(within(row).getByText("mock:deterministic")).toBeInTheDocument();
    expect(within(row).getByText("5,400")).toBeInTheDocument();
    expect(screen.getByText("1 run(s)")).toBeInTheDocument();
  });

  it("flags a run that used the unisolated sandbox", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue({
      items: [run({ sandbox_backend: "local", sandbox_isolated: false })],
      total: 1,
      limit: 50,
      offset: 0,
    });
    renderPage();
    expect(await screen.findByText("not isolated")).toBeInTheDocument();
  });

  it("shows an empty state rather than an empty table", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue({
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
    });
    renderPage();
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });

  it("surfaces an actionable error when the API is unreachable", async () => {
    vi.spyOn(api, "listRuns").mockRejectedValue(
      new PatchPilotApiError(0, {
        code: "network_error",
        message: "Could not reach the PatchPilot API.",
        remediation: "Start it with `patchpilot serve`.",
      }),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Could not reach the PatchPilot API.",
      ),
    );
    expect(screen.getByText(/patchpilot serve/)).toBeInTheDocument();
  });
});
