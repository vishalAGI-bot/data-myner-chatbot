// Client for the analyst Settings endpoints (backend/analyst/api.py).
// Passwords are write-only: the server never sends them back, only whether one is saved.

import { ANALYST_API } from "./analyst";

export type SourceKind = "postgres" | "snapshot";
export type LlmProvider = "ollama" | "openai";

export interface SettingsValues {
  source: SourceKind;
  snapshot_dir: string;
  ssh_host: string;
  ssh_port: number;
  ssh_username: string;
  ssh_password: string;
  ssh_pkey: string;
  ssh_host_key_sha256: string;
  db_host: string;
  db_port: number;
  db_name: string;
  db_username: string;
  db_password: string;
  db_sslmode: string;
  db_schema: string;
  statement_timeout_ms: number;
  max_rows: number;
  allow_free_sql: boolean;
  home_app: string;
  default_window_hours: number;
  timezone: string;
  refresh_seconds: number;
  llm_provider: LlmProvider;
  llm_host: string;
  llm_model: string;
  openai_api_key: string;
  openai_model: string;
}

export interface SettingsView {
  values: SettingsValues;
  secrets_set: { ssh_password: boolean; db_password: boolean; openai_api_key: boolean };
  from_ui: string[];
  settings_file: string;
  saved: boolean;                       // settings were saved from this page (the file exists)
  admin: "local" | "token";
  token_required: boolean;
  active: { kind: string; database?: string; via_ssh?: boolean } | null;
  error: string | null;
}

export interface ConnectionReport {
  ok: boolean;
  error?: string;
  invalid?: boolean;                    // the form has missing / wrong fields (nothing was tried)
  kind?: string;
  database?: string;
  schema?: string;
  via_ssh?: boolean;
  ssh_host_key?: string;
  server_version?: string;
  user?: string;
  read_only_session?: boolean;
  connect_ms?: number;
  ping_ms?: number;
  tables_found?: number;
  tables_missing?: string[];
  metrics_ready?: number;
  metrics_total?: number;
  apps?: string[];
  apps_total?: number;
  date_range?: [string | null, string | null];
  row_counts?: Record<string, number | null>;
  warnings?: string[];
  saved?: boolean;
  settings?: SettingsView;
}

export interface LlmReport {
  ok: boolean;
  latency_ms?: number;
  models?: string[];
  error?: string | null;
}

export class SettingsApiError extends Error {
  constructor(message: string, public status: number, public fields: Record<string, string> = {}) {
    super(message);
  }
}

const TOKEN_KEY = "datamyner:admin-token";

export function getAdminToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setAdminToken(token: string): void {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: the token is simply asked for again */
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getAdminToken();
  if (token) headers["X-Admin-Token"] = token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${ANALYST_API}${path}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    if (detail && typeof detail === "object") {
      throw new SettingsApiError(detail.message ?? "Please check the settings.", res.status, detail.errors ?? {});
    }
    throw new SettingsApiError(typeof detail === "string" ? detail : `Request failed (HTTP ${res.status})`, res.status);
  }
  return data as T;
}

export const getSettings = () => call<SettingsView>("GET", "/api/settings");

export const testSettings = (values: Partial<SettingsValues> & Record<string, unknown>) =>
  call<ConnectionReport>("POST", "/api/settings/test", { values });

export const saveSettings = (values: Partial<SettingsValues> & Record<string, unknown>, force = false) =>
  call<ConnectionReport>("PUT", "/api/settings", { values, force });

export const resetSettings = () => call<ConnectionReport>("DELETE", "/api/settings");

export const testLlm = (values: Pick<SettingsValues,
  "llm_provider" | "llm_host" | "llm_model" | "openai_api_key" | "openai_model">) =>
  call<LlmReport>("POST", "/api/llm/test", values);
