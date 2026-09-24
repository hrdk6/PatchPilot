import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Attempt, RetrievedChunk, RunEvent, SandboxExecution } from "../api/types";
import {
  AttemptCard,
  DiffView,
  RetrievalTable,
  SandboxOutput,
  StageRail,
  StateTimeline,
} from "./RunViews";

const events: RunEvent[] = [
  {
    index: 0,
    from_state: null,
    to_state: "INGEST",
    reason: "pinned sha256:abc; sandbox=docker; baseline reproduced the failure",
    attempt: 0,
    duration_ms: 1200,
    detail: {},
    at: "2026-01-01T00:00:00Z",
  },
  {
    index: 1,
    from_state: "VALIDATE_PATCH",
    to_state: "SANDBOX_TEST",
    reason: "docker: validation passed",
    attempt: 1,
    duration_ms: 3400,
    detail: {},
    at: "2026-01-01T00:00:05Z",
  },
];

const execution: SandboxExecution = {
  backend: "docker",
  image: "python:3.12-slim-bookworm",
  status: "completed",
  limits: {
    cpus: 1,
    memory_mb: 1024,
    pids: 256,
    timeout_seconds: 180,
    max_output_bytes: 64000,
    network: "none",
    user: "10001:10001",
  },
  results: [
    {
      kind: "validation",
      command: "python -m pytest -q",
      exit_code: 1,
      status: "completed",
      stdout: "1 failed, 5 passed",
      stderr: "",
      duration_ms: 900,
      truncated: false,
      bytes_dropped: 0,
    },
  ],
  duration_ms: 1500,
  error: null,
  workspace_digest: "deadbeef",
};

describe("StateTimeline", () => {
  it("renders each transition with its reason and duration", () => {
    render(<StateTimeline events={events} live={false} />);
    expect(screen.getByText(/INGEST/)).toBeInTheDocument();
    expect(
      screen.getByText(/baseline reproduced the failure/),
    ).toBeInTheDocument();
    expect(screen.getByText("3.4s")).toBeInTheDocument();
    expect(screen.getByText(/attempt 1/)).toBeInTheDocument();
  });

  it("explains itself when there is nothing to show yet", () => {
    render(<StateTimeline events={[]} live />);
    expect(screen.getByText("No transitions yet")).toBeInTheDocument();
  });
});

describe("RetrievalTable", () => {
  const chunks: RetrievedChunk[] = [
    {
      rank: 1,
      path: "text_pipeline/tokenizer.py",
      symbol: "normalize",
      symbol_type: "function",
      start_line: 12,
      end_line: 20,
      score: 1.306,
      reasons: [
        { reason: "semantic-similarity", weight: 0.717, detail: "cosine=0.374" },
        {
          reason: "import-graph-neighbor",
          weight: 0.35,
          detail: "analytics.py imports tokenizer.py",
        },
      ],
      is_test: false,
      content: "def normalize(word): ...",
    },
  ];

  it("shows the score and every reason a chunk was retrieved", () => {
    render(<RetrievalTable chunks={chunks} truncated={false} />);
    const row = screen.getByRole("row", { name: /tokenizer\.py/ });
    expect(within(row).getByText("1.306")).toBeInTheDocument();
    expect(within(row).getByText(/semantic-similarity/)).toBeInTheDocument();
    expect(within(row).getByText(/import-graph-neighbor/)).toBeInTheDocument();
  });

  it("warns when the context budget dropped chunks", () => {
    render(<RetrievalTable chunks={chunks} truncated />);
    expect(screen.getByText("Context budget reached")).toBeInTheDocument();
  });

  it("highlights the chunks a chosen signal selected, and clears on a second press", async () => {
    const { container } = render(<RetrievalTable chunks={chunks} truncated={false} />);
    const key = screen.getByRole("button", { name: /import-graph-neighbor/ });
    await userEvent.click(key);
    expect(key).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector("table")).toHaveAttribute("data-focus", "import-graph-neighbor");
    expect(container.querySelectorAll("tbody tr[data-match]").length).toBeGreaterThan(0);
    await userEvent.click(key);
    expect(container.querySelector("table")).not.toHaveAttribute("data-focus");
  });
});

describe("DiffView", () => {
  it("marks added and removed lines", () => {
    const { container } = render(
      <DiffView diff={"--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"} />,
    );
    expect(container.querySelectorAll(".diff-add")).toHaveLength(1);
    expect(container.querySelectorAll(".diff-del")).toHaveLength(1);
    expect(container.querySelectorAll(".diff-hunk")).toHaveLength(1);
  });
});

describe("SandboxOutput", () => {
  it("surfaces the limits actually applied and the failing command", () => {
    render(<SandboxOutput execution={execution} />);
    expect(screen.getByText(/network none/)).toBeInTheDocument();
    expect(screen.getByText(/validation · exit 1/)).toBeInTheDocument();
    expect(screen.getByText(/1 failed, 5 passed/)).toBeInTheDocument();
  });
});

