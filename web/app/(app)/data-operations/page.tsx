"use client";

import { useQuery } from "@tanstack/react-query";

import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { DataProvenance } from "@/components/ui/DataProvenance";
import { api, type DataOperations, type DatasetHealth, type SourceHealth } from "@/lib/api/client";
import { formatRelative } from "@/lib/utils";
import { cn } from "@/lib/utils";

/**
 * Data Operations.
 *
 * Phase 2's dashboard is deliberately about the *plumbing*, not about the
 * market. It answers "is the data trustworthy right now?" — freshness, record
 * counts, source health, quarantine. There is no analysis and no advice here,
 * because none of that exists yet and a page that implied otherwise would be
 * lying.
 */

const FRESHNESS_STYLE: Record<string, { dot: string; text: string; label: string }> = {
  FRESH: { dot: "bg-ok", text: "text-ok", label: "FRESH" },
  STALE: { dot: "bg-warn", text: "text-warn", label: "STALE" },
  DEGRADED: { dot: "bg-warn", text: "text-warn", label: "DEGRADED" },
  MISSING: { dot: "bg-bad", text: "text-bad", label: "MISSING" },
};

const SOURCE_STYLE: Record<string, { dot: string; text: string }> = {
  ACTIVE: { dot: "bg-ok", text: "text-ok" },
  DEGRADED: { dot: "bg-warn", text: "text-warn" },
  UNVERIFIED: { dot: "bg-fg-subtle", text: "text-fg-subtle" },
  DISABLED: { dot: "bg-off", text: "text-fg-subtle" },
};

function DatasetRow({ d }: { d: DatasetHealth }) {
  const style = FRESHNESS_STYLE[d.status] ?? FRESHNESS_STYLE.MISSING!;
  return (
    <div className="grid grid-cols-[1.4fr_0.9fr_0.9fr_0.8fr_0.7fr_0.7fr] items-center gap-3 border-b border-border px-4 py-2.5 text-sm last:border-0">
      <div>
        <div className="font-medium">{d.dataset}</div>
        {d.description ? (
          <div className="text-[11px] text-fg-subtle">{d.description}</div>
        ) : null}
      </div>
      <span className={cn("flex items-center gap-2 text-xs font-semibold tracking-wider", style.text)}>
        <span className={cn("dot", style.dot)} />
        {style.label}
      </span>
      <span className="tabular text-xs text-fg-muted">{d.last_data_date ?? "—"}</span>
      <span className="tabular text-xs text-fg-muted">
        {formatRelative(d.last_ingested_at)}
      </span>
      <span className="tabular text-xs text-fg-muted">
        {d.record_count.toLocaleString()}
      </span>
      <span
        className={cn(
          "tabular text-xs",
          d.quarantined > 0 ? "font-semibold text-warn" : "text-fg-subtle",
        )}
      >
        {d.quarantined}
      </span>
    </div>
  );
}

function SourceRow({ s }: { s: SourceHealth }) {
  const style = SOURCE_STYLE[s.status] ?? SOURCE_STYLE.DISABLED!;
  return (
    <div className="border-b border-border px-4 py-3 last:border-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={cn("dot", style.dot)} />
            <span className="text-sm font-medium">{s.code}</span>
            <span className={cn("text-[10px] font-semibold tracking-wider", style.text)}>
              {s.status}
            </span>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-fg-subtle">{s.name}</div>
          <div className="mt-0.5 truncate font-mono text-[10px] text-fg-subtle">
            {s.base_url}
          </div>
        </div>
        <div className="shrink-0 text-right text-[11px] text-fg-muted">
          <div>
            success{" "}
            <span className="tabular">{formatRelative(s.last_success_at)}</span>
          </div>
          {s.consecutive_failures > 0 ? (
            <div className="text-bad">{s.consecutive_failures} consecutive failures</div>
          ) : null}
          <div className="tabular text-fg-subtle">{s.rate_limit_per_minute}/min</div>
        </div>
      </div>
      {s.last_error ? (
        <p className="mt-2 rounded border border-bad/30 bg-bad/5 px-2 py-1 text-[11px] text-bad">
          {s.last_error}
        </p>
      ) : null}
      {s.status === "UNVERIFIED" && s.notes ? (
        <p className="mt-2 rounded border border-border bg-surface-2 px-2 py-1 text-[11px] leading-relaxed text-fg-muted">
          {s.notes}
        </p>
      ) : null}
    </div>
  );
}

