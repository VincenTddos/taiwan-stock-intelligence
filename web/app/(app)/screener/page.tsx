"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { DataProvenance } from "@/components/ui/DataProvenance";
import {
  ApiError,
  api,
  type FactorSpec,
  type ScreenerCondition,
  type ScreenerRow,
  type StockFactor,
} from "@/lib/api/client";
import { MIN_RADAR_AXES, formatDecimal, radarPoints } from "@/lib/quant";
import { cn } from "@/lib/utils";

/**
 * Screener.
 *
 * Three decisions here are about honesty rather than about screening.
 *
 * The result table shows **the values that passed**, not just the symbols. The
 * first question anyone asks of a screen is why a particular stock is in it,
 * and a list of codes cannot answer it.
 *
 * A factor with no scores for the session produces an **error naming it**,
 * surfaced verbatim from the backend, rather than an empty table. "Nothing
 * passed" and "this factor was never computed" are different answers.
 *
 * Every factor in the picker carries its **verdict**. A factor that has not met
 * the validity thresholds can still be screened on — that is legitimate
 * research — but it is labelled, because a research-only factor rendered
 * identically to a validated one is a recommendation nobody agreed to make.
 */

const FIELDS = [
  { value: "score", label: "score (direction applied)" },
  { value: "percentile", label: "percentile 0–1" },
  { value: "zscore", label: "z-score" },
  { value: "raw_value", label: "raw value" },
] as const;

const OPERATORS = [
  { value: "gte", label: "≥" },
  { value: "gt", label: ">" },
  { value: "lte", label: "≤" },
  { value: "lt", label: "<" },
  { value: "between", label: "between" },
] as const;

const VERDICT_STYLE: Record<string, { text: string; label: string }> = {
  PASS: { text: "text-ok", label: "VALIDATED" },
  RESEARCH_ONLY: { text: "text-warn", label: "RESEARCH ONLY" },
  INSUFFICIENT_DATA: { text: "text-fg-subtle", label: "NOT ENOUGH DATA" },
  NOT_COMPUTABLE: { text: "text-fg-subtle", label: "NO INPUTS" },
};

const ROW_HEIGHT = 34;
const VIEWPORT_ROWS = 14;

/** A radar of one symbol's factor scores, drawn rather than pulled in. */
function FactorRadar({ factors }: { factors: StockFactor[] }) {
  const points = radarPoints(factors);

  if (points.length < MIN_RADAR_AXES) {
    return (
      <p className="text-xs text-fg-subtle">
        A radar needs at least three factors with values; this symbol has {points.length}.
      </p>
    );
  }

  const size = 220;
  const centre = size / 2;
  const radius = centre - 34;
  const step = (Math.PI * 2) / points.length;

  const at = (index: number, fraction: number) => {
    const angle = index * step - Math.PI / 2;
    return [centre + Math.cos(angle) * radius * fraction, centre + Math.sin(angle) * radius * fraction];
  };

  const polygon = points.map((p, i) => at(i, Math.max(0.02, p.value)).join(",")).join(" ");

  return (
    <svg viewBox={`0 0 ${size} ${size}`} className="h-56 w-56" role="img" aria-label="factor radar">
      {[0.25, 0.5, 0.75, 1].map((ring) => (
        <polygon
          key={ring}
          points={points.map((_, i) => at(i, ring).join(",")).join(" ")}
          className="fill-none stroke-border"
          strokeWidth={0.5}
        />
      ))}
      {points.map((p, i) => {
        const [x, y] = at(i, 1);
        return <line key={p.label} x1={centre} y1={centre} x2={x} y2={y} className="stroke-border" strokeWidth={0.5} />;
      })}
      <polygon points={polygon} className="fill-accent/25 stroke-accent" strokeWidth={1.5} />
      {points.map((p, i) => {
        const [x, y] = at(i, 1.16);
        return (
          <text
            key={p.label}
            x={x}
            y={y}
            textAnchor="middle"
            dominantBaseline="middle"
            className="fill-current text-[7px] text-fg-subtle"
          >
            {p.label.length > 14 ? `${p.label.slice(0, 13)}…` : p.label}
          </text>
        );
      })}
    </svg>
  );
}

interface DraftCondition {
  factor: string;
  field: ScreenerCondition["field"];
  op: ScreenerCondition["op"];
  value: string;
  upper: string;
}

