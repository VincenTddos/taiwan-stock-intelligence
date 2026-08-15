import type { Meta } from "@/lib/api/client";

/**
 * Renders where a number came from.
 *
 * This component is the UI half of the platform rule that no value is shown
 * without its timestamp, source and model version. It is intentionally small
 * and intentionally hard to omit: every panel that displays data mounts it.
 */
export function DataProvenance({ meta, className }: { meta: Partial<Meta>; className?: string }) {
  const parts: string[] = [];
  if (meta.data_timestamp) parts.push(`資料時間 ${new Date(meta.data_timestamp).toLocaleString("zh-TW")}`);
  if (meta.source?.length) parts.push(`來源 ${meta.source.join(", ")}`);
  if (meta.model_version) parts.push(`模型 ${meta.model_version}`);
  if (meta.confidence != null) parts.push(`信心 ${(meta.confidence * 100).toFixed(0)}%`);
  if (meta.cache?.hit) parts.push(`快取 ${meta.cache.age_seconds ?? 0}s`);

  return (
    <div className={className}>
      {meta.is_demo ? (
        <span className="mr-2 rounded border border-bad px-1.5 py-0.5 text-[10px] font-bold tracking-widest text-bad">
          DEMO DATA
        </span>
      ) : null}
      {meta.is_stale ? (
        <span className="mr-2 rounded border border-warn px-1.5 py-0.5 text-[10px] font-bold tracking-widest text-warn">
          STALE
        </span>
      ) : null}
      <span className="text-[11px] text-fg-subtle tabular">{parts.join(" · ") || "—"}</span>
    </div>
  );
}
