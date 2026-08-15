"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { DataProvenance } from "@/components/ui/DataProvenance";
import { StatusDot, type Status } from "@/components/ui/StatusDot";
import { api } from "@/lib/api/client";
import { useAuth } from "@/stores/auth";

/**
 * Dashboard shell.
 *
 * The panels that will hold market data are present as *empty states*, not as
 * placeholder numbers. Phase 1 forbids fabricated market information, and a
 * mocked-up chart is exactly the kind of thing that survives into a demo and
 * gets mistaken for real output.
 */
const PLANNED = [
  { title: "Market Overview", phase: "Phase 2", note: "指數、成交量、市場廣度" },
  { title: "AI Score Ranking", phase: "Phase 5", note: "全市場排行與分數變化" },
  { title: "Sector Heatmap", phase: "Phase 5", note: "產業強度與輪動" },
  { title: "Anomaly Radar", phase: "Phase 5", note: "量價、波動、新聞異常" },
  { title: "Breaking News", phase: "Phase 4", note: "新聞情報與關聯個股" },
  { title: "Institutional Flow", phase: "Phase 2", note: "三大法人買賣超" },
];

export default function DashboardPage() {
  const { user } = useAuth();

  const health = useQuery({
    queryKey: ["health", "full"],
    queryFn: () => api.healthFull(),
    refetchInterval: 30_000,
  });

  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
  });

  const report = health.data?.data;

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-6">
        <h1 className="text-lg font-semibold tracking-wide">DASHBOARD</h1>
        <p className="mt-1 text-xs text-fg-muted">
          {user?.display_name ?? user?.email} · {capabilities.data?.data.phase ?? ""}
        </p>
      </header>

      <Card className="mb-6 border-warn/30 bg-warn/5">
        <CardBody>
          <p className="text-sm font-medium text-warn">Phase 1 — Foundation</p>
          <p className="mt-1 text-xs leading-relaxed text-fg-muted">
            此部署尚未載入任何市場資料。以下面板為版面骨架，
            <strong className="text-fg"> 不含任何模擬或示意數字</strong>
            —— 依開發原則，系統不會在沒有真實資料來源的情況下產生數值。
          </p>
        </CardBody>
      </Card>

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardBody>
            <p className="text-[10px] font-semibold tracking-widest text-fg-subtle">SYSTEM</p>
            <div className="mt-2 flex items-center gap-2">
              <StatusDot status={(report?.status as Status) ?? "unknown"} live />
              <span className="text-sm font-medium">{report?.status ?? "checking…"}</span>
            </div>
            <Link href="/health" className="mt-2 inline-block text-[11px] text-accent hover:underline">
              View details →
            </Link>
          </CardBody>
        </Card>

        <Card>
          <CardBody>
            <p className="text-[10px] font-semibold tracking-widest text-fg-subtle">ENVIRONMENT</p>
            <p className="mt-2 text-sm font-medium">{report?.environment ?? "—"}</p>
            <p className="mt-2 text-[11px] text-fg-subtle tabular">v{report?.version ?? "—"}</p>
          </CardBody>
        </Card>

        <Card>
          <CardBody>
            <p className="text-[10px] font-semibold tracking-widest text-fg-subtle">COMPONENTS</p>
            <p className="mt-2 text-sm font-medium tabular">
              {report ? `${report.components.filter((c) => c.status === "healthy").length}/${report.components.length}` : "—"}
              <span className="ml-1 text-xs font-normal text-fg-muted">healthy</span>
            </p>
          </CardBody>
        </Card>

        <Card>
          <CardBody>
            <p className="text-[10px] font-semibold tracking-widest text-fg-subtle">LLM</p>
            <p className="mt-2 text-sm font-medium">
              {capabilities.data?.data.llm_enabled ? "enabled" : "disabled"}
            </p>
            <p className="mt-2 text-[11px] leading-tight text-fg-subtle">
              核心功能不依賴 LLM
            </p>
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {PLANNED.map((panel) => (
          <Card key={panel.title} className="border-dashed">
            <CardHeader
              title={panel.title}
              subtitle={panel.note}
              right={
                <span className="rounded border border-border px-1.5 py-0.5 text-[9px] font-medium tracking-wider text-fg-subtle">
                  {panel.phase}
                </span>
              }
            />
            <CardBody className="flex h-28 items-center justify-center">
              <p className="text-center text-xs leading-relaxed text-fg-subtle">
                無資料
                <br />
                <span className="text-[10px]">尚未接入資料來源</span>
              </p>
            </CardBody>
          </Card>
        ))}
      </div>

      <footer className="mt-8 border-t border-border pt-4">
        <DataProvenance
          meta={{
            data_timestamp: report?.checked_at ?? null,
            source: ["SELF"],
            is_demo: false,
            is_stale: false,
          }}
        />
        <p className="mt-2 text-[11px] leading-relaxed text-fg-subtle">
          本系統為研究工具，所有輸出（含未來的模型分數與預測）均為推論結果，不構成投資建議。
        </p>
      </footer>
    </div>
  );
}
