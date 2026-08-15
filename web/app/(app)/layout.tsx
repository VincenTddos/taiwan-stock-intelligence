"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { StatusDot } from "@/components/ui/StatusDot";
import { useAuth } from "@/stores/auth";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";

const NAV = [
  { href: "/dashboard", label: "Dashboard", enabled: true },
  { href: "/health", label: "System Health", enabled: true },
  { href: "#", label: "Market", enabled: false },
  { href: "#", label: "Screener", enabled: false },
  { href: "#", label: "Sectors", enabled: false },
  { href: "#", label: "Supply Chain", enabled: false },
  { href: "#", label: "Portfolio", enabled: false },
  { href: "#", label: "Backtest", enabled: false },
  { href: "#", label: "Copilot", enabled: false },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, status, restore, logout } = useAuth();

  useEffect(() => {
    void restore();
  }, [restore]);

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  // The shell polls overall health so the sidebar lamp is always current;
  // 30s matches the worker heartbeat cadence.
  const { data: health } = useQuery({
    queryKey: ["health", "full"],
    queryFn: () => api.healthFull(),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  if (status !== "authenticated") {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-fg-muted">
        Loading…
      </div>
    );
  }

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 flex-col border-r border-border bg-surface">
        <div className="border-b border-border px-4 py-4">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-bold tracking-tight">twquant</span>
            <span className="rounded border border-border px-1 py-0.5 text-[9px] font-medium tracking-wider text-fg-muted">
              P1
            </span>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 p-2">
          {NAV.map((item) => {
            const active = pathname === item.href;
            if (!item.enabled) {
              return (
                <div
                  key={item.label}
                  title="尚未實作 — 依 Phase 2+ 開放"
                  className="flex cursor-not-allowed items-center justify-between rounded px-3 py-2 text-sm text-fg-subtle/50"
                >
                  {item.label}
                  <span className="text-[9px] tracking-wider">SOON</span>
                </div>
              );
            }
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "block rounded px-3 py-2 text-sm transition-colors",
                  active ? "bg-surface-2 text-fg" : "text-fg-muted hover:bg-surface-2 hover:text-fg",
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="border-t border-border p-3">
          <Link
            href="/health"
            className="mb-3 flex items-center gap-2 text-xs text-fg-muted hover:text-fg"
          >
            <StatusDot status={health?.data.status ?? "unknown"} live />
            <span>{health?.data.status ?? "checking…"}</span>
          </Link>
          <div className="mb-2 truncate text-xs text-fg-muted" title={user?.email}>
            {user?.display_name ?? user?.email}
            <span className="ml-1 text-fg-subtle">({user?.role})</span>
          </div>
          <button
            onClick={() => void logout().then(() => router.replace("/login"))}
            className="text-xs text-fg-subtle transition-colors hover:text-bad"
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="flex-1 overflow-x-hidden">{children}</main>
    </div>
  );
}
