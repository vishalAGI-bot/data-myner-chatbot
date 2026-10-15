/* Saved conversations.
 *
 * Every conversation is kept in this browser's own database (IndexedDB), so it survives a page reload, a
 * closed tab and the servers being stopped or restarted - and can still be read while they are down.
 * Nothing removes a conversation except the History panel's Delete buttons.
 *
 * Browser rather than server: there are no logins, so a server-side copy would still need a key kept in
 * this browser to know whose conversations are whose - it would be lost exactly when the browser's own
 * copy is. Each /ask reader sees only their own questions.
 *
 * Two stores: `meta` (title, dates, size - what the list needs) and `body` (the messages, which can carry
 * result tables of hundreds of rows), so opening the list never reads every answer. Where IndexedDB is
 * not available (some private windows) the conversations are kept in memory and `historyStorage()` says so.
 */

import type { AgentId, Message } from "./types";

/** The working page and /ask keep separate lists: /ask never shows an answer the working page produced. */
export type HistoryScope = "work" | "ask";

export interface ConversationMeta {
  id: string;
  scope: HistoryScope;
  agent: AgentId;
  title: string;                 // the first question
  createdAt: number;             // ms since 1970, the first message
  updatedAt: number;             // the latest message
  questions: number;             // questions asked in it
}

export interface ConversationBody {
  id: string;
  messages: Message[];
  state: Record<string, unknown>;     // the analyst's follow-up state, so a reopened chat can be continued
}

const DB_NAME = "datamyner-history";
const CHANNEL = "datamyner-history";
const TITLE_CHARS = 140;

let opening: Promise<IDBDatabase | null> | null = null;
const memoryMeta = new Map<string, ConversationMeta>();
const memoryBody = new Map<string, ConversationBody>();

function database(): Promise<IDBDatabase | null> {
  if (!opening) {
    opening = new Promise((resolve) => {
      try {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => {
          const db = request.result;
          db.createObjectStore("meta", { keyPath: "id" }).createIndex("scope", "scope");
          db.createObjectStore("body", { keyPath: "id" });
        };
        request.onsuccess = () => {
          const db = request.result;
          // A newer build upgrading the database in another tab: step aside and reopen on next use.
          db.onversionchange = () => { db.close(); opening = null; };
          resolve(db);
        };
        request.onerror = () => resolve(null);
      } catch {
        resolve(null);
      }
    });
  }
  return opening;
}

function finished(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new DOMException("The save was cancelled.", "AbortError"));
  });
}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** "saved": kept in this browser until deleted. "memory": only until the page closes. */
export async function historyStorage(): Promise<"saved" | "memory"> {
  return (await database()) ? "saved" : "memory";
}

/** Newest first. */
export async function listConversations(scope: HistoryScope): Promise<ConversationMeta[]> {
  const db = await database();
  const all = db
    ? await result(db.transaction("meta").objectStore("meta").index("scope").getAll(scope)) as ConversationMeta[]
    : [...memoryMeta.values()].filter((c) => c.scope === scope);
  return all.sort((a, b) => b.updatedAt - a.updatedAt);
}

export async function loadConversation(id: string): Promise<ConversationBody | null> {
  const db = await database();
  if (!db) return memoryBody.get(id) ?? null;
  return (await result(db.transaction("body").objectStore("body").get(id)) as ConversationBody | undefined) ?? null;
}

export async function saveConversation(meta: ConversationMeta, body: ConversationBody): Promise<void> {
  // A plain-JSON copy: the stored answer is exactly what the API sent, and nothing uncloneable slips in.
  const copy = JSON.parse(JSON.stringify(body)) as ConversationBody;
  const db = await database();
  if (!db) {
    memoryMeta.set(meta.id, meta);
    memoryBody.set(meta.id, copy);
  } else {
    const tx = db.transaction(["meta", "body"], "readwrite");
    tx.objectStore("meta").put(meta);
    tx.objectStore("body").put(copy);
    await finished(tx);
  }
  announce(meta.scope);
}

export async function deleteConversation(scope: HistoryScope, id: string): Promise<void> {
  const db = await database();
  if (!db) {
    memoryMeta.delete(id);
    memoryBody.delete(id);
  } else {
    const tx = db.transaction(["meta", "body"], "readwrite");
    tx.objectStore("meta").delete(id);
    tx.objectStore("body").delete(id);
    await finished(tx);
  }
  announce(scope, [id]);
}

/** Every conversation of one page; the other page's list is left alone. */
export async function deleteAllConversations(scope: HistoryScope): Promise<void> {
  const ids = (await listConversations(scope)).map((c) => c.id);
  const db = await database();
  if (!db) {
    ids.forEach((id) => { memoryMeta.delete(id); memoryBody.delete(id); });
  } else {
    const tx = db.transaction(["meta", "body"], "readwrite");
    ids.forEach((id) => { tx.objectStore("meta").delete(id); tx.objectStore("body").delete(id); });
    await finished(tx);
  }
  announce(scope, "all");
}

/** The list entry for a conversation, worked out from its messages. */
export function describe(id: string, scope: HistoryScope, agent: AgentId, messages: Message[]): ConversationMeta {
  const first = messages.find((m) => m.role === "user")?.content ?? "Conversation";
  const title = first.replace(/\s+/g, " ").trim();
  const now = Date.now();
  return {
    id, scope, agent,
    title: title.length > TITLE_CHARS ? `${title.slice(0, TITLE_CHARS - 1)}…` : title,
    createdAt: messages[0]?.at ?? now,
    updatedAt: messages[messages.length - 1]?.at ?? now,
    questions: messages.filter((m) => m.role === "user").length,
  };
}

export function newConversationId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Ask the browser not to clear this site's storage when the disk runs low. Called on a click: Firefox asks
 *  the user once; Chrome and Safari decide on their own. A refusal still keeps the history as usual. */
export function keepHistory(): void {
  try {
    void navigator.storage?.persisted?.().then((kept) => kept || navigator.storage.persist()).catch(() => undefined);
  } catch {
    /* not supported */
  }
}

// ---- other tabs of this browser: a save or delete in one refreshes the list in the others
let channel: BroadcastChannel | null = null;
try {
  channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null;
} catch {
  channel = null;
}

/** What another tab deleted: those conversations, or all of them. */
export type Deleted = string[] | "all" | undefined;

function announce(scope: HistoryScope, deleted?: Deleted): void {
  try {
    channel?.postMessage({ scope, deleted });
  } catch {
    /* ignore */
  }
}

export function onOtherTabChange(scope: HistoryScope, callback: (deleted: Deleted) => void): () => void {
  if (!channel) return () => undefined;
  const listener = (event: MessageEvent) => { if (event.data?.scope === scope) callback(event.data.deleted); };
  channel.addEventListener("message", listener);
  return () => channel?.removeEventListener("message", listener);
}
