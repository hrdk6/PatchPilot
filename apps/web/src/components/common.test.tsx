import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BarChart } from "./BarChart";
import {
  formatCost,
  formatDuration,
  formatNumber,
  percent,
  StatusBadge,
} from "./common";

describe("formatters", () => {
  it("formats durations at three scales", () => {
    expect(formatDuration(450)).toBe("450ms");
    expect(formatDuration(2500)).toBe("2.5s");
    expect(formatDuration(95_000)).toBe("1m 35s");
    expect(formatDuration(null)).toBe("—");
  });

  it("never shows a cost it cannot stand behind", () => {
    expect(formatCost(0.12345, true)).toBe("$0.123");
    expect(formatCost(0.000004, true)).toBe("$0.00000");
    expect(formatCost(0, true)).toBe("$0.00");
    expect(formatCost(1.5, false)).toBe("n/a");
  });

  it("formats counts and percentages", () => {
    expect(formatNumber(12345)).toBe((12345).toLocaleString());
    expect(percent(0.6667)).toBe("67%");
  });
});

describe("StatusBadge", () => {
  it("explains what a terminal status means", () => {
    render(<StatusBadge status="budget-exhausted" />);
    expect(screen.getByTitle(/retry budget ran out/)).toBeInTheDocument();
  });

  it("marks live runs so they read as in-progress", () => {
    const { container } = render(<StatusBadge status="running" />);
    expect(container.querySelector(".badge-pulse")).not.toBeNull();
  });
});

describe("BarChart", () => {
  it("exposes the same data as a table for assistive technology", () => {
    render(
      <BarChart
        title="Pass rate by model"
        max={1}
        data={[
          { label: "mock:deterministic", value: 1, display: "100% (3/3)" },
          { label: "mock:stubborn", value: 0, display: "0% (0/3)" },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "Pass rate by model" })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Pass rate by model" });
    expect(table).toHaveTextContent("mock:deterministic");
    expect(table).toHaveTextContent("100% (3/3)");
  });

  it("says so rather than drawing an empty chart", () => {
    render(<BarChart title="empty" data={[]} />);
    expect(screen.getByText("No data.")).toBeInTheDocument();
  });
});
