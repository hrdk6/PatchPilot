import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { ModelInfo, SystemInfo } from "../api/types";
import { ROUTER_FUTURE } from "../router";
import { NewRunPage } from "./NewRunPage";

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => navigate };
});

const MODELS: ModelInfo[] = [
  {
    id: "mock:deterministic",
    provider: "mock",
    model: "deterministic",
    available: true,
    reason: "Deterministic test double; runs offline.",
    is_test_double: true,
    pricing_known: true,
    input_per_mtok: 0,
    output_per_mtok: 0,
  },
  {
    id: "openai:gpt-4o",
    provider: "openai",
    model: "gpt-4o",
    available: false,
    reason: "Not configured: set PATCHPILOT_OPENAI_API_KEY to enable.",
    is_test_double: false,
    pricing_known: true,
    input_per_mtok: 2.5,
    output_per_mtok: 10,
  },
];

function system(isolated: boolean): SystemInfo {
  return {
    version: "0.1.0",
    environment: "local",
    database: "sqlite+pysqlite",
    vector_store: "local",
    embedding_provider: "hash",
    default_model: "mock:deterministic",
    max_repair_attempts: 3,
    worker_running: true,
    queued_jobs: 0,
    sandbox: {
      backend: isolated ? "docker" : "local",
      available: true,
      isolated,
      reason: isolated ? "docker daemon 27.0" : "Docker unavailable; no isolation.",
      controls: {},
    },
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <NewRunPage />
    </MemoryRouter>,
  );
}

describe("NewRunPage", () => {
  beforeEach(() => {
    navigate.mockReset();
    vi.spyOn(api, "models").mockResolvedValue(MODELS);
    vi.spyOn(api, "system").mockResolvedValue(system(true));
  });

  it("disables models that are not configured and explains why", async () => {
    renderPage();
    const option = await screen.findByRole("option", {
      name: /openai:gpt-4o — not configured/,
    });
    expect(option).toBeDisabled();
  });

  it("warns when the sandbox provides no isolation", async () => {
    vi.spyOn(api, "system").mockResolvedValue(system(false));
    renderPage();
    expect(
      await screen.findByText("The sandbox is not isolated on this machine"),
    ).toBeInTheDocument();
  });

  it("fills the form from a bundled fixture", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "calc_service" }));
    expect(screen.getByLabelText(/Repository URL/)).toHaveValue(
      "fixtures/repos/calc_service",
    );
    const issue = screen.getByLabelText("Issue text") as HTMLTextAreaElement;
    expect(issue.value).toContain("ZeroDivisionError");
  });

  it("submits the run and navigates to its detail page", async () => {
    const user = userEvent.setup();
    const create = vi.spyOn(api, "createRun").mockResolvedValue({
      id: "run_created",
    } as never);
    renderPage();

    await user.type(screen.getByLabelText(/Repository URL/), "fixtures/repos/calc_service");
    await user.type(screen.getByLabelText("Issue text"), "percentage(0,0) raises");
    await user.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0]?.[0]).toMatchObject({
      repository_url: "fixtures/repos/calc_service",
      issue_text: "percentage(0,0) raises",
      model: "mock:deterministic",
      max_repair_attempts: 3,
    });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/runs/run_created"));
  });

  it("keeps the submit button disabled until a repository is given", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: "Start run" })).toBeDisabled();
  });
});
