"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { StatusDot, StatusLabel, type Status } from "@/components/ui/StatusDot";
import { api, type ComponentHealth } from "@/lib/api/client";
import { formatMs, formatRelative } from "@/lib/utils";
import { useAuth } from "@/stores/auth";

const DISPLAY_NAME: Record<string, string> = {
  api: "API",
  postgres: "DATABASE",
  timescaledb: "TIMESCALEDB",
  pgvector: "PGVECTOR",
  redis: "REDIS",
  celery: "CELERY",
  llm: "LLM (OPTIONAL)",
};

/** Fixed order, so the list does not reshuffle between polls. */
const ORDER = ["api", "postgres", "timescaledb", "pgvector", "redis", "celery", "llm"];

function sortComponents(components: ComponentHealth[]): ComponentHealth[] {
  return [...components].sort(
    (a, b) => (ORDER.indexOf(a.name) + 1 || 99) - (ORDER.indexOf(b.name) + 1 || 99),
  );
}

function ComponentRow({ c }: { c: ComponentHealth }) {
  const [open, setOpen] = useState(false);
  const hasDetail = c.error || Object.keys(c.detail ?? {}).length > 0;

  return (
    <div className="border-b border-border last:border-0">
      <div
        className={hasDetail ? "cursor-pointer hover:bg-surface-2" : ""}
        onClick={() => hasDetail && setOpen((v) => !v)}
      >
        <div className="grid grid-cols-[1.6fr_1fr_0.8fr_1fr_auto] items-center gap-3 px-4 py-2.5 text-sm">
          <div className="flex items-center gap-2.5">
            <StatusDot status={c.status as Status} live />
            <span className="font-medium tracking-wide">{DISPLAY_NAME[c.name] ?? c.name.toUpperCase()}</span>
          </div>
          <span
            className={
              c.status === "healthy"
                ? "text-xs font-semibold tracking-wider text-ok"
                : c.status === "degraded"
                  ? "text-xs font-semibold tracking-wider text-warn"
                  : c.status === "disabled"
                    ? "text-xs font-semibold tracking-wider text-fg-subtle"
                    : "text-xs font-semibold tracking-wider text-bad"
            }
          >
            {c.status.toUpperCase()}
          </span>
          <span className="tabular text-xs text-fg-muted">{formatMs(c.latency_ms)}</span>
          <span className="truncate text-xs text-fg-muted" title={c.version ?? ""}>
            {c.version ?? "—"}
          </span>
          <span className="tabular text-xs text-fg-subtle">{formatRelative(c.checked_at)}</span>
        </div>
      </div>

      {open && hasDetail ? (
        <div className="bg-surface-2 px-4 py-3">
          {c.error ? (
            <p className="mb-2 rounded border border-bad/40 bg-bad/10 px-2 py-1.5 text-xs text-bad">
              {c.error}
            </p>
          ) : null}
          <pre className="overflow-x-auto text-[11px] leading-relaxed text-fg-muted">
            {JSON.stringify(c.detail, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

export default function HealthPage() {
  const { user } = useAuth();
  const [echo, setEcho] = useState<string | null>(null);

  const health = useQuery({
    queryKey: ["health", "full"],
    queryFn: () => api.healthFull(),
    refetchInterval: 15_000,
  });

  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
  });

  const roundTrip = useMutation({
    mutationFn: () => api.workerEcho("health-dashboard"),
    onSuccess: (res) => setEcho(JSON.stringify(res.data, null, 2)),
    onError: (err: Error) => setEcho(`Failed: ${err.message}`),
  });

  const report = health.data?.data;

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-wide">SYSTEM HEALTH</h1>
          <p className="mt-1 text-xs text-fg-muted">
            {report ? (
              <>
                {report.app} v{report.version} · {report.environment} · 每 15 秒自動更新
              </>
            ) : (
              "connecting…"
            )}
          </p>
        </div>
        {report ? <StatusLabel status={report.status as Status} /> : null}
      </header>

      <Card className="mb-6">
        <CardHeader
          title="Components"
          subtitle="每個元件獨立檢查；可選元件停用不會使系統降級"
          right={
            <span className="tabular text-[11px] text-fg-subtle">
              {report ? `checked ${formatRelative(report.checked_at)}` : ""}
            </span>
          }
        />
        <div>
          <div className="grid grid-cols-[1.6fr_1fr_0.8fr_1fr_auto] gap-3 border-b border-border px-4 py-2 text-[10px] font-semibold tracking-widest text-fg-subtle">
            <span>COMPONENT</span>
            <span>STATUS</span>
            <span>LATENCY</span>
            <span>VERSION</span>
            <span>CHECKED</span>
          </div>
          {health.isLoading ? (
            <p className="px-4 py-6 text-sm text-fg-muted">Loading…</p>
          ) : health.isError ? (
            <p className="px-4 py-6 text-sm text-bad">無法連線至 API</p>
          ) : (
            sortComponents(report?.components ?? []).map((c) => <ComponentRow key={c.name} c={c} />)
          )}
        </div>
      </Card>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader title="End-to-end worker round trip" subtitle="API → Redis → Celery → Task" />
          <CardBody>
            <p className="mb-3 text-xs leading-relaxed text-fg-muted">
              派送一個 <code className="text-fg">health_check_task</code> 到 broker，由 worker
              執行並回報它自己看到的 Postgres / Redis 狀態。這證明整條非同步路徑是通的，而不只是
              broker 可連線。
            </p>
            <Button
              onClick={() => roundTrip.mutate()}
              disabled={roundTrip.isPending || user?.role !== "admin"}
              variant="ghost"
            >
              {roundTrip.isPending ? "Dispatching…" : "Run round trip"}
            </Button>
            {user?.role !== "admin" ? (
              <p className="mt-2 text-[11px] text-fg-subtle">需要 admin 權限。</p>
            ) : null}
            {echo ? (
              <pre className="mt-3 max-h-64 overflow-auto rounded border border-border bg-surface-2 p-3 text-[11px] leading-relaxed text-fg-muted">
                {echo}
              </pre>
            ) : null}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Deployment capabilities" subtitle="這個部署現在真正能做什麼" />
          <CardBody>
            {capabilities.data ? (
              <>
                <dl className="mb-3 space-y-1.5 text-xs">
                  {Object.entries(capabilities.data.data.features).map(([name, on]) => (
                    <div key={name} className="flex items-center justify-between">
                      <dt className="text-fg-muted">{name}</dt>
                      <dd className={on ? "text-ok" : "text-fg-subtle"}>
                        {on ? "enabled" : "not implemented"}
                      </dd>
                    </div>
                  ))}
                </dl>
                <p className="rounded border border-border bg-surface-2 px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
                  {capabilities.data.data.note}
                </p>
              </>
            ) : (
              <p className="text-sm text-fg-muted">Loading…</p>
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
