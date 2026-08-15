"use client";

import { cn } from "@/lib/utils";
import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "ghost";

const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-white hover:bg-accent/85 disabled:bg-accent/40",
  ghost: "border border-border bg-surface-2 text-fg hover:border-fg-subtle disabled:opacity-50",
};

export function Button({
  variant = "primary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium",
        "transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent",
        "disabled:cursor-not-allowed",
        VARIANT[variant],
        className,
      )}
    />
  );
}
