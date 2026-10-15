/* Chat history: every saved conversation with its date and time, on both pages.
 * Opening one restores it, follow-ups included; deleting is the only way one goes away (see history.ts). */

import { useEffect, useRef, useState } from "react";
import type { ConversationMeta } from "../history";
import { AGENTS } from "../types";
import { dayLabel, fullLabel, isSameDay, isoLabel, timeLabel } from "../when";

interface Props {
  open: boolean;
  conversations: ConversationMeta[];
  /** Open in the chat right now (one per agent). */
  activeIds: string[];
  /** Being answered right now: it cannot be deleted until the answer is in. */
  busyId: string | null;
  running: boolean;
  storage: "saved" | "memory";
  /** The working page has two agents; say which one each conversation was with. */
  showAgent: boolean;
  onOpen: (conversation: ConversationMeta) => void;
  onDelete: (conversation: ConversationMeta) => void;
  onDeleteAll: () => void;
  onClose: () => void;
}

const SEARCH_FROM = 4;            // the search box appears once the list is long enough to need it

export default function HistoryPanel(p: Props) {
  const [query, setQuery] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const { open, onClose } = p;

  useEffect(() => {
    if (!open) {
      setConfirmId(null);
      setConfirmAll(false);
      return;
    }
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const q = query.trim().toLowerCase();
  const shown = p.conversations.filter((c) => !q || c.title.toLowerCase().includes(q));
  const now = Date.now();
  // The list is newest first, so each day's conversations are already next to each other.
  const groups: { label: string; items: ConversationMeta[] }[] = [];
  for (const c of shown) {
    const label = dayLabel(c.updatedAt, now);
    const last = groups[groups.length - 1];
    if (last?.label === label) last.items.push(c);
    else groups.push({ label, items: [c] });
  }
  const total = p.conversations.length;

  return (
    <>
      <button type="button" className="history-backdrop" aria-label="Close chat history" onClick={onClose} />
      <aside className="history-panel" role="dialog" aria-modal="true" aria-labelledby="history-heading">
        <header className="history-head">
          <h2 id="history-heading">Chat history</h2>
          {total > 0 && <span className="history-total">{total}</span>}
          <button ref={closeRef} type="button" className="history-close" aria-label="Close chat history"
                  onClick={onClose}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
          </button>
        </header>

        {total >= SEARCH_FROM && (
          <input type="search" className="history-search" value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="Search your questions…" aria-label="Search your questions" />
        )}

        <div className="history-list">
          {total === 0 && (
            <div className="history-empty">
              <strong>No conversations yet</strong>
              <p>Each question and answer is saved here automatically, with its date and time.</p>
            </div>
          )}
          {total > 0 && shown.length === 0 && (
            <div className="history-empty"><p>No conversation matches “{query.trim()}”.</p></div>
          )}
          {groups.map((group) => (
            <section key={group.label} className="history-group" aria-label={group.label}>
              <h3>{group.label}</h3>
              <ul>
                {group.items.map((c) => {
                  const active = p.activeIds.includes(c.id);
                  const started = isSameDay(c.createdAt, c.updatedAt) ? "" : `started ${dayLabel(c.createdAt, now)}`;
                  return (
                    <li key={c.id} className={`history-item${active ? " active" : ""}`}>
                      <button type="button" className="history-open" disabled={p.running}
                              aria-current={active ? "true" : undefined}
                              title={`Started ${fullLabel(c.createdAt)}\nLast message ${fullLabel(c.updatedAt)}`}
                              onClick={() => p.onOpen(c)}>
                        <span className="history-title">{c.title}</span>
                        <span className="history-meta">
                          <time dateTime={isoLabel(c.updatedAt)}>{timeLabel(c.updatedAt)}</time>
                          <span>{c.questions} question{c.questions === 1 ? "" : "s"}</span>
                          {started && <span>{started}</span>}
                          {p.showAgent && <span className="history-agent">{AGENTS[c.agent].name}</span>}
                          {active && <span className="history-current">open</span>}
                        </span>
                      </button>
                      {confirmId === c.id ? (
                        <span className="history-confirm" role="group" aria-label="Confirm delete">
                          <button type="button" className="danger" onClick={() => { setConfirmId(null); p.onDelete(c); }}>
                            Delete
                          </button>
                          <button type="button" onClick={() => setConfirmId(null)}>Keep</button>
                        </span>
                      ) : (
                        <button type="button" className="history-delete" disabled={c.id === p.busyId}
                                aria-label={`Delete conversation: ${c.title}`}
                                title={c.id === p.busyId ? "Being answered - delete it after the answer"
                                  : "Delete this conversation"}
                                onClick={() => { setConfirmAll(false); setConfirmId(c.id); }}>
                          <svg viewBox="0 0 16 16" aria-hidden="true">
                            <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5M7 7v4M9 7v4" />
                          </svg>
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>

        <footer className="history-foot">
          <p className="history-note">
            {p.storage === "memory"
              ? "This browser cannot keep history (for example in a private window), so it is lost when the page closes."
              : "Saved in this browser: kept after a reload or a restart, until you delete it here."}
          </p>
          {total > 0 && (confirmAll ? (
            <div className="history-confirm-all" role="group" aria-label="Confirm delete all">
              <span>Delete all {total} conversation{total === 1 ? "" : "s"}? This cannot be undone.</span>
              <div>
                <button type="button" className="danger" onClick={() => { setConfirmAll(false); p.onDeleteAll(); }}>
                  Delete all
                </button>
                <button type="button" onClick={() => setConfirmAll(false)}>Cancel</button>
              </div>
            </div>
          ) : (
            <button type="button" className="history-clear" disabled={p.running}
                    onClick={() => { setConfirmId(null); setConfirmAll(true); }}>
              Delete all history
            </button>
          ))}
        </footer>
      </aside>
    </>
  );
}
