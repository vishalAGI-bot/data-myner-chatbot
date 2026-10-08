import { useEffect, useState } from "react";
import type { Depth, JourneyHealth, JourneySource, JourneySummary } from "../api/journey";
import { THINK_CHOICES, formatThinkTime } from "../api/journey";
import type { AnalystCatalog, AnalystHealth } from "../api/analyst";
import { AGENTS, AgentId } from "../types";
import type { Connection } from "../App";

interface Props {
  agent: AgentId;
  onAgent: (a: AgentId) => void;
  open: boolean;
  onClose: () => void;
  summary: JourneySummary | null;
  catalog: AnalystCatalog | null;
  simple: boolean;
  onSimple: (v: boolean) => void;
  depth: Depth;
  onDepth: (d: Depth) => void;
  maxSeconds: number;
  onMaxSeconds: (s: number) => void;
  running: boolean;
  onAsk: (q: string) => void;
  connection: Connection;
  onRetry: () => void;
  health: AnalystHealth | null;
  journeyHealth: JourneyHealth | null;
  onJourneyRange: (dateFrom: string, dateTo: string) => void;
  onJourneyLatest: () => void;
  onJourney24Hours: () => void;
  onSettings: () => void;
  settingsOpen: boolean;
}

const n = (v: number | null | undefined) => (v === null || v === undefined || v < 0 ? "–" : v.toLocaleString());

const DEPTHS: { value: Depth; label: string; hint: string }[] = [
  { value: "auto", label: "Auto", hint: "Thinks deeper only when the question is complex" },
  { value: "fast", label: "Fast", hint: "One exact query, instant" },
  { value: "deep", label: "Deep", hint: "Splits the question, answers each part, verifies the result" },
];

const STATUS: Record<Connection, string> = {
  checking: "Connecting…",
  connected: "Selected agent connected",
  "no-data": "Connected, no data yet",
  offline: "Agent offline, start the backend",
};

