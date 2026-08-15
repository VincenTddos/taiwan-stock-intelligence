import { cn } from "@/lib/utils";

export type Status = "healthy" | "degraded" | "unhealthy" | "disabled" | "unknown";

const COLOR: Record<Status, string> = {
  healthy: "bg-ok",
  degraded: "bg-warn",
  unhealthy: "bg-bad",
  disabled: "bg-off",
  unknown: "bg-off",
};

const LABEL: Record<Status, string> = {
  healthy: "HEALTHY",
  degraded: "DEGRADED",
  unhealthy: "UNHEALTHY",
  disabled: "DISABLED",
  unknown: "UNKNOWN",
};

const TEXT: Record<Status, string> = {
  healthy: "text-ok",
  degraded: "text-warn",
  unhealthy: "text-bad",
  disabled: "text-fg-subtle",
  unknown: "text-fg-subtle",
};

export function StatusDot({ status, live = false }: { status: Status; live?: boolean }) {
  return <span className={cn("dot", COLOR[status], live && status === "healthy" && "dot-live")} />;
}

export function StatusLabel({ status }: { status: Status }) {
  return (
    <span className={cn("flex items-center gap-2 text-xs font-semibold tracking-wider", TEXT[status])}>
      <StatusDot status={status} live />
      {LABEL[status]}
    </span>
  );
}
