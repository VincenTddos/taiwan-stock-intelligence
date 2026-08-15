"use client";

import { create } from "zustand";
import { api, ApiError, tokenStore, type CurrentUser } from "@/lib/api/client";

interface AuthState {
  user: CurrentUser | null;
  status: "idle" | "loading" | "authenticated" | "anonymous";
  error: string | null;
  login: (email: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  restore: () => Promise<void>;
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  status: "idle",
  error: null,

  async login(email, password) {
    set({ status: "loading", error: null });
    try {
      const { data } = await api.login(email, password);
      tokenStore.set(data.access_token, data.refresh_token);
      const me = await api.me();
      set({ user: me.data, status: "authenticated", error: null });
      return true;
    } catch (err) {
      const message =
        err instanceof ApiError ? err.problem.detail : "Unable to reach the API";
      set({ status: "anonymous", error: message, user: null });
      return false;
    }
  },

  async logout() {
    const refresh = tokenStore.refresh;
    if (refresh) {
      try {
        await api.logout(refresh);
      } catch {
        // Logging out locally must succeed even if the server call fails.
      }
    }
    tokenStore.clear();
    set({ user: null, status: "anonymous", error: null });
  },

  async restore() {
    if (!tokenStore.access) {
      set({ status: "anonymous" });
      return;
    }
    try {
      const me = await api.me();
      set({ user: me.data, status: "authenticated" });
    } catch {
      tokenStore.clear();
      set({ user: null, status: "anonymous" });
    }
  },
}));
