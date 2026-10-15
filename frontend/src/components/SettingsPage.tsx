import { useCallback, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import type { AnalystHealth } from "../api/analyst";
import {
  ConnectionReport, LlmReport, SettingsApiError, SettingsValues, SettingsView,
  getAdminToken, getSettings, resetSettings, saveSettings, setAdminToken, testLlm, testSettings,
} from "../api/settings";

interface Props {
  health: AnalystHealth | null;
  onBack: () => void;
  onMenu: () => void;
  onConnected: (report: ConnectionReport) => void;
  /** The colour theme picker, shown in the header as on the chat page. */
  themeControl?: ReactNode;
}

type Secret = "ssh_password" | "db_password" | "openai_api_key";
const SSL_MODES = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"];

export default function SettingsPage({ health, onBack, onMenu, onConnected, themeControl }: Props) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [form, setForm] = useState<SettingsValues | null>(null);
  const [useSsh, setUseSsh] = useState(true);
  const [sshAuth, setSshAuth] = useState<"password" | "key">("password");
  const [clear, setClear] = useState<Record<Secret, boolean>>({
    ssh_password: false, db_password: false, openai_api_key: false,
  });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);
  const [token, setToken] = useState(getAdminToken());
  const [busy, setBusy] = useState<"" | "test" | "save" | "reset" | "llm">("");
  const [report, setReport] = useState<ConnectionReport | null>(null);
  const [reportKind, setReportKind] = useState<"test" | "save">("test");
  const [llmReport, setLlmReport] = useState<LlmReport | null>(null);
  const [message, setMessage] = useState("");

  const adopt = useCallback((v: SettingsView) => {
    setView(v);
    setForm(v.values);
    // First-time live setup: your database sits behind SSH, so start with the tunnel switched on.
    setUseSsh(Boolean(v.values.ssh_host) || v.values.source !== "postgres");
    setSshAuth(v.values.ssh_pkey ? "key" : "password");
    setClear({ ssh_password: false, db_password: false, openai_api_key: false });
    setFieldErrors({});
  }, []);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      adopt(await getSettings());
    } catch (err) {
      const e = err as SettingsApiError;
      setLoadError({ status: e.status ?? 0, message: e.message || "The analyst backend is not reachable." });
    }
  }, [adopt]);

  useEffect(() => { void load(); }, [load]);

  const set = <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => {
    setForm((f) => (f ? { ...f, [key]: value } : f));
    setFieldErrors((errors) => {
      const next = { ...errors };
      delete next[key];
      return next;
    });
  };

  // Only what changed is sent; an empty password means "keep the saved one".
  const changes = useMemo(() => {
    if (!form || !view) return {} as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    (Object.keys(form) as (keyof SettingsValues)[]).forEach((k) => {
      if (k === "ssh_password" || k === "db_password" || k === "openai_api_key") {
        if (form[k]) out[k] = form[k];
      } else if (form[k] !== view.values[k]) {
        out[k] = form[k];
      }
    });
    if (form.source === "postgres") {
      if (!useSsh) {
        if (view.values.ssh_host) out.ssh_host = "";
      } else if (sshAuth === "password" && view.values.ssh_pkey) {
        out.ssh_pkey = "";
      } else if (sshAuth === "key") {
        if (view.secrets_set.ssh_password) out.clear_ssh_password = true;
      }
    }
    (Object.keys(clear) as Secret[]).forEach((k) => { if (clear[k]) out[`clear_${k}`] = true; });
    return out;
  }, [form, view, useSsh, sshAuth, clear]);

  const dirty = Object.keys(changes).length > 0;

  const run = async (kind: "test" | "save", force = false) => {
    if (!form || !view) return;
    const missing = checkRequired(form, view, useSsh, sshAuth, clear);
    if (Object.keys(missing).length) {
      showInvalid(missing);
      return;
    }
    setBusy(kind);
    setMessage("");
    setReport(null);
    setReportKind(kind);
    try {
      const result = kind === "test" ? await testSettings(changes) : await saveSettings(changes, force);
      setReport(result);
      if (kind === "save" && result.saved) {
        if (result.settings) adopt(result.settings);
        setMessage(result.ok ? "Saved. The chatbot now answers from this connection." :
          "Saved. The chatbot will keep retrying this connection.");
        if (result.ok) onConnected(result);
      }
    } catch (err) {
      const e = err as SettingsApiError;
      if (e.fields && Object.keys(e.fields).length) showInvalid(e.fields);
      else setReport({ ok: false, error: e.message });
      if (e.status === 401 || e.status === 403) setLoadError({ status: e.status, message: e.message });
    } finally {
      setBusy("");
    }
  };

  // Highlight the fields, say which ones in the message, and take the user to the first one.
  const showInvalid = (errors: Record<string, string>) => {
    setFieldErrors(errors);
    const names = Object.keys(errors).map((k) => LABELS[k] ?? k.replace(/_/g, " "));
    setReport({ ok: false, invalid: true, error: `Please fill in or fix: ${names.join(", ")}.` });
    window.requestAnimationFrame(() => {
      const first = document.querySelector<HTMLInputElement>(".set-field.has-error input, .set-field.has-error select");
      first?.scrollIntoView({ behavior: "smooth", block: "center" });
      first?.focus({ preventScroll: true });
    });
  };

  const reset = async () => {
    if (!window.confirm("Forget the settings saved here and use backend/.env again?")) return;
    setBusy("reset");
    setMessage("");
    try {
      const result = await resetSettings();
      setReport(result);
      setReportKind("save");
      if (result.settings) adopt(result.settings);
      setMessage("Back to the settings in backend/.env.");
      if (result.ok) onConnected(result);
    } catch (err) {
      setReport({ ok: false, error: (err as Error).message });
    } finally {
      setBusy("");
    }
  };

  const checkModel = async () => {
    if (!form) return;
    setBusy("llm");
    setLlmReport(null);
    try {
      setLlmReport(await testLlm({
        llm_provider: form.llm_provider,
        llm_host: form.llm_host,
        llm_model: form.llm_model,
        openai_api_key: form.openai_api_key,
        openai_model: form.openai_model,
      }));
    } catch (err) {
      const e = err as SettingsApiError;
      setFieldErrors((f) => ({ ...f, ...(e.fields ?? {}) }));
      setLlmReport({ ok: false, error: e.message });
    } finally {
      setBusy("");
    }
  };

  const unlock = () => {
    setAdminToken(token.trim());
    void load();
  };

  const header = (
    <header className="chat-header">
      <button type="button" className="menu-btn" aria-label="Open menu" onClick={onMenu}>
        <span /><span /><span />
      </button>
      <div className="chat-title">
        <h1>Settings</h1>
        <p>Shared live data source for the data analyst and journey agent</p>
      </div>
      {themeControl}
      <button type="button" className="new-chat" onClick={onBack}>Back to chat</button>
    </header>
  );

  if (loadError && !form) {
    return (
      <main className="chat settings">
        {header}
        <div className="settings-body">
          <section className="set-card">
            {loadError.status === 401 ? (
              <>
                <h2>Admin token needed</h2>
                <p className="set-help">Enter the <code>ANALYST_ADMIN_TOKEN</code> value from <code>backend/.env</code>.
                  It is kept only for this browser tab.</p>
                <form className="token-row" onSubmit={(e) => { e.preventDefault(); unlock(); }}>
                  <input type="password" autoComplete="off" value={token} aria-label="Admin token"
                         onChange={(e) => setToken(e.target.value)} placeholder="Admin token" />
                  <button type="submit" className="btn primary" disabled={!token.trim()}>Unlock</button>
                </form>
                {getAdminToken() && <p className="set-error">{loadError.message}</p>}
              </>
            ) : loadError.status === 403 ? (
              <>
                <h2>Settings are locked on this computer</h2>
                <p className="set-help">{loadError.message}</p>
                <p className="set-help">Open the app on the server itself (http://localhost:5173), or set
                  <code> ANALYST_ADMIN_TOKEN=&lt;a long random value&gt;</code> in <code>backend/.env</code>,
                  restart, and enter that token here.</p>
              </>
            ) : (
              <>
                <h2>Cannot reach the analyst backend</h2>
                <p className="set-help">{loadError.message}</p>
                <button type="button" className="btn" onClick={() => void load()}>Retry</button>
              </>
            )}
          </section>
        </div>
      </main>
    );
  }

  if (!form || !view) {
    return (
      <main className="chat settings">
        {header}
        <div className="settings-body"><p className="set-help">Loading settings…</p></div>
      </main>
    );
  }

  const live = form.source === "postgres";
  const err = (k: string) => fieldErrors[k];

  return (
    <main className="chat settings">
      {header}
      <div className="settings-body">
        <LiveStatus health={health} view={view} />

        <section className="set-card">
          <h2>Data source</h2>
          <div className="choice-grid" role="radiogroup" aria-label="Data source">
            <Choice on={live} onClick={() => set("source", "postgres")} title="Live PostgreSQL"
                    sub="Real-time answers from your database, read-only" />
            <Choice on={!live} onClick={() => set("source", "snapshot")} title="Offline snapshot"
                    sub="JSON export in a folder on the server, for demos and testing" />
          </div>
          {live && (
            <p className="set-help">
              You need the same details your database script uses: the <strong>SSH server, SSH username and
              password</strong> (or key file), and the <strong>database username and password</strong>. With SSH, the
              database host is usually <code>127.0.0.1</code> and port <code>5432</code>.
            </p>
          )}
          {!live && (
            <div className="grid-2">
              <Text label="Snapshot folder (on the server)" value={form.snapshot_dir} error={err("snapshot_dir")}
                    onChange={(v) => set("snapshot_dir", v)} wide />
            </div>
          )}
        </section>

        {live && (
          <>
            <section className="set-card">
              <div className="card-head">
                <h2>SSH tunnel</h2>
                <label className="inline-toggle">
                  <input type="checkbox" className="switch light" checked={useSsh}
                         onChange={(e) => setUseSsh(e.target.checked)} />
                  <span>Connect through SSH</span>
                </label>
              </div>
              {useSsh ? (
                <>
                  <div className="grid-3">
                    <Text label="SSH server" value={form.ssh_host} error={err("ssh_host")} placeholder="server address"
                          onChange={(v) => set("ssh_host", v)} />
                    <Num label="Port" value={form.ssh_port} error={err("ssh_port")} onChange={(v) => set("ssh_port", v)} />
                    <Text label="SSH username" value={form.ssh_username} error={err("ssh_username")}
                          onChange={(v) => set("ssh_username", v)} />
                  </div>
                  <div className="seg light" role="radiogroup" aria-label="SSH login">
                    <button type="button" role="radio" aria-checked={sshAuth === "password"}
                            className={sshAuth === "password" ? "on" : ""} onClick={() => setSshAuth("password")}>Password</button>
                    <button type="button" role="radio" aria-checked={sshAuth === "key"}
                            className={sshAuth === "key" ? "on" : ""} onClick={() => setSshAuth("key")}>Key file (safer)</button>
                  </div>
                  <div className="grid-2">
                    {sshAuth === "password" ? (
                      <Password label="SSH password" value={form.ssh_password} saved={view.secrets_set.ssh_password}
                                cleared={clear.ssh_password} error={err("ssh_password")}
                                onChange={(v) => set("ssh_password", v)}
                                onClear={(c) => setClear((s) => ({ ...s, ssh_password: c }))} />
                    ) : (
                      <Text label="Private key file (path on the server)" value={form.ssh_pkey} error={err("ssh_pkey")}
                            placeholder="/home/user/.ssh/id_ed25519" onChange={(v) => set("ssh_pkey", v)} />
                    )}
                    <Text label="Server host key (optional)" value={form.ssh_host_key_sha256} error={err("ssh_host_key_sha256")}
                          placeholder="SHA256:…" hint="When set, any other key is refused (protects against interception)"
                          onChange={(v) => set("ssh_host_key_sha256", v)} />
                  </div>
                </>
              ) : (
                <p className="set-help">The server connects straight to the database host below.</p>
              )}
            </section>

            <section className="set-card">
              <h2>Database</h2>
              {useSsh && <p className="set-help">Host and port as seen from the SSH server (usually 127.0.0.1 : 5432).</p>}
              <div className="grid-3">
                <Text label="Database host" value={form.db_host} error={err("db_host")} onChange={(v) => set("db_host", v)} />
                <Num label="Port" value={form.db_port} error={err("db_port")} onChange={(v) => set("db_port", v)} />
                <Text label="Database name" value={form.db_name} error={err("db_name")} onChange={(v) => set("db_name", v)} />
                <Text label="Username" value={form.db_username} error={err("db_username")}
                      hint="Best: the read-only role from readonly_role.sql" onChange={(v) => set("db_username", v)} />
                <Password label="Password" value={form.db_password} saved={view.secrets_set.db_password}
                          cleared={clear.db_password} error={err("db_password")}
                          onChange={(v) => set("db_password", v)}
                          onClear={(c) => setClear((s) => ({ ...s, db_password: c }))} />
                <Text label="Schema" value={form.db_schema} error={err("db_schema")} onChange={(v) => set("db_schema", v)} />
                <Field label="SSL mode" error={err("db_sslmode")}>
                  <select value={form.db_sslmode} onChange={(e) => set("db_sslmode", e.target.value)}>
                    {SSL_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </Field>
                <Num label="Refresh apps and latest date every (s)" value={form.refresh_seconds} error={err("refresh_seconds")}
                     hint="0 = never" onChange={(v) => set("refresh_seconds", v)} />
              </div>
            </section>
          </>
        )}

        <section className="set-card">
          <h2>Answers</h2>
          <div className="grid-3">
            <Text label="“We / our” means" value={form.home_app} error={err("home_app")} onChange={(v) => set("home_app", v)} />
            <Field label="Purchase attribution window" error={err("default_window_hours")}
                   hint="Used for purchase matching, not the Journey data date range">
              <select value={form.default_window_hours} onChange={(e) => set("default_window_hours", Number(e.target.value))}>
                {[24, 48, 72].map((h) => <option key={h} value={h}>{h} hours</option>)}
              </select>
            </Field>
            <Text label="Time zone for days" value={form.timezone} error={err("timezone")} onChange={(v) => set("timezone", v)} />
            <Num label="Query time limit (s)" value={Math.round(form.statement_timeout_ms / 1000)} error={err("statement_timeout_ms")}
                 hint="Used for background checks. A chat question uses its own “Max thinking time” instead"
                 onChange={(v) => set("statement_timeout_ms", v * 1000)} />
            <Num label="Maximum result rows" value={form.max_rows} error={err("max_rows")}
                 hint="Aggregates can scan the full dataset; this only caps rows returned to the chat"
                 onChange={(v) => set("max_rows", v)} />
            <Field label="Custom questions">
              <label className="inline-toggle">
                <input type="checkbox" className="switch light" checked={form.allow_free_sql}
                       onChange={(e) => set("allow_free_sql", e.target.checked)} />
                <span>{form.allow_free_sql ? "Model may write checked queries" : "Built-in analyses only"}</span>
              </label>
            </Field>
          </div>
        </section>

        <section className="set-card">
          <h2>Language model</h2>
          <div className="choice-grid" role="radiogroup" aria-label="Language model provider">
            <Choice on={form.llm_provider === "ollama"} onClick={() => set("llm_provider", "ollama")}
                    title="Local Ollama" sub="Runs on your server; prompts stay local" />
            <Choice on={form.llm_provider === "openai"} onClick={() => set("llm_provider", "openai")}
                    title="OpenAI API" sub="Uses gpt-6-luna; API usage is billed by OpenAI" />
          </div>
          <div className="grid-3">
            {form.llm_provider === "ollama" ? (
              <>
                <Text label="Ollama address" value={form.llm_host} error={err("llm_host")} onChange={(v) => set("llm_host", v)} />
                <Text label="Model" value={form.llm_model} error={err("llm_model")} onChange={(v) => set("llm_model", v)} />
              </>
            ) : (
              <>
                <Password label="OpenAI API key" value={form.openai_api_key}
                          saved={view.secrets_set.openai_api_key} cleared={clear.openai_api_key}
                          error={err("openai_api_key")} onChange={(v) => set("openai_api_key", v)}
                          onClear={(c) => setClear((s) => ({ ...s, openai_api_key: c }))} />
                <Field label="Model" error={err("openai_model")} hint="Responses API · storage disabled">
                  <input type="text" value={form.openai_model} readOnly aria-readonly="true" />
                </Field>
              </>
            )}
            <Field label=" ">
              <button type="button" className="btn" disabled={busy !== ""} onClick={() => void checkModel()}>
                {busy === "llm" ? "Checking…" : "Test model"}
              </button>
            </Field>
          </div>
          {llmReport && (
            <p className={llmReport.ok ? "set-ok" : "set-error"} role="status">
              {llmReport.ok ? `Model ready (${llmReport.latency_ms} ms).` : llmReport.error}
            </p>
          )}
        </section>

        {report && <Report report={report} kind={reportKind}
                           onPin={report.ssh_host_key && report.ssh_host_key !== form.ssh_host_key_sha256
                             ? () => set("ssh_host_key_sha256", report.ssh_host_key!) : undefined} />}
        {message && <p className="set-ok" role="status">{message}</p>}

        <div className="set-actions">
          <button type="button" className="btn" disabled={busy !== ""} onClick={() => void run("test")}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </button>
          <button type="button" className="btn primary" disabled={busy !== ""} onClick={() => void run("save")}>
            {busy === "save" ? "Connecting…" : dirty ? "Save & connect" : "Reconnect"}
          </button>
          {report && !report.ok && reportKind === "save" && report.saved === false && (
            <button type="button" className="btn ghost" disabled={busy !== ""} onClick={() => void run("save", true)}>
              Save anyway
            </button>
          )}
          <span className="spacer" />
          {view.saved && (
            <button type="button" className="btn ghost" disabled={busy !== ""} onClick={() => void reset()}>
              {busy === "reset" ? "Resetting…" : "Use backend/.env"}
            </button>
          )}
        </div>
        <p className="set-foot">
          Saved on the server in <code>{view.settings_file}</code> (owner-only). Passwords are never sent back to the
          browser. {view.admin === "token" ? "Unlocked with the admin token." : "Editable because you are on the server itself."}
        </p>
      </div>
    </main>
  );
}

// ------------------------------------------------------------------ checks

const LABELS: Record<string, string> = {
  ssh_host: "SSH server", ssh_port: "SSH port", ssh_username: "SSH username", ssh_password: "SSH password",
  ssh_pkey: "Private key file", ssh_host_key_sha256: "Server host key", db_host: "Database host", db_port: "Port",
  db_name: "Database name", db_username: "Username", db_password: "Password", db_schema: "Schema",
  db_sslmode: "SSL mode", snapshot_dir: "Snapshot folder", statement_timeout_ms: "Query time limit",
  max_rows: "Max rows per answer", refresh_seconds: "Refresh every", home_app: "“We / our” means",
  timezone: "Time zone", llm_host: "Ollama address", llm_model: "Model", default_window_hours: "Default purchase window",
  openai_api_key: "OpenAI API key", openai_model: "OpenAI model",
};

function checkRequired(form: SettingsValues, view: SettingsView, useSsh: boolean, sshAuth: "password" | "key",
                       clear: Record<Secret, boolean>): Record<string, string> {
  const out: Record<string, string> = {};
  const numbers: (keyof SettingsValues)[] = ["db_port", "statement_timeout_ms", "max_rows", "refresh_seconds"];
  if (form.source === "postgres" && useSsh) numbers.push("ssh_port");
  numbers.filter((k) => Number.isNaN(form[k] as number)).forEach((k) => { out[k] = "enter a number"; });
  const has = (k: Secret) => Boolean(form[k]) || (view.secrets_set[k] && !clear[k]);
  if (form.llm_provider === "openai" && !has("openai_api_key")) out.openai_api_key = "required";
  if (form.source !== "postgres") {
    if (!form.snapshot_dir.trim()) out.snapshot_dir = "required";
    return out;
  }
  if (useSsh) {
    if (!form.ssh_host.trim()) out.ssh_host = "required (or turn off “Connect through SSH”)";
    if (!form.ssh_username.trim()) out.ssh_username = "required";
    if (sshAuth === "password" && !has("ssh_password")) out.ssh_password = "required";
    if (sshAuth === "key" && !form.ssh_pkey.trim()) out.ssh_pkey = "required";
  }
  if (!form.db_host.trim()) out.db_host = "required";
  if (!form.db_name.trim()) out.db_name = "required";
  if (!form.db_username.trim()) out.db_username = "required for a live connection";
  if (!has("db_password")) out.db_password = "required for a live connection";
  return out;
}

// ------------------------------------------------------------------ pieces

function LiveStatus({ health, view }: { health: AnalystHealth | null; view: SettingsView }) {
  const src = health?.source;
  const ok = Boolean(health?.data_ready);
  let title = "Checking…";
  if (health) {
    title = ok
      ? src?.kind === "postgres" ? `Live · ${src.database}${src.via_ssh ? " via SSH" : ""}` : `Offline snapshot · ${src?.database ?? ""}`
      : "Not connected";
  }
  const since = health?.connected_since ? new Date(health.connected_since * 1000).toLocaleTimeString() : null;
  const refreshed = health?.refreshed_at ? new Date(health.refreshed_at * 1000).toLocaleTimeString() : null;
  return (
    <section className={`set-card live-card ${ok ? "is-ok" : health ? "is-bad" : ""}`} aria-live="polite">
      <div className="live-row">
        <span className={`live-dot ${ok ? "ok" : health ? "bad" : ""}`} />
        <strong>{title}</strong>
        {ok && health?.db_ping_ms != null && <span className="live-ping">{health.db_ping_ms} ms</span>}
      </div>
      <div className="live-lines">
        {!ok && (src?.error || view.error) && <div className="set-error">{src?.error ?? view.error}</div>}
        {ok && since && <span>Connected since {since}</span>}
        {ok && health?.latest_date && <span>Latest data {health.latest_date}</span>}
        {ok && refreshed && src?.kind === "postgres" && <span>Apps and dates re-read {refreshed}</span>}
        <span>Model: {health?.llm_ok ? health.llm_model ?? "ready" : "offline (built-in analyses still work)"}</span>
      </div>
    </section>
  );
}

function Report({ report, kind, onPin }: { report: ConnectionReport; kind: "test" | "save"; onPin?: () => void }) {
  const [showMissing, setShowMissing] = useState(false);
  if (!report.ok) {
    return (
      <section className="set-card report bad" role="alert">
        <strong>{report.invalid ? "Some fields need attention" : kind === "save" && report.saved ? "Saved, but not connected" : "Connection failed"}</strong>
        <p>{report.error}</p>
      </section>
    );
  }
  const counts = Object.entries(report.row_counts ?? {}).filter(([, v]) => v !== null && v !== undefined);
  return (
    <section className="set-card report good" role="status">
      <strong>{kind === "save" ? "Connected and saved" : "Connection works"} · {report.connect_ms} ms</strong>
      <ul>
        <li>
          {report.kind === "postgres"
            ? `PostgreSQL ${report.server_version} as ${report.user}${report.via_ssh ? " through SSH" : ""}, ${report.read_only_session ? "read-only session" : "session NOT read-only"}, ping ${report.ping_ms} ms`
            : `Offline snapshot (${report.database})`}
        </li>
        <li>{report.tables_found} tables readable · {report.metrics_ready} of {report.metrics_total} analyses ready</li>
        {report.apps_total ? <li>{report.apps_total} apps: {report.apps?.slice(0, 8).join(", ")}{(report.apps_total ?? 0) > 8 ? "…" : ""}</li> : null}
        {report.date_range?.[0] && <li>Data from {report.date_range[0]} to {report.date_range[1]}</li>}
        {counts.length > 0 && <li>{counts.map(([k, v]) => `${k} ~${Number(v).toLocaleString()}`).join(" · ")}</li>}
        {report.ssh_host_key && (
          <li>
            SSH server key <code>{report.ssh_host_key}</code>
            {onPin && <button type="button" className="link-btn dark inline" onClick={onPin}>Pin this key</button>}
          </li>
        )}
      </ul>
      {(report.warnings ?? []).map((w) => <p key={w} className="set-warn">{w}</p>)}
      {(report.tables_missing?.length ?? 0) > 0 && (
        <>
          <button type="button" className="link-btn dark" onClick={() => setShowMissing(!showMissing)} aria-expanded={showMissing}>
            {showMissing ? "Hide" : "Show"} {report.tables_missing!.length} documented tables that are missing or empty
          </button>
          {showMissing && <p className="set-help">{report.tables_missing!.join(", ")}</p>}
        </>
      )}
    </section>
  );
}

function Choice({ on, onClick, title, sub }: { on: boolean; onClick: () => void; title: string; sub: string }) {
  return (
    <button type="button" role="radio" aria-checked={on} className={`choice ${on ? "on" : ""}`} onClick={onClick}>
      <span className="choice-title">{title}</span>
      <span className="choice-sub">{sub}</span>
    </button>
  );
}

function Field({ label, error, hint, children, wide }: { label: string; error?: string; hint?: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className={`set-field ${error ? "has-error" : ""} ${wide ? "wide" : ""}`}>
      <span className="set-label">{label}</span>
      {children}
      {error ? <span className="set-error">{error}</span> : hint ? <span className="set-hint">{hint}</span> : null}
    </label>
  );
}

function Text(p: { label: string; value: string; onChange: (v: string) => void; error?: string; hint?: string; placeholder?: string; wide?: boolean }) {
  return (
    <Field label={p.label} error={p.error} hint={p.hint} wide={p.wide}>
      <input type="text" value={p.value} placeholder={p.placeholder} spellCheck={false} autoComplete="off"
             aria-invalid={Boolean(p.error)} onChange={(e) => p.onChange(e.target.value)} />
    </Field>
  );
}

function Num(p: { label: string; value: number; onChange: (v: number) => void; error?: string; hint?: string }) {
  return (
    <Field label={p.label} error={p.error} hint={p.hint}>
      <input type="number" inputMode="numeric" value={Number.isFinite(p.value) ? p.value : ""}
             aria-invalid={Boolean(p.error)} onChange={(e) => p.onChange(e.target.value === "" ? NaN : Number(e.target.value))} />
    </Field>
  );
}

function Password(p: { label: string; value: string; saved: boolean; cleared: boolean; error?: string;
                       onChange: (v: string) => void; onClear: (c: boolean) => void }) {
  const id = useId();
  const hint = p.saved && !p.value ? (p.cleared ? "Will be removed when you save" : "Saved. Leave empty to keep it") : "";
  return (
    <div className={`set-field ${p.error ? "has-error" : ""}`}>
      <label className="set-label" htmlFor={id}>{p.label}</label>
      <input id={id} type="password" value={p.value} autoComplete="new-password" disabled={p.cleared}
             placeholder={p.saved ? "•••••••• (saved)" : ""} aria-invalid={Boolean(p.error)}
             onChange={(e) => p.onChange(e.target.value)} />
      {p.error ? <span className="set-error">{p.error}</span> : hint ? <span className="set-hint">{hint}</span> : null}
      {p.saved && (
        <label className="clear-secret">
          <input type="checkbox" checked={p.cleared} onChange={(e) => p.onClear(e.target.checked)} /> remove saved value
        </label>
      )}
    </div>
  );
}
