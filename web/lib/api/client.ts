/**
 * API client.
 *
 * Two properties matter here:
 *
 * 1. Every response is the backend's envelope `{ data, meta }`. The client
 *    returns both, never just `data`, so a caller can't accidentally render a
 *    number without access to its timestamp, source and demo flag.
 *
 * 2. Errors are RFC 9457 Problem Details. `ApiError` preserves the whole
 *    problem document including `request_id`, so a user-visible failure can be
 *    matched to a log line.
 */

export interface Meta {
  data_timestamp: string | null;
  trading_date: string | null;
  source: string[];
  model_version: string | null;
  feature_version: string | null;
  dataset_version: string | null;
  calculated_at: string | null;
  data_as_of: string | null;
  confidence: number | null;
  is_demo: boolean;
  is_stale: boolean;
  quality: Record<string, number | null> | null;
  cache: { hit: boolean; age_seconds: number | null };
  request_id: string | null;
  generated_at: string;
}

export interface Envelope<T> {
  data: T;
  meta: Meta;
  pagination?: unknown;
}

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  request_id?: string;
  errors?: { field: string; message: string }[];
}

export class ApiError extends Error {
  constructor(
    readonly problem: Problem,
    readonly status: number,
  ) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
  }
}

const ACCESS_KEY = "twq.access";
const REFRESH_KEY = "twq.refresh";

export const tokenStore = {
  get access() {
    return typeof window === "undefined" ? null : window.sessionStorage.getItem(ACCESS_KEY);
  },
  get refresh() {
    return typeof window === "undefined" ? null : window.sessionStorage.getItem(REFRESH_KEY);
  },
  set(access: string, refresh: string) {
    window.sessionStorage.setItem(ACCESS_KEY, access);
    window.sessionStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    window.sessionStorage.removeItem(ACCESS_KEY);
    window.sessionStorage.removeItem(REFRESH_KEY);
  },
};

const BASE = "/api/v1";

async function parseProblem(res: Response): Promise<Problem> {
  try {
    return (await res.json()) as Problem;
  } catch {
    return { type: "about:blank", title: res.statusText, status: res.status, detail: res.statusText };
  }
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  auth?: boolean;
  /** Internal: prevents infinite refresh recursion. */
  _retried?: boolean;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const { body, auth = true, _retried = false, ...init } = opts;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (body !== undefined) headers.set("Content-Type", "application/json");

  const token = auth ? tokenStore.access : null;
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // A 401 on an authenticated call means the short-lived access token expired.
  // Refresh once, then replay. If the refresh also fails the session is over.
  if (res.status === 401 && auth && !_retried && tokenStore.refresh) {
    const refreshed = await tryRefresh();
    if (refreshed) return request<T>(path, { ...opts, _retried: true });
    tokenStore.clear();
  }

  if (res.status === 204) return { data: undefined as T, meta: {} as Meta };

  if (!res.ok) {
    // Health endpoints answer 503 with a full, useful body — that is a valid
    // report, not a transport failure, so it is returned rather than thrown.
    if (res.status === 503 && path.startsWith("/health")) {
      return (await res.json()) as Envelope<T>;
    }
    throw new ApiError(await parseProblem(res), res.status);
  }

  return (await res.json()) as Envelope<T>;
}

async function tryRefresh(): Promise<boolean> {
  const refresh = tokenStore.refresh;
  if (!refresh) return false;
  try {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) return false;
    const payload = (await res.json()) as Envelope<{ access_token: string; refresh_token: string }>;
    tokenStore.set(payload.data.access_token, payload.data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- endpoints
export interface ComponentHealth {
  name: string;
  status: "healthy" | "degraded" | "unhealthy" | "disabled" | "unknown";
  latency_ms: number | null;
  version: string | null;
  checked_at: string;
  detail: Record<string, unknown>;
  error: string | null;
}

export interface HealthReport {
  status: ComponentHealth["status"];
  app: string;
  version: string;
  environment: string;
  checked_at: string;
  components: ComponentHealth[];
}

export interface Capabilities {
  environment: string;
  version: string;
  phase: string;
  features: Record<string, boolean>;
  llm_enabled: boolean;
  mock_data_allowed: boolean;
  note: string;
}

export interface DatasetHealth {
  dataset: string;
  description: string | null;
  status: "FRESH" | "STALE" | "MISSING" | "DEGRADED";
  last_data_date: string | null;
  last_ingested_at: string | null;
  expected_next_update: string | null;
  record_count: number;
  lag_minutes: number | null;
  expected_lag_minutes: number;
  quarantined: number;
  detail: Record<string, unknown> | null;
}

export interface SourceHealth {
  code: string;
  name: string;
  status: "ACTIVE" | "DEGRADED" | "UNVERIFIED" | "DISABLED";
  market: string | null;
  base_url: string;
  verified_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
  rate_limit_per_minute: number;
  notes: string | null;
}

export interface DataOperations {
  overall: "FRESH" | "STALE" | "MISSING" | "DEGRADED";
  datasets: DatasetHealth[];
  sources: SourceHealth[];
  quarantine_total: number;
}

export interface MarketStatus {
  market: string;
  last_trading_date: string | null;
  next_trading_date: string | null;
  is_trading_day_today: boolean | null;
  session_type_today: string | null;
  symbol_count: number | null;
  price_row_count: number | null;
  coverage: { from: string | null; to: string | null };
  freshness: string;
  is_stale: boolean;
  updated_at: string;
}

export interface CurrentUser {
  id: number;
  email: string;
  display_name: string | null;
  role: string;
  is_active: boolean;
  last_login_at: string | null;
}

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; refresh_token: string; expires_at: string }>("/auth/login", {
      method: "POST",
      body: { email, password },
      auth: false,
    }),
  logout: (refresh_token: string) =>
    request<void>("/auth/logout", { method: "POST", body: { refresh_token } }),
  me: () => request<CurrentUser>("/auth/me"),
  healthFull: () => request<HealthReport>("/health/full", { auth: false }),
  healthDatabase: () => request<ComponentHealth[]>("/health/database", { auth: false }),
  capabilities: () => request<Capabilities>("/meta/capabilities", { auth: false }),
  dataOperations: () => request<DataOperations>("/market/data-operations", { auth: false }),
  marketStatus: () => request<MarketStatus>("/market/status", { auth: false }),
  workerEcho: (message: string) =>
    request<{ dispatched: boolean; task_id: string; completed: boolean; result?: unknown; error?: string }>(
      `/health/worker/echo?message=${encodeURIComponent(message)}`,
      { method: "POST" },
    ),
};
