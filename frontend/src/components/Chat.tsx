import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { formatThinkTime } from "../api/journey";
import type { JourneySuggestionGroup, ThinkingStep } from "../api/journey";
import type { AgentInfo, Message } from "../types";
import MessageBubble from "./MessageBubble";

interface Props {
  info: AgentInfo;
  messages: Message[];
  simple: boolean;
  running: boolean;
  steps: ThinkingStep[];
  elapsed: number;
  maxSeconds: number | null;
  notice: string;
  canAsk: boolean;
  sourceSubtitle?: string;
  emptyTitle?: string;
  emptyText?: string;
  suggestionGroups?: JourneySuggestionGroup[];
  modelStatus: {
    provider?: string;
    model?: string;
    ok: boolean;
  } | null;
  /** The /ask page: no menu, no model, no SQL - see view.ts. */
  userMode?: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onNewChat: () => void;
  onAsk: (q: string) => void;
  onMenu: () => void;
}

export default function Chat(p: Props) {
  const [text, setText] = useState("");
  const [suggestionKey, setSuggestionKey] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const groups = p.suggestionGroups ?? [];
  const selectedGroup = groups.find((group) => group.key === suggestionKey) ?? groups[0];

  useEffect(() => {
    if (groups.length && !groups.some((group) => group.key === suggestionKey)) {
      setSuggestionKey(groups[0].key);
    }
  }, [groups, suggestionKey]);

  // Keep the newest message in view.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [p.messages.length, p.running, p.steps.length]);

  // Auto-grow the input up to 6 lines.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || p.running || !p.canAsk) return;
    p.onSend(text);
    setText("");
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const subtitle = p.userMode
    ? "Ask a question about your shopping data in plain English"
    : p.simple
      ? "Simple meaning is on: answers use real-life language"
      : p.sourceSubtitle ?? p.info.subtitle;

  return (
    <main className="chat">
      <header className="chat-header">
        {!p.userMode && (
          <button type="button" className="menu-btn" aria-label="Open menu" onClick={p.onMenu}>
            <span /><span /><span />
          </button>
        )}
        <div className="chat-title">
          {p.userMode ? (
            <div className="brand">
              <span className="brand-mark" aria-hidden="true">D</span>
              <span className="brand-text">
                <h1>DataMyner Intelligence</h1>
                <p>Market Xcel Data Matrix Pvt Ltd</p>
              </span>
            </div>
          ) : (
            <>
              <h1>{p.info.name}</h1>
              <p>{subtitle}</p>
            </>
          )}
        </div>
        {!p.userMode && <ModelStatus status={p.modelStatus} />}
        <button type="button" className="new-chat" onClick={p.onNewChat} disabled={p.running}>New chat</button>
      </header>

      <div className="messages" ref={listRef}>
        {p.messages.length === 0 && !p.running && (
          <div className="empty">
            <div className="empty-title">{p.emptyTitle ?? (p.canAsk
              ? (p.userMode ? "What would you like to know?" : `Ask the ${p.info.name.toLowerCase()}`)
              : p.userMode ? "The data is not available right now" : "Connect a data source to start")}</div>
            <p>{p.emptyText ?? (p.canAsk
              ? "Answers come straight from the data. Numbers are calculated by queries, not guessed."
              : p.userMode ? "Please try again shortly, or ask whoever set this up to check the connection."
                : "Open Connection settings in the sidebar, then retry.")}</p>
            {p.canAsk && (
              groups.length ? (
                <div className="suggestion-browser">
                  <div className="suggestion-categories" role="tablist" aria-label="Journey question categories">
                    {groups.map((group) => (
                      <button key={group.key} type="button" role="tab"
                              aria-selected={selectedGroup?.key === group.key}
                              className={selectedGroup?.key === group.key ? "active" : ""}
                              onClick={() => setSuggestionKey(group.key)}>
                        {group.category}
                      </button>
                    ))}
                  </div>
                  {selectedGroup && (
                    <div className="suggestion-panel" role="tabpanel">
                      <p>{selectedGroup.description}</p>
                      <div className="empty-suggestions">
                        {selectedGroup.questions.map((q) => (
                          <button key={q} type="button" onClick={() => p.onAsk(q)}>{q}</button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="empty-suggestions">
                  {p.info.suggestions.slice(0, 4).map((q) => (
                    <button key={q} type="button" onClick={() => p.onAsk(q)}>{q}</button>
                  ))}
                </div>
              )
            )}
          </div>
        )}

        {p.messages.map((m) => (
          <MessageBubble userMode={p.userMode} key={m.id} message={m} caption={p.info.caption} onAsk={p.onAsk}
                         onDraft={(draft) => { setText(draft); requestAnimationFrame(() => inputRef.current?.focus()); }} />
        ))}

        {p.running && (
          <ThinkingBubble steps={p.steps} elapsed={p.elapsed} maxSeconds={p.maxSeconds} onStop={p.onStop} />
        )}
      </div>

      {p.notice && <div className="notice" role="status">{p.notice}</div>}

      <form className="composer" onSubmit={submit}>
        <textarea ref={inputRef} rows={1} value={text}
                  placeholder={p.canAsk
                    ? (p.userMode ? "Ask a question about your shopping data…" : p.info.placeholder)
                    : p.userMode ? "The data is not available right now…" : "Connect live PostgreSQL to ask a question…"}
                  onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
                  aria-label="Message" />
        {p.running ? (
          <button type="button" className="send stop" aria-label="Stop and show answer" title="Stop and show answer"
                  onClick={p.onStop}>
            <span className="stop-square" />
          </button>
        ) : (
          <button type="submit" className="send" aria-label="Send" disabled={!text.trim() || !p.canAsk}>↑</button>
        )}
      </form>
    </main>
  );
}

function ModelStatus({ status }: { status: Props["modelStatus"] }) {
  if (!status) {
    return <div className="model-status checking" title="Checking language-model connection">
      <span className="model-status-dot" /> Checking model…
    </div>;
  }
  const cloud = status.provider === "openai" || (!status.provider && status.model?.startsWith("gpt-"));
  const provider = cloud ? "OpenAI"
    : status.provider === "ollama" || (!status.provider && status.model) ? "Local LLM" : "Language model";
  const label = status.ok
    ? `${provider}${status.model ? ` · ${status.model}` : ""}`
    : `${provider} offline`;
  return <div className={`model-status ${status.ok ? "connected" : "offline"}`}
              title={status.ok ? `Connected to ${provider}` : `${provider} is not connected`}
              role="status">
    <span className="model-status-dot" /> {label}
  </div>;
}

function ThinkingBubble(p: { steps: ThinkingStep[]; elapsed: number; maxSeconds: number | null; onStop: () => void }) {
  const last = p.steps[p.steps.length - 1];
  const pct = p.maxSeconds ? Math.min(100, (p.elapsed / p.maxSeconds) * 100) : 0;
  return (
    <div className="row assistant">
      <div className="bubble thinking" aria-live="polite">
        <div className="think-row">
          <span className="pulse" />
          <span className="think-text">{last ? last.text : "Checking the data…"}</span>
          <span className="think-time">
            {formatThinkTime(p.elapsed)}{p.maxSeconds ? ` / ${formatThinkTime(p.maxSeconds)}` : ""}
          </span>
        </div>
        {p.maxSeconds !== null && <div className="think-bar"><div style={{ width: `${pct}%` }} /></div>}
        {p.steps.length > 1 && (
          <ol className="think-steps">
            {p.steps.map((s, i) => (
              <li key={i} className={s.ok ? "" : "warn"}>
                <small>{(s.elapsed_ms / 1000).toFixed(1)}s</small> {s.text}
              </li>
            ))}
          </ol>
        )}
        <button type="button" className="think-stop" onClick={p.onStop}>Stop and show answer</button>
      </div>
    </div>
  );
}