export default function Sidebar(p: Props) {
  const [showTables, setShowTables] = useState(false);
  const [catalogView, setCatalogView] = useState<"tables" | "analyses" | "apps">("tables");
  const [catalogQuery, setCatalogQuery] = useState("");
  const info = AGENTS[p.agent];
  const c = p.catalog;
  const query = catalogQuery.trim().toLowerCase();
  const visibleTables = (c?.tables ?? []).filter((t) => !query
    || t.name.toLowerCase().includes(query) || t.title.toLowerCase().includes(query));
  const visibleMetrics = (c?.metrics ?? []).filter((m) => !query
    || m.name.toLowerCase().includes(query) || m.title.toLowerCase().includes(query)
    || m.description.toLowerCase().includes(query));
  const visibleApps = (c?.apps ?? []).filter((app) => !query || app.toLowerCase().includes(query));
  const readyMetrics = c?.metrics_ready ?? c?.metrics.filter((m) => m.ready !== false).length ?? 0;
  const tablesWithData = c?.tables.filter((t) => (t.rows ?? 0) > 0).length ?? 0;
  const journeySource = p.summary?.source ?? p.journeyHealth?.source;
  const [rangeFrom, setRangeFrom] = useState("");
  const [rangeTo, setRangeTo] = useState("");
  useEffect(() => {
    setRangeFrom(journeySource?.date_from ?? p.summary?.first_date ?? "");
    setRangeTo(journeySource?.date_to ?? p.summary?.last_date ?? "");
  }, [journeySource?.date_from, journeySource?.date_to, p.summary?.first_date, p.summary?.last_date]);
  const availableFrom = journeySource?.available_first_date ?? p.summary?.first_date ?? "";
  const availableTo = journeySource?.available_last_date ?? p.summary?.last_date ?? "";
  const maxRangeTo = minDate(availableTo, addDays(rangeFrom, 30));
  const rangeError = validateRange(rangeFrom, rangeTo);

  return (
    <aside className={`sidebar ${p.open ? "open" : ""}`} aria-label="Agents and settings">
      <button type="button" className="close-menu" aria-label="Close menu" onClick={p.onClose}>×</button>
      <div className="brand">
        <div className="brand-logo">D</div>
        <div>
          <div className="brand-name">DataMyner Intelligence</div>
          <div className="brand-sub">AI Help Desk</div>
        </div>
      </div>

      <div className="section-label">Choose an agent</div>
      {(["analyst", "journey"] as AgentId[]).map((id) => (
        <button key={id} type="button" disabled={p.running && p.agent !== id}
                className={`agent-card ${p.agent === id ? "active" : ""}`}
                aria-pressed={p.agent === id} onClick={() => p.onAgent(id)}>
          <span className="agent-letter">{AGENTS[id].letter}</span>
          <span className="agent-text">
            <span className="agent-name">{AGENTS[id].name}</span>
            <span className="agent-blurb">{AGENTS[id].blurb}</span>
          </span>
        </button>
      ))}

      <div className="panel">
        {p.agent === "analyst" ? (
          <>
            <div className="panel-label">Data source</div>
            {c ? (
              <>
                <div className="panel-file">
                  {c.source.kind === "postgres"
                    ? `${c.source.database} · live${c.source.via_ssh ? " via SSH" : ""} · read-only`
                    : `Offline snapshot (${c.source.database})`}
                </div>
                <div className="stats">
                  <Stat value={n(c.tables.length)} label="Queryable" />
                  <Stat value={`${readyMetrics}/${c.metrics.length}`} label="Ready analyses" />
                  <Stat value={n(c.apps.length)} label="Apps" />
                  <Stat value={n(c.blocked_tables)} label="Policy-hidden" />
                </div>
                <div className="panel-lines">
                  {c.date_range[0] && <div>Data {c.date_range[0]} to {p.health?.latest_date ?? c.date_range[1]}</div>}
                  <div>{tablesWithData} tables with data · {c.tables.length - tablesWithData} empty</div>
                  {c.total_rows != null && <div>≈{n(c.total_rows)} rows available; queries aggregate in PostgreSQL</div>}
                  <div>“We / our” = {c.home_app}</div>
                  <button type="button" className="link-btn" onClick={() => setShowTables(!showTables)}
                          aria-expanded={showTables}>
                    {showTables ? "Hide data catalog" : "Browse tables, analyses and apps"}
                  </button>
                </div>
                {(c.quality_issues ?? []).map((issue) => (
                  <details className={`quality-issue ${issue.severity}`} key={issue.code}>
                    <summary>{issue.title}</summary>
                    <p>{issue.detail}</p>
                  </details>
                ))}
                {showTables && (
                  <div className="catalog-browser">
                    <div className="catalog-tabs" role="tablist" aria-label="Data catalog">
                      {(["tables", "analyses", "apps"] as const).map((view) => (
                        <button type="button" role="tab" aria-selected={catalogView === view} key={view}
                                className={catalogView === view ? "on" : ""} onClick={() => setCatalogView(view)}>
                          {view}
                        </button>
                      ))}
                    </div>
                    <input className="catalog-search" value={catalogQuery} onChange={(e) => setCatalogQuery(e.target.value)}
                           placeholder={`Filter ${catalogView}…`} aria-label={`Filter ${catalogView}`} />
                    {catalogView === "tables" && (
                      <ul className="dataset-list">
                        {visibleTables.map((t) => (
                          <li key={t.name} title={t.description} className={(t.rows ?? 0) === 0 ? "empty-row" : ""}>
                            <span className="dataset-kind">{t.domain === "operations" ? "ops" : "data"}</span>
                            <span className="dataset-info">
                              <span className="dataset-name">{t.name}</span>
                              <span className="dataset-title">{t.title} · {t.columns} columns · {t.status}</span>
                            </span>
                            <span className="dataset-rows">{(t.rows ?? 0) === 0 ? "empty" : `≈${n(t.rows)}`}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {catalogView === "analyses" && (
                      <ul className="analysis-list">
                        {visibleMetrics.map((m) => (
                          <li key={m.name} title={m.issue ?? m.description}>
                            <span className={`readiness ${m.ready === false ? "not-ready" : "ready"}`} aria-hidden="true" />
                            <span><strong>{m.title}</strong><small>{m.ready === false ? "Data needed" : "Ready"}</small></span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {catalogView === "apps" && <div className="app-list">
                      {visibleApps.map((app) => <span key={app}>{app}</span>)}
                    </div>}
                  </div>
                )}
              </>
            ) : (
              <div className="panel-empty">
                {p.connection === "offline" ? "The analyst backend is not running."
                  : p.connection === "checking" ? "Connecting…" : "Not connected to a database yet."}
                <button type="button" className="link-btn" onClick={p.onRetry}>Retry</button>
                {p.connection !== "offline" && p.health?.source?.error && (
                  <div className="warn-text panel-error">{p.health.source.error}</div>
                )}
              </div>
            )}
            <button type="button" className={`upload-btn settings-btn ${p.settingsOpen ? "active" : ""}`}
                    disabled={p.running} onClick={p.onSettings}>
              <GearIcon /> {c?.source.kind === "postgres" ? "Connection settings" : "Connect live database"}
            </button>
          </>
        ) : (
          <>
            <div className="panel-label">Journey data source</div>
            {p.summary ? (
              <>
                <div className="panel-file" title={journeySourceTitle(journeySource)}>
                  {journeySourceTitle(journeySource)} · live{journeySource?.via_ssh ? " via SSH" : ""} · read-only
                </div>
                <div className="stats">
                  <Stat value={n(p.summary.journeys)} label="Journeys" />
                  <Stat value={n(p.summary.users)} label="Users" />
                  <Stat value={n(p.summary.events)} label="Events" />
                  <Stat value={n(p.summary.cross_app)} label="Multi-app" />
                </div>
                <div className="panel-lines">
                  <div>{n(p.summary.open_journeys)} open</div>
                  <div>{n(p.summary.purchased)} purchased</div>
                  <div>{n(p.summary.timed_out)} timed out</div>
                  <div><strong>Data coverage:</strong> {dateRange(p.summary)}</div>
                  {availableFrom && availableTo && (
                    <div>Source available: {availableFrom} to {availableTo}</div>
                  )}
                  {journeySource?.available_last_update && (
                    <div>Database last updated: {journeySource.available_last_update}</div>
                  )}
                  <div>Window: {filterLabel(journeySource)}</div>
                </div>
                <div className="journey-range" aria-label="Journey date range">
                  <div className="journey-range-grid">
                    <label>
                      <span>From</span>
                      <input type="date" value={rangeFrom} min={availableFrom} max={availableTo}
                             disabled={p.running} onChange={(e) => setRangeFrom(e.target.value)} />
                    </label>
                    <label>
                      <span>To</span>
                      <input type="date" value={rangeTo} min={rangeFrom || availableFrom}
                             max={maxRangeTo || availableTo} disabled={p.running}
                             onChange={(e) => setRangeTo(e.target.value)} />
                    </label>
                  </div>
                  <div className={rangeError ? "journey-range-error" : "journey-range-hint"}>
                    {rangeError || "Maximum 31 calendar days"}
                  </div>
                  <div className="journey-range-actions">
                    <button type="button" disabled={p.running} onClick={p.onJourneyLatest}>Latest month</button>
                    <button type="button" disabled={p.running} onClick={p.onJourney24Hours}>Last 24h</button>
                    <button type="button" className="primary" disabled={p.running || Boolean(rangeError)}
                            onClick={() => p.onJourneyRange(rangeFrom, rangeTo)}>Apply</button>
                  </div>
                  <button type="button" className="link-btn" disabled={p.running} onClick={p.onRetry}>
                    Refresh current window
                  </button>
                </div>
                {p.journeyHealth?.stale && (
                  <div className="warn-text panel-error" role="status">
                    Live refresh is delayed. Answers use the last successful PostgreSQL refresh.
                    {p.journeyHealth.refresh_error ? ` ${p.journeyHealth.refresh_error}` : ""}
                  </div>
                )}
              </>
            ) : (
              <div className="panel-empty">
                {p.connection === "checking" ? "Connecting to live cross_app_journey…"
                  : p.connection === "offline" ? "The journey service is not running."
                    : journeySource?.error || p.journeyHealth?.refresh_error
                      || "Live PostgreSQL journey data is not ready."}
                <button type="button" className="link-btn" disabled={p.running} onClick={p.onRetry}>Retry</button>
              </div>
            )}
            <button type="button" className={`upload-btn settings-btn ${p.settingsOpen ? "active" : ""}`}
                    disabled={p.running} onClick={p.onSettings}>
              <GearIcon /> {journeySource?.kind === "postgres" ? "Connection settings" : "Connect live database"}
            </button>
          </>
        )}

        <div className="divider" />
        <label className="toggle-row">
          <span>
            <span className="toggle-title">Simple meaning</span>
            <span className="toggle-sub">Explain results as a real-life story</span>
          </span>
          <input type="checkbox" className="switch" checked={p.simple}
                 onChange={(e) => p.onSimple(e.target.checked)} aria-label="Simple meaning" />
        </label>
      </div>

      <div className="panel">
        <div className="toggle-title">Thinking depth</div>
        <div className="toggle-sub">{DEPTHS.find((d) => d.value === p.depth)?.hint}</div>
        <div role="radiogroup" aria-label="Thinking depth" className="seg">
          {DEPTHS.map((d) => (
            <button key={d.value} type="button" role="radio" aria-checked={p.depth === d.value}
                    disabled={p.running} className={p.depth === d.value ? "on" : ""}
                    onClick={() => p.onDepth(d.value)}>{d.label}</button>
          ))}
        </div>
        <label className="limit">
          <span>Max thinking time</span>
          <strong>{formatThinkTime(p.maxSeconds)}</strong>
          {/* The slider moves over THINK_CHOICES by index: a linear 10-1200 s range would
              squeeze every short, commonly used value into the first few pixels. */}
          <input type="range" min={0} max={THINK_CHOICES.length - 1} step={1}
                 value={Math.max(0, THINK_CHOICES.indexOf(p.maxSeconds))}
                 disabled={p.running} aria-valuetext={formatThinkTime(p.maxSeconds)}
                 onChange={(e) => p.onMaxSeconds(THINK_CHOICES[Number(e.target.value)])} />
        </label>
        {p.maxSeconds > 120 && (
          <div className="limit-hint">
            Long questions keep a database query running for up to {formatThinkTime(p.maxSeconds)}.
            Stop returns the parts already verified.
          </div>
        )}
      </div>

      <div className="section-label">Try asking</div>
      {info.suggestions.map((q) => (
        <button key={q} type="button" className="suggestion" disabled={p.running} onClick={() => p.onAsk(q)}>{q}</button>
      ))}

      <div className="footer">By Market Xcel Data Matrix Pvt. Ltd.</div>
      <div className={`status status-${p.connection}`} role="status">
        <span className="status-dot" />
        <span className="status-text">{statusText(p)}</span>
      </div>
    </aside>
  );
}

function statusText(p: Props): string {
  if (p.connection !== "connected") return STATUS[p.connection];
  if (p.agent === "journey") {
    if (p.journeyHealth?.stale) return "Live journey data · refresh delayed";
    return `Live cross_app_journey${p.journeyHealth?.db_ping_ms != null ? ` · ${p.journeyHealth.db_ping_ms} ms` : ""}`;
  }
  if (p.health) {
    const src = p.health.source;
    if (src.kind === "postgres") {
      return `Live database${p.health.db_ping_ms != null ? ` · ${p.health.db_ping_ms} ms` : ""}`;
    }
    return "Connected · offline snapshot";
  }
  return STATUS.connected;
}

function journeySourceTitle(source: JourneySource | undefined): string {
  const table = source?.table || "cross_app_journey";
  const schema = source?.schema ? `${source.schema}.` : "";
  return source?.database ? `${source.database} · ${schema}${table}` : `${schema}${table}`;
}

function dateRange(summary: JourneySummary): string {
  if (summary.first_date && summary.last_date) return `${summary.first_date} to ${summary.last_date}`;
  return summary.first_date ?? summary.last_date ?? "not available";
}

function filterLabel(source: JourneySource | undefined): string {
  if (source?.filter_mode === "last_24_hours") return "Last 24 hours";
  if (source?.filter_mode === "date_range") return "Selected dates";
  return "Latest available month";
}

function addDays(value: string, days: number): string {
  if (!value) return "";
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return "";
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

function minDate(...values: string[]): string {
  return values.filter(Boolean).sort()[0] ?? "";
}

function validateRange(from: string, to: string): string {
  if (!from || !to) return "Choose both dates";
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "Choose valid dates";
  if (start > end) return "From must be before To";
  const days = Math.round((end - start) / 86_400_000) + 1;
  return days > 31 ? "Maximum range is 31 days" : "";
}

function GearIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
