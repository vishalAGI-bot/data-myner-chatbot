import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, DEFAULT_THINK_SECONDS, Depth, JourneyFilter, JourneyHealth, JourneySummary, ThinkingStep,
  connectJourneyLive, getJourneyHealth, getJourneySummary, nearestThinkChoice, newRequestId,
  stopJourneyChat, streamJourneyChat,
} from "./api/journey";
import {
  AnalystCatalog, AnalystHealth, getAnalystCatalog, getAnalystHealth, stopAnalystChat, streamAnalystChat,
} from "./api/analyst";
import type { ConnectionReport } from "./api/settings";
import { AGENTS, AgentId, Message } from "./types";
import { load, save } from "./storage";
import { isUserView } from "./view";
import Sidebar from "./components/Sidebar";
import Chat from "./components/Chat";
import SettingsPage from "./components/SettingsPage";
import SplashScreen from "./components/SplashScreen";

const HEALTH_POLL_MS = 15000;     // live connection status (database ping) refresh

// The backends recognise this wrapper and add a plain-language intro (with no numbers).
const SIMPLE_WRAPPER = "Explain the result in simple meaning, as a short real-life story. Original question: ";

let counter = 0;
const uid = () => `${Date.now().toString(36)}-${(counter++).toString(36)}`;

export type Connection = "checking" | "connected" | "no-data" | "offline";

