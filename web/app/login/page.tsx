"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { useAuth } from "@/stores/auth";

export default function LoginPage() {
  const router = useRouter();
  const { login, status, error, restore } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    void restore();
  }, [restore]);

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (await login(email, password)) router.replace("/dashboard");
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8">
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-xl font-bold tracking-tight">twquant</span>
            <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-medium tracking-wider text-fg-muted">
              PHASE 1
            </span>
          </div>
          <p className="text-sm text-fg-muted">AI Taiwan Stock Intelligence Platform</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4 rounded-lg border border-border bg-surface p-5">
          <div>
            <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-fg-muted">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>

          <div>
            <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-fg-muted">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              minLength={8}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>

          {error ? (
            <p role="alert" className="rounded border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          ) : null}

          <Button type="submit" disabled={status === "loading"} className="w-full">
            {status === "loading" ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-4 text-center text-[11px] leading-relaxed text-fg-subtle">
          此部署為 Phase 1 基礎建設，尚未載入任何市場資料。
          <br />
          系統不提供投資建議。
        </p>
      </div>
    </main>
  );
}
