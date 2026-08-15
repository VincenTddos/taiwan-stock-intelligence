import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusLabel } from "@/components/ui/StatusDot";
import { DataProvenance } from "@/components/ui/DataProvenance";

describe("StatusLabel", () => {
  it.each([
    ["healthy", "HEALTHY"],
    ["degraded", "DEGRADED"],
    ["unhealthy", "UNHEALTHY"],
    ["disabled", "DISABLED"],
  ] as const)("renders %s", (status, label) => {
    render(<StatusLabel status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe("DataProvenance", () => {
  it("shows the DEMO DATA badge whenever data is mocked", () => {
    render(<DataProvenance meta={{ is_demo: true, source: ["MOCK"] }} />);
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
  });

  it("does not show the badge for real data", () => {
    render(<DataProvenance meta={{ is_demo: false, source: ["TWSE"] }} />);
    expect(screen.queryByText("DEMO DATA")).toBeNull();
  });

  it("shows STALE when data is past its expected lag", () => {
    render(<DataProvenance meta={{ is_stale: true, source: ["TWSE"] }} />);
    expect(screen.getByText("STALE")).toBeInTheDocument();
  });

  it("surfaces model version and confidence when present", () => {
    render(
      <DataProvenance
        meta={{ source: ["TWSE"], model_version: "stockrank-v1.4", confidence: 0.82 }}
      />,
    );
    expect(screen.getByText(/stockrank-v1\.4/)).toBeInTheDocument();
    expect(screen.getByText(/82%/)).toBeInTheDocument();
  });
});
