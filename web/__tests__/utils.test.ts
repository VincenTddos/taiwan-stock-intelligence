import { describe, expect, it } from "vitest";
import { formatMs, formatRelative } from "@/lib/utils";

describe("formatMs", () => {
  it("handles null", () => expect(formatMs(null)).toBe("—"));
  it("handles sub-millisecond", () => expect(formatMs(0.4)).toBe("<1 ms"));
  it("rounds", () => expect(formatMs(12.6)).toBe("13 ms"));
});

describe("formatRelative", () => {
  it("handles null", () => expect(formatRelative(null)).toBe("—"));
  it("reports recent timestamps", () =>
    expect(formatRelative(new Date().toISOString())).toBe("just now"));
  it("reports minutes", () =>
    expect(formatRelative(new Date(Date.now() - 120_000).toISOString())).toBe("2m ago"));
});
