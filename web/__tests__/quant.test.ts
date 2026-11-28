import { describe, expect, it } from "vitest";

import { MIN_RADAR_AXES, decimal, formatDecimal, radarPoints } from "@/lib/quant";

describe("decimal", () => {
  it("parses the strings the API actually sends", () => {
    // Decimals cross the wire as strings so a price is never rounded by a
    // float on the way out of the database.
    expect(decimal("0.123456")).toBeCloseTo(0.123456, 10);
    expect(decimal("-1.5")).toBe(-1.5);
  });

  it("treats absent and unparseable as absent, never as zero", () => {
    // Zero is a real factor value. A missing one that rendered as 0 would sort
    // into the middle of a ranking and look like a measurement.
    expect(decimal(null)).toBeNull();
    expect(decimal(undefined)).toBeNull();
    expect(decimal("")).toBeNull();
    expect(decimal("n/a")).toBeNull();
  });
});

describe("formatDecimal", () => {
  it("renders an em dash rather than a number for absent values", () => {
    expect(formatDecimal(null)).toBe("—");
    expect(formatDecimal("1.23456", 3)).toBe("1.235");
    expect(formatDecimal("0", 2)).toBe("0.00");
  });
});

describe("radarPoints", () => {
  const factors = [
    { factor: "momentum_12_1", percentile: "0.9" },
    { factor: "hv_60", percentile: "0.2" },
    { factor: "log_market_cap", percentile: null },
    { factor: "avg_turnover_20d", percentile: "0.55" },
  ];

  it("drops factors with no value instead of plotting them at zero", () => {
    const points = radarPoints(factors);
    expect(points.map((p) => p.label)).toEqual([
      "momentum_12_1",
      "hv_60",
      "avg_turnover_20d",
    ]);
  });

  it("keeps the shape readable by capping the number of axes", () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      factor: `f${i}`,
      percentile: "0.5",
    }));
    expect(radarPoints(many)).toHaveLength(10);
  });

  it("needs three axes before a radar is a shape", () => {
    expect(radarPoints(factors.slice(0, 2)).length).toBeLessThan(MIN_RADAR_AXES);
  });
});
