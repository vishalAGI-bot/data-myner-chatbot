// Client for the Data analyst API (backend/analyst/api.py): every database table.

import { ApiError, ChatTurn, Depth, MAX_THINK_SECONDS, ResultTable, ThinkingStep } from "./journey";

export const ANALYST_API: string = import.meta.env.VITE_ANALYST_API ?? "/analyst-api";

export interface AnalystSource {
  kind: "postgres" | "snapshot" | string;
  database?: string;
  schema?: string;
  via_ssh?: boolean;
  read_only?: boolean;
  error?: string;
}

export interface AnalystCatalog {
  tables: { name: string; title: string; description: string; rows: number | null; status: string; domain: string; columns: number }[];
  unavailable_tables: string[];
  metrics: { name: string; title: string; description: string; ready?: boolean; issue?: string | null }[];
  metrics_ready?: number;
  blocked_tables: number;
  apps: string[];
  date_range: [string | null, string | null];
  home_app: string;
  source: AnalystSource;
  total_rows?: number;
  profile?: Record<string, number>;
  quality_issues?: {
    code: string;
    severity: "error" | "warning" | string;
    title: string;
    detail: string;
    tables: string[];
    affected_metrics: string[];
  }[];
}

export interface AnalystPart {
  question: string;
  understood_as: string;
  answer: string;
  sql: string | null;
  table: ResultTable | null;
  verified: boolean;
}

export interface AnalystResponse {
  answer: string;
  route: string;              // metric | proxy | sql | deep | data_gap | clarify | unsupported | out_of_scope | help | error | timeout
  source: string;             // rules | llm
  resolved: string;
  metric: string | null;
  params: Record<string, unknown>;
  tables: string[];
  sql: string | null;
  table: ResultTable | null;
  notes: string[];
  thinking: ThinkingStep[];
  parts: AnalystPart[];
  verified: boolean;
  timed_out: boolean;
  depth: "fast" | "deep";
  latency_ms: number;
  state: Record<string, unknown>;
  suggested_questions: string[];
  request_id: string;
}

async function detail(res: Response): Promise<string> {
  const body = await res.json().catch(() => null);
  if (body && typeof body.detail === "string") return body.detail;
  return `Request failed (HTTP ${res.status})`;
}

export interface AnalystHealth {
  status: string;
  data_ready: boolean;
  source: AnalystSource;
  db_ping_ms?: number | null;        // round trip of SELECT 1 on the live connection
  connected_since?: number | null;   // unix seconds
  refreshed_at?: number | null;      // when apps / categories / latest date were last re-read
  latest_date?: string | null;
  llm: string;
  llm_ok: boolean;
  llm_provider?: "ollama" | "openai" | string;
  llm_model?: string;
}

export async function getAnalystHealth(): Promise<AnalystHealth> {
  const res = await fetch(`${ANALYST_API}/api/health`);
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return res.json();
}

export async function getAnalystCatalog(): Promise<AnalystCatalog> {
  const res = await fetch(`${ANALYST_API}/api/catalog`);
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return res.json();
}

export async function streamAnalystChat(
  body: { message: string; history: ChatTurn[]; state: Record<string, unknown>; depth: Depth; max_seconds: number; request_id: string },
  onStep: (step: ThinkingStep) => void,
  signal?: AbortSignal,
): Promise<AnalystResponse> {
  const res = await fetch(`${ANALYST_API}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, max_seconds: Math.min(body.max_seconds, MAX_THINK_SECONDS) }),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError(await detail(res), res.status);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const event = JSON.parse(line.slice(5));
      if (event.type === "thinking") onStep(event as ThinkingStep);
      else if (event.type === "final") return event as AnalystResponse;
      else if (event.type === "error") throw new ApiError(event.detail ?? "The analyst failed.", 500);
    }
  }
  throw new ApiError("The connection closed before an answer arrived.", 0);
}

export async function stopAnalystChat(requestId: string): Promise<void> {
  await fetch(`${ANALYST_API}/api/chat/${encodeURIComponent(requestId)}/stop`, { method: "POST" });
}