const DEFAULT_CONDITION: DraftCondition = {
  factor: "",
  field: "percentile",
  op: "gte",
  value: "0.8",
  upper: "",
};

export default function ScreenerPage() {
  const catalogue = useQuery({
    queryKey: ["factor-catalogue"],
    queryFn: () => api.factorCatalogue(),
    staleTime: 5 * 60 * 1000,
  });

  const computable = useMemo(
    () => (catalogue.data?.data ?? []).filter((f) => f.verdict !== "NOT_COMPUTABLE"),
    [catalogue.data],
  );

  const [conditions, setConditions] = useState<DraftCondition[]>([DEFAULT_CONDITION]);
  const [selected, setSelected] = useState<string | null>(null);
  const [scroll, setScroll] = useState(0);

  const run = useMutation({
    mutationFn: async () => {
      const payload: ScreenerCondition[] = conditions
        .filter((c) => c.factor)
        .map((c) => ({
          factor: c.factor,
          field: c.field,
          op: c.op,
          value: c.value === "" ? null : Number(c.value),
          upper: c.upper === "" ? null : Number(c.upper),
        }));
      return api.screener(payload, payload[0]?.factor ?? null, 200);
    },
  });

  const symbolFactors = useQuery({
    queryKey: ["stock-factors", selected],
    queryFn: () => api.stockFactors(selected as string),
    enabled: Boolean(selected),
  });

  const rows: ScreenerRow[] = run.data?.data ?? [];
  const shownFactors = useMemo(
    () => Array.from(new Set(conditions.map((c) => c.factor).filter(Boolean))),
    [conditions],
  );

  // A window over the rows rather than all of them. Two hundred rows is not a
  // performance problem today; the ceiling is five hundred and the point of
  // doing it now is that the table does not have to be rewritten when a
  // universe of two thousand arrives.
  const first = Math.max(0, Math.floor(scroll / ROW_HEIGHT) - 3);
  const visible = rows.slice(first, first + VIEWPORT_ROWS + 6);

  const update = (index: number, patch: Partial<DraftCondition>) =>
    setConditions((current) => current.map((c, i) => (i === index ? { ...c, ...patch } : c)));

  const problem = run.error instanceof ApiError ? run.error.problem : null;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Screener</h1>
        <p className="text-sm text-fg-muted">
          Filter the market on computed factors. Every result shows the values that put it there.
        </p>
      </header>

      <Card>
        <CardHeader title="Conditions" />
        <CardBody className="space-y-2">
          {conditions.map((condition, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <select
                aria-label="factor"
                className="min-w-56 rounded border border-border bg-bg-subtle px-2 py-1.5 text-sm"
                value={condition.factor}
                onChange={(e) => update(index, { factor: e.target.value })}
              >
                <option value="">select a factor…</option>
                {computable.map((factor: FactorSpec) => (
                  <option key={factor.name} value={factor.name}>
                    {factor.name} · {factor.category}
                    {factor.verdict && factor.verdict !== "PASS"
                      ? ` (${VERDICT_STYLE[factor.verdict]?.label ?? factor.verdict})`
                      : ""}
                  </option>
                ))}
              </select>

              <select
                aria-label="field"
                className="rounded border border-border bg-bg-subtle px-2 py-1.5 text-sm"
                value={condition.field}
                onChange={(e) => update(index, { field: e.target.value as DraftCondition["field"] })}
              >
                {FIELDS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>

              <select
                aria-label="operator"
                className="rounded border border-border bg-bg-subtle px-2 py-1.5 text-sm"
                value={condition.op}
                onChange={(e) => update(index, { op: e.target.value as DraftCondition["op"] })}
              >
                {OPERATORS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>

              <input
                aria-label="value"
                className="tabular w-24 rounded border border-border bg-bg-subtle px-2 py-1.5 text-sm"
                value={condition.value}
                onChange={(e) => update(index, { value: e.target.value })}
              />
              {condition.op === "between" ? (
                <input
                  aria-label="upper bound"
                  className="tabular w-24 rounded border border-border bg-bg-subtle px-2 py-1.5 text-sm"
                  value={condition.upper}
                  onChange={(e) => update(index, { upper: e.target.value })}
                />
              ) : null}

              <button
                type="button"
                className="text-xs text-fg-subtle hover:text-bad"
                onClick={() =>
                  setConditions((current) =>
                    current.length === 1 ? current : current.filter((_, i) => i !== index),
                  )
                }
              >
                remove
              </button>
            </div>
          ))}

          <div className="flex items-center gap-3 pt-1">
            <button
              type="button"
              className="text-xs text-accent hover:underline"
              onClick={() => setConditions((current) => [...current, { ...DEFAULT_CONDITION }])}
            >
              + add condition
            </button>
            <Button
              onClick={() => {
                setScroll(0);
                setSelected(null);
                run.mutate();
              }}
              disabled={run.isPending || conditions.every((c) => !c.factor)}
            >
              {run.isPending ? "Screening…" : "Run screen"}
            </Button>
            {catalogue.data ? (
              <span className="text-xs text-fg-subtle">
                {computable.length} of {catalogue.data.data.length} factors are computable today
              </span>
            ) : null}
          </div>

          {problem ? (
            <p className="rounded border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
              {problem.detail}
            </p>
          ) : null}
        </CardBody>
      </Card>

      {run.data ? (
        <Card>
          <CardHeader
            title={`${rows.length} match${rows.length === 1 ? "" : "es"}`}
            subtitle={
              run.data.meta.trading_date
                ? `session ${run.data.meta.trading_date} · feature version ${run.data.meta.feature_version ?? "—"}`
                : undefined
            }
          />
          <CardBody className="p-0">
            {rows.length === 0 ? (
              <p className="px-4 py-6 text-sm text-fg-subtle">
                No symbol satisfied every condition. The factors themselves were computed for this
                session — this is an empty result, not a missing one.
              </p>
            ) : (
              <div
                className="overflow-auto"
                style={{ maxHeight: ROW_HEIGHT * VIEWPORT_ROWS }}
                onScroll={(e) => setScroll(e.currentTarget.scrollTop)}
              >
                <div style={{ height: rows.length * ROW_HEIGHT, position: "relative" }}>
                  <div style={{ transform: `translateY(${first * ROW_HEIGHT}px)` }}>
                    {visible.map((row) => (
                      <button
                        type="button"
                        key={row.symbol}
                        onClick={() => setSelected(row.symbol === selected ? null : row.symbol)}
                        className={cn(
                          "grid w-full items-center gap-3 border-b border-border px-4 text-left text-sm",
                          row.symbol === selected ? "bg-bg-subtle" : "hover:bg-bg-subtle/60",
                        )}
                        style={{
                          height: ROW_HEIGHT,
                          gridTemplateColumns: `5rem 1fr repeat(${shownFactors.length}, 7rem)`,
                        }}
                      >
                        <span className="tabular font-medium">{row.symbol}</span>
                        <span className="truncate text-fg-muted">{row.name ?? "—"}</span>
                        {shownFactors.map((factor) => (
                          <span key={factor} className="tabular text-xs text-fg-muted">
                            {formatDecimal(row.values[factor]?.score ?? null)}
                          </span>
                        ))}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </CardBody>
        </Card>
      ) : null}

      {selected ? (
        <Card>
          <CardHeader title={`${selected} — factor profile`} subtitle="percentile across the market" />
          <CardBody className="flex flex-wrap items-start gap-6">
            {symbolFactors.data ? (
              <>
                <FactorRadar factors={symbolFactors.data.data} />
                <div className="min-w-72 flex-1">
                  <div className="grid grid-cols-[1fr_5rem_5rem_5rem] border-b border-border pb-1 text-[11px] uppercase tracking-wider text-fg-subtle">
                    <span>factor</span>
                    <span className="text-right">raw</span>
                    <span className="text-right">z</span>
                    <span className="text-right">pct</span>
                  </div>
                  {symbolFactors.data.data.map((factor) => (
                    <div
                      key={factor.factor}
                      className="grid grid-cols-[1fr_5rem_5rem_5rem] border-b border-border/60 py-1 text-xs last:border-0"
                    >
                      <span className="truncate">{factor.factor}</span>
                      <span className="tabular text-right text-fg-muted">{formatDecimal(factor.raw_value, 4)}</span>
                      <span className="tabular text-right text-fg-muted">{formatDecimal(factor.zscore)}</span>
                      <span className="tabular text-right text-fg-muted">{formatDecimal(factor.percentile)}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="text-sm text-fg-subtle">Loading factor profile…</p>
            )}
          </CardBody>
        </Card>
      ) : null}

      {run.data ? (
        <DataProvenance meta={run.data.meta} />
      ) : null}
    </div>
  );
}