export default function App() {
  // Fixed for the lifetime of the page: /ask is a different audience, not a toggle.
  const userView = useRef(isUserView()).current;
  const [agent, setAgent] = useState<AgentId>(() => {
    // /ask is one assistant, not a choice between two: the agents are an internal split.
    if (isUserView()) return "analyst";
    const saved = load<string>("agent", "analyst");
    return saved === "journey" ? "journey" : "analyst";
  });
  const [messages, setMessages] = useState<Record<AgentId, Message[]>>({ analyst: [], journey: [] });
  const [followUp, setFollowUp] = useState<Record<string, unknown>>({});   // analyst state for follow-ups
  const [simple, setSimple] = useState<boolean>(() => (isUserView() ? true : load("simple", false)));
  const [depth, setDepth] = useState<Depth>(() => load<Depth>("depth", "auto"));
  // nearestThinkChoice also repairs a value saved by an older build (which allowed any
  // multiple of 5 up to 60) so the slider always has a position to sit on.
  const [maxSeconds, setMaxSeconds] = useState<number>(
    () => nearestThinkChoice(load("maxSeconds", DEFAULT_THINK_SECONDS)));
  const [summary, setSummary] = useState<JourneySummary | null>(null);
  const [journeyHealth, setJourneyHealth] = useState<JourneyHealth | null>(null);
  const [catalog, setCatalog] = useState<AnalystCatalog | null>(null);
  const [connection, setConnection] = useState<Record<AgentId, Connection>>({ analyst: "checking", journey: "checking" });
  const [llmNote, setLlmNote] = useState<Record<AgentId, string>>({ analyst: "", journey: "" });
  const [running, setRunning] = useState(false);
  const [steps, setSteps] = useState<ThinkingStep[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [notice, setNotice] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [page, setPage] = useState<"chat" | "settings">("chat");
  const [health, setHealth] = useState<AnalystHealth | null>(null);
  // Only the /ask page opens with the splash: whoever runs the working page should not wait.
  const [splashDone, setSplashDone] = useState(!userView);
  // Defined here, not in the JSX: a hook inside the `!splashDone &&` branch would stop being
  // called the moment the splash finishes, changing the hook order.
  const finishSplash = useCallback(() => setSplashDone(true), []);

  const requestId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const started = useRef(0);

  useEffect(() => {
    document.body.classList.toggle("user-view", userView);
    document.title = userView ? "DataMyner Intelligence" : "DataMyner";
  }, [userView]);
  useEffect(() => { if (!userView) save("agent", agent); }, [agent, userView]);
  useEffect(() => { if (!userView) save("simple", simple); }, [simple, userView]);
  useEffect(() => save("depth", depth), [depth]);
  useEffect(() => save("maxSeconds", maxSeconds), [maxSeconds]);

  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setElapsed((Date.now() - started.current) / 1000), 200);
    return () => window.clearInterval(t);
  }, [running]);

  // ---------------------------------------------------------------- connect
  const lastSource = useRef<string>("");
  const lastJourneySource = useRef<string>("");

  const refreshAnalyst = useCallback(async (withCatalog = true) => {
    try {
      const h = await getAnalystHealth();
      setHealth(h);
      setLlmNote((n) => ({ ...n, analyst: h.llm_ok ? "" : "Language model offline: built-in analyses still work." }));
      if (!h.data_ready) {
        setConnection((c) => ({ ...c, analyst: "no-data" }));
        setCatalog(null);
        lastSource.current = "";
        return;
      }
      // Reload after a source switch/reconnect or a backend refresh of row
      // counts, applications, and data-quality checks.
      const key = `${h.source.kind}|${h.source.database}|${h.connected_since ?? ""}|${h.refreshed_at ?? ""}`;
      if (withCatalog || key !== lastSource.current) {
        setCatalog(await getAnalystCatalog());
        lastSource.current = key;
      }
      setConnection((c) => ({ ...c, analyst: "connected" }));
    } catch {
      setHealth(null);
      setCatalog(null);
      lastSource.current = "";
      setConnection((c) => ({ ...c, analyst: "offline" }));
    }
  }, []);

  const refreshJourney = useCallback(async (withSummary = true) => {
    try {
      const h = await getJourneyHealth();
      setJourneyHealth(h);
      setLlmNote((n) => ({ ...n, journey: h.llm_ok ? "" : "Language model offline: built-in questions still work." }));
      const ready = (h.data_ready ?? Boolean(h.data_loaded)) && h.source?.kind === "postgres";
      if (!ready) {
        setSummary(null);
        lastJourneySource.current = "";
        setConnection((c) => ({ ...c, journey: "no-data" }));
        return;
      }

      const source = h.source ?? { kind: "postgres" };
      const key = [source.kind, source.database, source.schema, source.table,
        h.connected_since, h.refreshed_at, h.latest_date].map((v) => v ?? "").join("|");
      if (withSummary || key !== lastJourneySource.current) {
        const s = await getJourneySummary();
        if (!s) {
          setSummary(null);
          lastJourneySource.current = "";
          setConnection((c) => ({ ...c, journey: "no-data" }));
          return;
        }
        setSummary(s);
        lastJourneySource.current = key;
      }
      setConnection((c) => ({ ...c, journey: "connected" }));
    } catch {
      setJourneyHealth(null);
      setSummary(null);
      lastJourneySource.current = "";
      setConnection((c) => ({ ...c, journey: "offline" }));
    }
  }, []);

  const reconnectJourney = useCallback(async (filter?: JourneyFilter) => {
    setConnection((c) => ({ ...c, journey: "checking" }));
    try {
      const loaded = await connectJourneyLive(filter);
      setSummary(loaded);
      if (filter?.mode === "date_range") {
        setNotice(`Journey range updated to ${filter.date_from} through ${filter.date_to}.`);
      } else if (filter?.mode === "last_24_hours") {
        setNotice("Journey data updated to the last 24 hours.");
      } else if (filter?.mode === "latest_month") {
        setNotice("Journey data updated to the latest available month.");
      }
    } catch (err) {
      setNotice(`Journey refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      lastJourneySource.current = "";
      await refreshJourney(true);
    }
  }, [refreshJourney]);

  useEffect(() => {
    void refreshAnalyst();
    void refreshJourney();
  }, [refreshAnalyst, refreshJourney]);

  // Live status: ping both read-only PostgreSQL sources every 15 s (paused while a question runs or the tab is hidden).
  useEffect(() => {
    if (running) return;
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshAnalyst(false);
        void refreshJourney(false);
      }
    }, HEALTH_POLL_MS);
    return () => window.clearInterval(t);
  }, [refreshAnalyst, refreshJourney, running]);

  const onConnected = useCallback((report: ConnectionReport) => {
    setFollowUp({});                       // follow-up context belonged to the old data source
    void refreshAnalyst(true);
    if (report.kind === "postgres") {
      void reconnectJourney();
    } else {
      setSummary(null);
      lastJourneySource.current = "";
      void refreshJourney(true);
    }
    const where = report.kind === "postgres" ? `${report.database} (live)` : `snapshot ${report.database}`;
    setMessages((all) => all.analyst.length ? {
      ...all,
      analyst: [...all.analyst, { id: uid(), role: "assistant", content: `Switched data source to ${where}. New questions are answered from it.` }],
    } : all);
  }, [reconnectJourney, refreshAnalyst, refreshJourney]);

  // ------------------------------------------------------------------ send
  const push = (id: AgentId, msg: Message) => setMessages((all) => ({ ...all, [id]: [...all[id], msg] }));

  const send = useCallback(async (text: string) => {
    const question = text.trim();
    if (!question || running) return;
    const current = agent;
    const history = messages[current].filter((m) => !m.error).map((m) => ({ role: m.role, content: m.content }));
    push(current, { id: uid(), role: "user", content: question });
    setNotice("");
    setMenuOpen(false);
    setRunning(true);
    setSteps([]);
    setElapsed(0);
    started.current = Date.now();
    abort.current = new AbortController();
    const id = newRequestId();
    requestId.current = id;
    const message = simple ? SIMPLE_WRAPPER + question : question;

    try {
      if (current === "analyst") {
        if (connection.analyst !== "connected") {
          throw new ApiError("The data analyst is not connected. Check Connection settings in the sidebar (or start the analyst backend on port 8200).", 503);
        }
        const result = await streamAnalystChat(
          { message, history, state: followUp, depth, max_seconds: maxSeconds, request_id: id },
          (step) => setSteps((s) => [...s, step]), abort.current.signal);
        // Empty state clears an older metric after a failed or unrelated
        // answer, preventing it from contaminating the next follow-up.
        setFollowUp(result.state ?? {});
        push(current, { id: uid(), role: "assistant", content: result.answer, analyst: result, thinking: result.thinking });
      } else {
        if (connection.journey !== "connected" || !summary) {
          const reason = journeyHealth?.source?.error || journeyHealth?.refresh_error;
          throw new ApiError(reason
            ? `Live journey data is not ready: ${reason}`
            : "The journey agent is not connected to live cross_app_journey data. Check Connection settings in the sidebar.", 503);
        }
        const result = await streamJourneyChat(
          { message, history, depth, max_seconds: maxSeconds, request_id: id },
          (step) => setSteps((s) => [...s, step]), abort.current.signal);
        push(current, { id: uid(), role: "assistant", content: result.answer, journey: result, thinking: result.thinking });
      }
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      push(current, { id: uid(), role: "assistant", error: true,
        content: aborted ? "Stopped." : err instanceof Error ? err.message : String(err) });
    } finally {
      setRunning(false);
      requestId.current = null;
      abort.current = null;
    }
  }, [agent, connection, depth, followUp, journeyHealth, maxSeconds, messages, running, simple, summary]);

  const stop = useCallback(async () => {
    const id = requestId.current;
    if (!id) return;
    const stopFn = agent === "analyst" ? stopAnalystChat : stopJourneyChat;
    // The server finishes at once with the verified parts; the stream then closes.
    await stopFn(id).catch(() => abort.current?.abort());
  }, [agent]);

  const newChat = useCallback(() => {
    if (running) return;
    setMessages((all) => ({ ...all, [agent]: [] }));
    if (agent === "analyst") setFollowUp({});
    setNotice("");
  }, [agent, running]);

  const journeyRange = summary?.first_date
    ? `${summary.first_date}${summary.last_date && summary.last_date !== summary.first_date ? ` to ${summary.last_date}` : ""}`
    : summary?.last_date ?? "";
  const journeyTable = summary?.source?.table || "cross_app_journey";
  const journeySubtitle = summary
    ? `Live ${journeyTable} · read-only${journeyRange ? ` · Data ${journeyRange}` : ""}`
    : undefined;
  const journeyError = journeyHealth?.source?.error || journeyHealth?.refresh_error || "";
  const journeyReady = connection.journey === "connected" && Boolean(summary);
  const journeyEmptyTitle = journeyReady ? "What would you like to check?"
    : connection.journey === "checking" ? "Connecting to live journey data…"
      : connection.journey === "offline" ? "Journey service unavailable"
        : "Live journey data is not ready";
  const journeyEmptyText = journeyReady
    ? "Choose a category and ask one of the questions generated from the current data."
    : journeyError || (connection.journey === "checking"
      ? "The journey agent is opening the saved read-only PostgreSQL connection."
      : "Open Connection settings in the sidebar, save the PostgreSQL connection, then retry.");
  const activeNotice = notice || (agent === "journey" && journeyHealth?.stale
    ? `Live refresh is delayed; answers use the last successful refresh.${journeyHealth.refresh_error ? ` ${journeyHealth.refresh_error}` : ""}`
    : "") || llmNote[agent];

  return (
    <div className="shell">
      {!splashDone && (
        <SplashScreen ready={connection.analyst === "connected"} onDone={finishSplash} />
      )}
      <div className={`frame${userView ? " frame-user" : ""}`}>
        {!userView && (
        <Sidebar
          agent={agent}
          onAgent={(a) => { if (!running) { setAgent(a); setNotice(""); setMenuOpen(false); setPage("chat"); } }}
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          summary={summary}
          catalog={catalog}
          simple={simple}
          onSimple={setSimple}
          depth={depth}
          onDepth={setDepth}
          maxSeconds={maxSeconds}
          onMaxSeconds={setMaxSeconds}
          running={running}
          onAsk={(q) => { setPage("chat"); void send(q); }}
          connection={connection[agent]}
          onRetry={agent === "analyst" ? () => void refreshAnalyst() : () => void reconnectJourney()}
          health={health}
          journeyHealth={journeyHealth}
          onJourneyRange={(dateFrom, dateTo) => void reconnectJourney({
            mode: "date_range", date_from: dateFrom, date_to: dateTo,
          })}
          onJourneyLatest={() => void reconnectJourney({ mode: "latest_month" })}
          onJourney24Hours={() => void reconnectJourney({ mode: "last_24_hours" })}
          onSettings={() => { if (!running) { setPage("settings"); setMenuOpen(false); } }}
          settingsOpen={page === "settings"}
        />
        )}
        {page === "settings" && !userView ? (
          <SettingsPage health={health} onBack={() => setPage("chat")} onMenu={() => setMenuOpen(true)}
                        onConnected={onConnected} />
        ) : (
        <Chat
          info={AGENTS[agent]}
          messages={messages[agent]}
          simple={simple}
          running={running}
          steps={steps}
          elapsed={elapsed}
          maxSeconds={maxSeconds}
          notice={activeNotice}
          canAsk={agent === "analyst" ? connection.analyst === "connected"
            : connection.journey === "connected" && Boolean(summary)}
          sourceSubtitle={agent === "journey" ? journeySubtitle : undefined}
          emptyTitle={agent === "journey" ? journeyEmptyTitle : undefined}
          emptyText={agent === "journey" ? journeyEmptyText : undefined}
          suggestionGroups={agent === "journey" ? summary?.suggestion_groups : undefined}
          userMode={userView}
          modelStatus={userView ? null : (() => {
            const model = agent === "journey" ? journeyHealth : health;
            return model ? {
              provider: model.llm_provider,
              model: model.llm_model,
              ok: model.llm_ok,
            } : null;
          })()}
          onSend={send}
          onStop={stop}
          onNewChat={newChat}
          onAsk={send}
          onMenu={() => setMenuOpen(true)}
        />
        )}
        {menuOpen && !userView && <button type="button" className="backdrop" aria-label="Close menu" onClick={() => setMenuOpen(false)} />}
      </div>
    </div>
  );
}
