// Client for the journey agent API (backend/journey_agent/api.py).

export const JOURNEY_API: string = import.meta.env.VITE_JOURNEY_API ?? "/journey-api";
// A question may ask for up to 20 minutes (the backend enforces the same ceiling).
export const MAX_THINK_SECONDS = 1200;
// What a new browser starts with: most questions finish in seconds, so a long budget
// is something the user chooses, not something they are given silently.
export const DEFAULT_THINK_SECONDS = 60;
// Slider stops. A linear 10-1200 s range would make every useful short value a pixel
// apart, so the slider moves over these steps instead.
export const THINK_CHOICES = [10, 20, 30, 45, 60, 90, 120, 180, 300, 450, 600, 900, 1200];

/** "45s" / "2 min" / "20 min" - the same wording the backend uses. */
export function formatThinkTime(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  return `${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)} min`;
}

/** The nearest allowed stop, so a stored or typed value always lands on the slider. */
export function nearestThinkChoice(seconds: number): number {
  const wanted = Math.min(Math.max(seconds || DEFAULT_THINK_SECONDS, THINK_CHOICES[0]), MAX_THINK_SECONDS);
  return THINK_CHOICES.reduce((best, c) => (Math.abs(c - wanted) < Math.abs(best - wanted) ? c : best),
    THINK_CHOICES[0]);
}

export type Depth = "auto" | "fast" | "deep";

export interface ThinkingStep {
  stage: string;              // understand | plan | query | verify | write | stop
  text: string;
  elapsed_ms: number;
  ok: boolean;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ResultTable {
  columns: string[];
  rows: unknown[][];
  total_rows: number;
  truncated: boolean;
}

export interface JourneyResponse {
  answer: string;
  route: string;
  depth: "fast" | "deep";
  resolved_question: string;
  sql: string | null;
  table: ResultTable | null;
  notes: string[];
  thinking: ThinkingStep[];
  parts: { question: string; understood_as: string; answer: string; verified: boolean }[];
  verified: boolean;
  timed_out: boolean;
  suggested_questions: string[];
  latency_ms: number;
  request_id: string;
}

export interface JourneySource {
  kind: "postgres" | string;
  database?: string | null;
  schema?: string | null;
  table?: string | null;
  via_ssh?: boolean;
  read_only?: boolean;
  error?: string | null;
  filter_mode?: "latest_month" | "date_range" | "last_24_hours";
  date_from?: string | null;
  date_to?: string | null;
  available_first_date?: string | null;
  available_last_date?: string | null;
  available_last_update?: string | null;
}

export interface JourneyFilter {
  mode: "latest_month" | "date_range" | "last_24_hours";
  date_from?: string;
  date_to?: string;
}

export interface JourneyHealth {
  status: string;
  data_ready: boolean;
  // Kept for compatibility while older journey backends are restarted.
  data_loaded?: boolean;
  source: JourneySource;
  db_ping_ms?: number | null;
  connected_since?: number | null;
  refreshed_at?: number | null;
  latest_date?: string | null;
  llm_ok: boolean;
  llm?: string;
  llm_provider?: "ollama" | "openai" | string;
  llm_model?: string;
  ollama?: string;
  stale?: boolean;
  refresh_error?: string | null;
}

export interface JourneySummary {
  filename: string | null;
  source: JourneySource;
  journeys: number;
  users: number;
  events: number;
  cross_app: number;
  open_journeys: number;
  closed_journeys: number;
  purchased: number;
  timed_out: number;
  first_date: string | null;
  last_date: string | null;
  apps: string[];
  suggestion_groups?: JourneySuggestionGroup[];
  data_quality?: { records_read: number; records_loaded: number; records_skipped: number; warnings: number; issues: string[] };
}

export interface JourneySuggestionGroup {
  key: string;
  category: string;
  description: string;
  fields: string[];
  questions: string[];
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function detail(res: Response): Promise<string> {
  const body = await res.json().catch(() => null);
  if (body && typeof body.detail === "string") return body.detail;
  if (body && Array.isArray(body.detail)) return body.detail.map((d: { msg?: string }) => d.msg).join("; ");
  return `Request failed (HTTP ${res.status})`;
}

export async function getJourneyHealth(): Promise<JourneyHealth> {
  const res = await fetch(`${JOURNEY_API}/api/health`);
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return res.json();
}

/** Returns null when the live journey source is not ready yet (HTTP 409). */
export async function getJourneySummary(): Promise<JourneySummary | null> {
  const res = await fetch(`${JOURNEY_API}/api/summary`);
  if (res.status === 409) return null;
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return res.json();
}

/** Re-open the saved read-only PostgreSQL source after connection settings change. */
export async function connectJourneyLive(filter?: JourneyFilter): Promise<JourneySummary> {
  const res = await fetch(`${JOURNEY_API}/api/source/live`, {
    method: "POST",
    headers: filter ? { "Content-Type": "application/json" } : undefined,
    body: filter ? JSON.stringify(filter) : undefined,
  });
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return res.json();
}

export function newRequestId(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}${Math.random()}`;
  return random.replace(/[^a-zA-Z0-9]/g, "").slice(0, 16);
}

/**
 * Streams the answer: calls onStep for every live thinking step and resolves
 * with the final response. Uses fetch + ReadableStream because EventSource
 * cannot send a POST body.
 */
export async function streamJourneyChat(
  body: { message: string; history: ChatTurn[]; depth: Depth; max_seconds: number; request_id: string },
  onStep: (step: ThinkingStep) => void,
  signal?: AbortSignal,
): Promise<JourneyResponse> {
  const res = await fetch(`${JOURNEY_API}/api/chat/stream`, {
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
      if (!line) continue; // keep-alive comment
      const event = JSON.parse(line.slice(5));
      if (event.type === "thinking") onStep(event as ThinkingStep);
      else if (event.type === "final") return event as JourneyResponse;
      else if (event.type === "error") throw new ApiError(event.detail ?? "The agent failed.", 500);
    }
  }
  throw new ApiError("The connection closed before an answer arrived.", 0);
}

/** The Stop button: the server returns the best verified answer so far. */
export async function stopJourneyChat(requestId: string): Promise<void> {
  await fetch(`${JOURNEY_API}/api/chat/${encodeURIComponent(requestId)}/stop`, { method: "POST" });
}