export default function DataOperationsPage() {
  const ops = useQuery({
    queryKey: ["data-operations"],
    queryFn: () => api.dataOperations(),
    refetchInterval: 60_000,
  });

  const status = useQuery({
    queryKey: ["market-status"],
    queryFn: () => api.marketStatus(),
    retry: false,
  });

  const data: DataOperations | undefined = ops.data?.data;
  const overall = data ? (FRESHNESS_STYLE[data.overall] ?? FRESHNESS_STYLE.MISSING!) : null;

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-wide">DATA OPERATIONS</h1>
          <p className="mt-1 text-xs text-fg-muted">
            資料管線狀態 · 每 60 秒自動更新 · 本頁不提供任何投資分析
          </p>
        </div>
        {overall ? (
          <span
            className={cn(
              "flex items-center gap-2 text-xs font-semibold tracking-wider",
              overall.text,
            )}
          >
            <span className={cn("dot dot-live", overall.dot)} />
            {overall.label}
          </span>
        ) : null}
      </header>

      {status.isError ? (
        <Card className="mb-6 border-warn/30 bg-warn/5">
          <CardBody>
            <p className="text-sm font-medium text-warn">尚未載入市場資料</p>
            <p className="mt-1 text-xs leading-relaxed text-fg-muted">
              執行 <code className="text-fg">make ingest-calendar</code> 與{" "}
              <code className="text-fg">make ingest-daily</code> 之後，此頁會顯示實際的資料覆蓋狀態。
            </p>
          </CardBody>
        </Card>
      ) : null}

      {status.data ? (
        <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { label: "LAST TRADING DAY", value: status.data.data.last_trading_date ?? "—" },
            {
              label: "SYMBOLS",
              value: (status.data.data.symbol_count ?? 0).toLocaleString(),
            },
            {
              label: "PRICE ROWS",
              value: (status.data.data.price_row_count ?? 0).toLocaleString(),
            },
            {
              label: "COVERAGE",
              value: status.data.data.coverage?.from
                ? `${status.data.data.coverage.from} → ${status.data.data.coverage.to}`
                : "—",
            },
          ].map((tile) => (
            <Card key={tile.label}>
              <CardBody>
                <p className="text-[10px] font-semibold tracking-widest text-fg-subtle">
                  {tile.label}
                </p>
                <p className="tabular mt-2 text-sm font-medium">{tile.value}</p>
              </CardBody>
            </Card>
          ))}
        </div>
      ) : null}

      <Card className="mb-6">
        <CardHeader
          title="Datasets"
          subtitle="新鮮度以交易日曆為準，不是以牆上時鐘 — 週末與連假不會誤報為過期"
        />
        <div>
          <div className="grid grid-cols-[1.4fr_0.9fr_0.9fr_0.8fr_0.7fr_0.7fr] gap-3 border-b border-border px-4 py-2 text-[10px] font-semibold tracking-widest text-fg-subtle">
            <span>DATASET</span>
            <span>STATUS</span>
            <span>LAST DATA</span>
            <span>INGESTED</span>
            <span>RECORDS</span>
            <span>QUARANTINE</span>
          </div>
          {ops.isLoading ? (
            <p className="px-4 py-6 text-sm text-fg-muted">Loading…</p>
          ) : data?.datasets.length ? (
            data.datasets.map((d) => <DatasetRow key={d.dataset} d={d} />)
          ) : (
            <p className="px-4 py-6 text-sm text-fg-subtle">無資料 — 尚未執行 ingestion</p>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Sources"
          subtitle="UNVERIFIED 代表尚未實測成功 — registry 不會宣稱未驗證的能力"
          right={
            data ? (
              <span className="text-[11px] text-fg-subtle">
                {data.sources.filter((s) => s.status === "ACTIVE").length} active /{" "}
                {data.sources.length}
              </span>
            ) : null
          }
        />
        <div>
          {data?.sources.map((s) => <SourceRow key={s.code} s={s} />) ?? (
            <p className="px-4 py-6 text-sm text-fg-muted">Loading…</p>
          )}
        </div>
      </Card>

      <footer className="mt-8 border-t border-border pt-4">
        <DataProvenance
          meta={{
            data_timestamp: ops.data?.meta.generated_at ?? null,
            source: ["SELF"],
            is_demo: false,
            is_stale: false,
          }}
        />
        <p className="mt-2 text-[11px] leading-relaxed text-fg-subtle">
          資料來源：臺灣證券交易所（依政府資料開放授權條款第 1 版）。
          本系統為研究工具，不構成投資建議。
        </p>
      </footer>
    </div>
  );
}