describe("AttemptCard", () => {
  it("shows a rejection instead of pretending the patch ran", () => {
    const attempt: Attempt = {
      attempt: 2,
      plan: null,
      plan_error: null,
      diff: "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-on: [push]\n+on: []\n",
      diff_hash: "abc",
      validation: {
        valid: false,
        rejections: ["protected-path"],
        messages: [".github/workflows/ci.yml is a protected path"],
        files_changed: 1,
        lines_added: 1,
        lines_removed: 1,
        changes: [],
      },
      execution: null,
      analysis: "This is a policy violation, so the run stops here.",
      should_retry: false,
      succeeded: false,
      input_tokens: 100,
      output_tokens: 20,
      duration_ms: 50,
    };
    render(<AttemptCard attempt={attempt} />);
    expect(screen.getByText(/Patch rejected: protected-path/)).toBeInTheDocument();
    expect(screen.getByText(/policy violation/)).toBeInTheDocument();
    expect(screen.getByText("did not pass")).toBeInTheDocument();
  });

  it("reports the attempt's own duration and token usage", () => {
    const attempt: Attempt = {
      attempt: 1,
      plan: null,
      plan_error: "no JSON object",
      diff: null,
      diff_hash: null,
      validation: null,
      execution: null,
      analysis: "",
      should_retry: true,
      succeeded: false,
      input_tokens: 1234,
      output_tokens: 56,
      duration_ms: 2500,
    };
    render(<AttemptCard attempt={attempt} />);
    expect(screen.getByText(/2\.5s · 1,234 in \/ 56 out/)).toBeInTheDocument();
  });
});

describe("StageRail", () => {
  it("marks the stage where a run that did not pass stopped", () => {
    const stopped: RunEvent[] = [
      { index: 0, from_state: null, to_state: "INGEST", reason: "pinned", attempt: 0, duration_ms: 900, detail: {}, at: "" },
      {
        index: 1,
        from_state: "INGEST",
        to_state: "FINISHED",
        reason: "final status sandbox-failed (sandbox-unavailable)",
        attempt: 0,
        duration_ms: 0,
        detail: {},
        at: "",
      },
    ];
    const { container } = render(
      <StageRail events={stopped} perStateMs={{ INGEST: 900 }} live={false} passed={false} />,
    );
    const marked = container.querySelector(".stage-stopped");
    expect(marked).toHaveTextContent("INGEST");
    expect(marked).toHaveTextContent("stopped");
    expect(screen.getByRole("table", { name: "Latency by state machine node" })).toHaveTextContent("900ms");
  });

  it("shows no stop marker on a run that passed", () => {
    const { container } = render(
      <StageRail events={events} perStateMs={{ INGEST: 1200 }} live={false} passed />,
    );
    expect(container.querySelector(".stage-stopped")).toBeNull();
  });
});

describe("DiffView gutters", () => {
  it("numbers old and new lines from the hunk header", () => {
    const { container } = render(
      <DiffView diff={"--- a/x.py\n+++ b/x.py\n@@ -10,2 +10,2 @@\n keep\n-old\n+new\n"} />,
    );
    const added = container.querySelector(".diff-add");
    const removed = container.querySelector(".diff-del");
    expect(added?.querySelectorAll(".diff-gutter")[1]).toHaveTextContent("11");
    expect(removed?.querySelectorAll(".diff-gutter")[0]).toHaveTextContent("11");
  });
});

describe("Collapsing earlier patchsets", () => {
  const twoAttempts: RunEvent[] = [
    { index: 0, from_state: null, to_state: "INGEST", reason: "pinned", attempt: 0, duration_ms: 5, detail: {}, at: "" },
    { index: 1, from_state: "RETRIEVE", to_state: "PLAN", reason: "root cause one", attempt: 1, duration_ms: 5, detail: {}, at: "" },
    { index: 2, from_state: "SANDBOX_TEST", to_state: "ANALYZE_RESULT", reason: "suite still fails", attempt: 1, duration_ms: 5, detail: {}, at: "" },
    { index: 3, from_state: "RETRIEVE", to_state: "PLAN", reason: "root cause two", attempt: 2, duration_ms: 5, detail: {}, at: "" },
  ];

  it("keeps only the latest patchset open in the review log, with the earlier outcome in its summary", () => {
    const { container } = render(<StateTimeline events={twoAttempts} live={false} />);
    const groups = container.querySelectorAll(".log-group details");
    expect(groups).toHaveLength(2);
    expect(groups[0]).not.toHaveAttribute("open");
    expect(groups[0]?.querySelector("summary")).toHaveTextContent("suite still fails");
    expect(groups[1]).toHaveAttribute("open");
  });

  it("renders a collapsed patchset card as a closed disclosure", () => {
    const attempt: Attempt = {
      attempt: 1,
      plan: null,
      plan_error: null,
      diff: null,
      diff_hash: null,
      validation: null,
      execution: null,
      analysis: "",
      should_retry: true,
      succeeded: false,
      input_tokens: 1,
      output_tokens: 1,
      duration_ms: 1,
    };
    const { container } = render(<AttemptCard attempt={attempt} collapsed />);
    expect(container.querySelector("details.patchset")).not.toHaveAttribute("open");
    expect(screen.getByText("Patchset 1")).toBeInTheDocument();
  });
});
