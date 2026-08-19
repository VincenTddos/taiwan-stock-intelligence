"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { api, type FactorSpec } from "@/lib/api/client";
import { cn } from "@/lib/utils";

/**
 * Factor catalogue.
 *
 * This page exists to make three things visible that a working screener would
 * otherwise hide:
 *
 * * which factors are **validated** and which are research-only, with the
 *   measurement that decided it
 * * which factors **cannot be computed at all**, and the exact input they are
 *   waiting for — thirty-seven of fifty-three, today
 * * the **specification** of each one: window, winsorisation, neutralisation,
 *   direction. A factor score without its definition is a number, not a
 *   measurement.
 *
 * The empty half of the catalogue is the useful half. It is the difference
 * between a platform that has not ingested financial statements and one that
 * has no value factors.
 */

const VERDICT: Record<string, { text: string; dot: string; label: string; note: string }> = {
  PASS: {
    text: "text-ok",
    dot: "bg-ok",
    label: "VALIDATED",
    note: "met every threshold in QUANT_ENGINE.md §2.4",
  },
  RESEARCH_ONLY: {
    text: "text-warn",
    dot: "bg-warn",
    label: "RESEARCH ONLY",
    note: "measured, but failed at least one threshold — excluded from any composite score",
  },
  INSUFFICIENT_DATA: {
    text: "text-fg-subtle",
    dot: "bg-off",
    label: "NOT ENOUGH DATA",
    note: "computed, but over too few sessions for the result to be evidence",
  },
  NOT_COMPUTABLE: {
    text: "text-fg-subtle",
    dot: "bg-off",
    label: "NO INPUTS",
    note: "the data this factor needs has not been ingested",
  },
};

function metric(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

function FactorRow({ factor }: { factor: FactorSpec }) {
  const [open, setOpen] = useState(false);
  const verdict = VERDICT[factor.verdict ?? "INSUFFICIENT_DATA"] ?? VERDICT.INSUFFICIENT_DATA!;
  const metrics = factor.metrics;

  return (
    <div className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="grid w-full grid-cols-[1.6fr_0.8fr_1fr_0.7fr_0.7fr_0.7fr] items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-bg-subtle/60"
      >
        <div>
          <div className="font-medium">{factor.name}</div>
          <div className="text-[11px] text-fg-subtle">{factor.description}</div>
        </div>
        <span className="text-xs text-fg-muted">{factor.category}</span>
        <span className={cn("flex items-center gap-2 text-xs font-semibold tracking-wider", verdict.text)}>
          <span className={cn("dot", verdict.dot)} />
          {verdict.label}
        </span>
        <span className="tabular text-xs text-fg-muted">{metric(metrics?.ic)}</span>
        <span className="tabular text-xs text-fg-muted">{metric(metrics?.ic_ir)}</span>
        <span className="tabular text-xs text-fg-subtle">
          {factor.direction > 0 ? "higher better" : "lower better"}
        </span>
      </button>

      {open ? (
        <div className="space-y-2 bg-bg-subtle/40 px-4 py-3 text-xs">
          <p className="text-fg-subtle">{verdict.note}</p>
          <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
            <Detail label="formula" value={factor.formula} mono />
            <Detail label="inputs" value={factor.inputs.join(", ")} mono />
            <Detail label="lookback" value={`${factor.lookback_days} sessions (min ${factor.min_periods})`} />
            <Detail
              label="winsorize"
              value={factor.winsorize ? `${factor.winsorize[0]} – ${factor.winsorize[1]}` : "none"}
            />
            <Detail label="standardise" value={factor.standardize} />
            <Detail
              label="neutralise"
              value={factor.neutralize.length ? factor.neutralize.join(", ") : "none"}
            />
          </div>

          {metrics?.missing_inputs?.length ? (
            <p className="text-fg-muted">
              waiting on <span className="font-mono text-warn">{metrics.missing_inputs.join(", ")}</span>
            </p>
          ) : null}

          {metrics && metrics.dates > 0 ? (
            <div className="grid gap-x-8 gap-y-1 sm:grid-cols-3">
              <Detail label="rank IC" value={metric(metrics.rank_ic)} />
              <Detail label="Q5 − Q1 t" value={metric(metrics.spread_t_stat, 2)} />
              <Detail label="coverage" value={metric(metrics.coverage, 3)} />
              <Detail label="stability" value={metric(metrics.stability, 3)} />
              <Detail label="sessions" value={String(metrics.dates)} />
              <Detail
                label="closest factor"
                value={
                  metrics.correlated_with
                    ? `${metrics.correlated_with} (${metric(metrics.max_correlation, 2)})`
                    : "—"
                }
              />
            </div>
          ) : null}

          {metrics?.failures?.length ? (
            <ul className="list-inside list-disc text-warn">
              {metrics.failures.map((failure) => (
                <li key={failure}>{failure}</li>
              ))}
            </ul>
          ) : null}

          {factor.evaluated_from ? (
            <p className="text-fg-subtle">
              measured over {factor.evaluated_from} → {factor.evaluated_to}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <span className="w-28 shrink-0 text-fg-subtle">{label}</span>
      <span className={cn("text-fg-muted", mono && "font-mono text-[11px]")}>{value}</span>
    </div>
  );
}

export default function FactorsPage() {
  const catalogue = useQuery({
    queryKey: ["factor-catalogue"],
    queryFn: () => api.factorCatalogue(),
    staleTime: 5 * 60 * 1000,
  });
  const indicators = useQuery({
    queryKey: ["indicator-catalogue"],
    queryFn: () => api.indicatorCatalogue(),
    staleTime: 5 * 60 * 1000,
  });

  // Memoised because the `?? []` allocates a new array on every render, which
  // would make the memo below recompute on every render — the lint rule is
  // right, and the fix is one line rather than a suppression.
  const factors = useMemo(() => catalogue.data?.data ?? [], [catalogue.data]);
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const factor of factors) {
      const key = factor.verdict ?? "INSUFFICIENT_DATA";
      out[key] = (out[key] ?? 0) + 1;
    }
    return out;
  }, [factors]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Factors</h1>
        <p className="text-sm text-fg-muted">
          Every declared factor, its specification, and whether it has earned a place in a score.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-4">
        {(["PASS", "RESEARCH_ONLY", "INSUFFICIENT_DATA", "NOT_COMPUTABLE"] as const).map((key) => (
          <Card key={key}>
            <CardBody>
              <div className={cn("text-2xl font-semibold tabular", VERDICT[key]!.text)}>
                {counts[key] ?? 0}
              </div>
              <div className="text-[11px] uppercase tracking-wider text-fg-subtle">
                {VERDICT[key]!.label}
              </div>
            </CardBody>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader
          title="Factor catalogue"
          subtitle={`${factors.length} declared · ${indicators.data?.data.length ?? "…"} indicators feed them`}
        />
        <CardBody className="p-0">
          <div className="grid grid-cols-[1.6fr_0.8fr_1fr_0.7fr_0.7fr_0.7fr] gap-3 border-b border-border px-4 py-2 text-[11px] uppercase tracking-wider text-fg-subtle">
            <span>factor</span>
            <span>category</span>
            <span>verdict</span>
            <span>IC</span>
            <span>IC-IR</span>
            <span>direction</span>
          </div>
          {catalogue.isLoading ? (
            <p className="px-4 py-6 text-sm text-fg-subtle">Loading…</p>
          ) : (
            factors.map((factor) => <FactorRow key={factor.name} factor={factor} />)
          )}
        </CardBody>
      </Card>
    </div>
  );
}
