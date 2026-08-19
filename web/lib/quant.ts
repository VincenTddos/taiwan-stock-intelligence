/**
 * Pure helpers shared by the quant pages.
 *
 * These live here rather than inside a component for one reason: the values
 * they format arrive from the API as *strings*, because the backend sends
 * Decimals and a Decimal that has been through a float is no longer the number
 * the exchange published. Every place that renders one has to remember that,
 * and a helper is a better place to remember it than a JSX expression.
 */

/** Parse an API decimal string. Returns null for null, empty or non-numeric. */
export function decimal(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Format an API decimal for display, with an em dash for absent values. */
export function formatDecimal(value: string | null | undefined, digits = 3): string {
  const parsed = decimal(value);
  return parsed === null ? "—" : parsed.toFixed(digits);
}

/**
 * A radar needs at least three axes to be a shape rather than a line.
 * Below that the honest thing is to say how many were found.
 */
export const MIN_RADAR_AXES = 3;

export function radarPoints(
  factors: { factor: string; percentile: string | null }[],
  limit = 10,
): { label: string; value: number }[] {
  return factors
    .map((f) => ({ label: f.factor, value: decimal(f.percentile) }))
    .filter((p): p is { label: string; value: number } => p.value !== null)
    .slice(0, limit);
}
