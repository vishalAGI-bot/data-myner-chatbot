import { useMemo, useState, type HTMLAttributes, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ResultTable, ThinkingStep } from "../api/journey";
import type { Message } from "../types";
import ResultChart, { chartSpec } from "./ResultChart";

export default function MessageBubble({ message, caption, onAsk, userMode = false }:
  { message: Message; caption: string; onAsk: (question: string) => void; userMode?: boolean }) {
  const [open, setOpen] = useState(false);
  if (message.role === "user") {
    return (
      <div className="row user">
        <div className="bubble user-bubble"><AnswerText text={message.content} /></div>
      </div>
    );
  }

  const j = message.journey;
  const a = message.analyst;
  const meta = j ?? a;
  const bits: string[] = [];
  if (meta) {
    const secs = (meta.latency_ms / 1000).toFixed(1);
    if (meta.depth === "deep") bits.push(`thought for ${secs}s`);
    else if (meta.latency_ms >= 1000) bits.push(`${secs}s`);
    if (a?.depth === "deep" && a.parts.length) {
      const supported = a.parts.filter((part) => part.verified).length;
      bits.push(`${supported} of ${a.parts.length} parts supported`);
    } else if (j?.depth === "deep" && j.route !== "data_gap") {
      bits.push(j.verified ? "numbers verified" : "not fully verified");
    }
    if (meta.timed_out) {
      bits.push(meta.thinking.some((t) => t.text.includes("stopped by the user")) ? "stopped by you" : "stopped at the time limit");
    }
  }
  if (a && a.tables.length && !userMode) bits.push(`tables: ${a.tables.join(", ")}`);

  const hasDetails = meta?.route !== "data_gap" && Boolean(
    userMode
      ? (a && (a.resolved || a.table?.rows.length || a.parts.length))
      : (j && (j.sql || j.table?.rows.length || j.thinking.length || j.resolved_question)) ||
        (a && (a.sql || a.table || a.thinking.length || a.parts.length)),
  );
  const status = a ? answerStatus(a) : null;
  const scope = a ? friendlyScope(a) : [];
  const answerHasTable = /^\s*\|.+\|\s*\n\s*\|[\s:|-]+\|/m.test(message.content);
  const inlineTable = meta?.route !== "data_gap" && meta?.table && meta.table.rows.length > 0 && !answerHasTable;
  const visibleAnswer = inlineTable ? withoutDuplicatedTableRows(message.content) : message.content;

  return (
    <div className="row assistant">
      <div className={`bubble ${message.error ? "error" : ""}`}><AnswerText text={visibleAnswer} /></div>
      {inlineTable && <TableField table={meta.table!} inline />}
      {(meta?.suggested_questions?.length ?? 0) > 0 && (
        <div className="answer-suggestions" aria-label="Available questions">
          {meta!.suggested_questions.slice(0, 4).map((question) => (
            <button type="button" key={question} onClick={() => onAsk(question)}>{question}</button>
          ))}
        </div>
      )}
      {a && (
        <div className="answer-evidence">
          {status && <span className={`evidence-badge ${status.tone}`}>{status.label}</span>}
          {a.resolved && <span className="scope-label">Understood as: {a.resolved}</span>}
          {scope.map((item) => <span className="scope-chip" key={item}>{item}</span>)}
        </div>
      )}
      <div className="caption">
        {message.error ? "the request did not complete" : userMode ? "answered from your data" : caption}
        {bits.length > 0 && <span> · {bits.join(" · ")}</span>}
        {hasDetails && (
          <button type="button" className="details-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide details" : "How this was calculated"}
          </button>
        )}
      </div>
      {open && j && (
        <div className="details">
          {j.resolved_question && <Field label="Understood as">{j.resolved_question}</Field>}
          <Thinking steps={j.thinking} />
          {j.sql && <Field label="Query"><pre className="code">{j.sql}</pre></Field>}
          {j.table && j.table.rows.length > 0 && <TableField table={j.table} />}
        </div>
      )}
      {open && a && userMode && (
        <div className="details">
          <Field label="Understood as">{a.resolved || "your question"}</Field>
          {scope.length > 0 && <Field label="Applied to">{scope.join(" · ")}</Field>}
          {a.parts.length > 0 && (
            <Field label={`Answered in ${a.parts.length} parts`}>
              <ol className="parts part-cards">
                {a.parts.map((part, i) => (
                  <li key={i} className={part.verified ? "supported" : "unsupported"}>
                    <strong>{part.question}</strong>
                    {part.answer && <div className="part-answer"><AnswerText text={part.answer} /></div>}
                    {part.table && <TableField table={part.table} compact />}
                  </li>
                ))}
              </ol>
            </Field>
          )}
          {a.table && a.parts.length === 0 && <TableField table={a.table} />}
        </div>
      )}
      {open && a && !userMode && (
        <div className="details">
          <Field label="Analysis">
            {a.resolved || a.route}
            {Object.keys(a.params).length > 0 && (
              <span className="muted"> · {Object.entries(a.params).map(([k, v]) => `${k}=${String(v)}`).join(", ")}</span>
            )}
            <span className="muted"> · {a.source === "rules" ? "matched without the language model" : "chosen with the language model"}</span>
          </Field>
          {a.tables.length > 0 && <Field label="Tables used">{a.tables.join(", ")}</Field>}
          <Thinking steps={a.thinking} />
          {a.parts.length > 0 && (
            <Field label={`Parts (${a.parts.length})`}>
              <ol className="parts part-cards">
                {a.parts.map((part, i) => (
                  <li key={i} className={part.verified ? "supported" : "unsupported"}>
                    <strong>{part.question}</strong>
                    <span className="part-status">{part.verified ? "Supported" : "Not supported"}</span>
                    {part.understood_as && <div className="muted">Understood as: {part.understood_as}</div>}
                    {part.answer && <div className="part-answer"><AnswerText text={part.answer} /></div>}
                    {part.sql && <pre className="code">{part.sql}</pre>}
                    {part.table && <TableField table={part.table} compact />}
                  </li>
                ))}
              </ol>
            </Field>
          )}
          {a.sql && a.parts.length === 0 && <Field label="Query"><pre className="code">{a.sql}</pre></Field>}
          {a.table && a.parts.length === 0 && <TableField table={a.table} />}
        </div>
      )}
    </div>
  );
}

function withoutDuplicatedTableRows(text: string): string {
  const lines = text.split("\n");
  const rowPattern = /^\s*\d+\.\s+/;
  if (lines.filter((line) => rowPattern.test(line)).length < 2) return text;
  return lines.filter((line) => !rowPattern.test(line) && !/^\s*Result:\s*$/i.test(line)).join("\n").trim();
}

function Thinking({ steps }: { steps: ThinkingStep[] }) {
  if (!steps.length) return null;
  return (
    <Field label={`Thinking (${steps.length} steps)`}>
      <ol className="think-steps">
        {steps.map((s, i) => (
          <li key={i} className={s.ok ? "" : "warn"}><small>{(s.elapsed_ms / 1000).toFixed(1)}s</small> {s.text}</li>
        ))}
      </ol>
    </Field>
  );
}

function TableField({ table, compact = false, inline = false }:
  { table: ResultTable; compact?: boolean; inline?: boolean }) {
  const [chart, setChart] = useState(false);
  const canChart = useMemo(() => chartSpec(table) !== null, [table]);
  const label = `Result table (${table.total_rows.toLocaleString()}${table.truncated ? "+" : ""} rows)`;
  const vizButton = canChart && (
    <button type="button" className="viz-btn" aria-expanded={chart} onClick={() => setChart(!chart)}>
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 14h12M4 11V7M8 11V3M12 11V6" /></svg>
      {chart ? "Hide chart" : "Visualize"}
    </button>
  );
  const contents = <>
      {!inline && vizButton && <div className="result-tools">{vizButton}</div>}
      {chart && <ResultChart table={table} />}
      {table.rows.length === 0 ? <div className="empty-result">The query ran successfully, but no rows matched.</div> : <div className="table-wrap">
        <table>
          <thead><tr>{table.columns.map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr></thead>
          <tbody>
            {table.rows.slice(0, 200).map((r, i) => (
              <tr key={i}>{r.map((v, k) => (
                <td key={k} className={typeof v === "number" ? "num" : ""}>
                  {v === null || v === undefined ? "" : typeof v === "number" ? v.toLocaleString()
                    : Array.isArray(v) ? v.join(", ") : String(v)}
                </td>
              ))}</tr>
            ))}
          </tbody>
        </table>
      </div>}
      {table.rows.length > 0 && <>
        <div className="table-preview-note">Showing {Math.min(200, table.rows.length).toLocaleString()} of the returned rows{table.truncated ? "; the API result was capped" : ""}.</div>
        {!compact && <button type="button" className="link-btn dark" onClick={() => downloadCsv(table)}>Download returned rows</button>}
      </>}
    </>;
  return inline
    ? <section className="inline-result">
        <div className="result-head"><div className="inline-result-title">{label}</div>{vizButton}</div>
        {contents}
      </section>
    : <Field label={label}>{contents}</Field>;
}

function answerStatus(a: NonNullable<Message["analyst"]>): { label: string; tone: string } {
  if (a.route === "proxy") return { label: "Directional proxy", tone: "warn" };
  if (a.route === "data_gap") return { label: "Source data incomplete", tone: "warn" };
  if (a.route === "clarify") return { label: "Needs clarification", tone: "warn" };
  if (a.route === "out_of_scope") return { label: "Outside data-analysis scope", tone: "warn" };
  if (a.route === "unsupported" || a.route === "help") return { label: "Not supported by this data", tone: "warn" };
  if (a.route === "error" || a.route === "timeout") return { label: "Not verified", tone: "bad" };
  if (a.parts.length) {
    const supported = a.parts.filter((part) => part.verified).length;
    return supported === a.parts.length && supported > 0
      ? { label: "Verified data", tone: "good" }
      : supported > 0 ? { label: "Partial answer", tone: "warn" } : { label: "No supporting rows", tone: "warn" };
  }
  if (a.table && a.table.rows.length === 0) return { label: "No matching rows", tone: "warn" };
  if ((a.route === "metric" || a.route === "sql") && a.verified && a.table?.rows.length) {
    return { label: "Verified data", tone: "good" };
  }
  return { label: "Not verified", tone: "warn" };
}

function friendlyScope(a: NonNullable<Message["analyst"]>): string[] {
  const params = a.params;
  const out: string[] = [];
  if (params.window_hours != null) out.push(`${String(params.window_hours)}h window`);
  for (const key of ["app", "from_app", "to_app"] as const) if (params[key]) out.push(String(params[key]));
  if (params.date_from || params.date_to) out.push(`${String(params.date_from ?? "…")} → ${String(params.date_to ?? "…")}`);
  if (params.category) out.push(`Category: ${String(params.category)}`);
  return out;
}

function AnswerText({ text }: { text: string }) {
  return (
    <div className="rich-answer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre: ({ children }) => <>{children}</>,
          code: MarkdownCode,
          table: ({ children }) => <div className="rich-table-wrap"><table>{children}</table></div>,
          a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
        }}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function MarkdownCode({ className, children }: HTMLAttributes<HTMLElement>) {
  const value = String(children ?? "").replace(/\n$/, "");
  const language = /language-([\w+-]+)/.exec(className ?? "")?.[1] ?? "";
  const block = Boolean(language) || value.includes("\n");
  return block ? <CodeCard code={value} language={language || "text"} />
    : <code className="inline-code">{children}</code>;
}

const CODE_EXTENSIONS: Record<string, string> = {
  python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts",
  json: "json", sql: "sql", bash: "sh", shell: "sh", html: "html", css: "css",
  java: "java", cpp: "cpp", "c++": "cpp", csharp: "cs", "c#": "cs", go: "go", rust: "rs",
};

function CodeCard({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const label = language === "text" ? "Code" : language.charAt(0).toUpperCase() + language.slice(1);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  const download = () => downloadText(code, `answer.${CODE_EXTENSIONS[language.toLowerCase()] ?? "txt"}`, "text/plain");
  return (
    <section className="code-card">
      <div className="code-card-head">
        <span className="code-language"><span aria-hidden="true">&lt;/&gt;</span> {label}</span>
        <span className="code-actions">
          <button type="button" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>
          <button type="button" onClick={download}>Download</button>
        </span>
      </div>
      <pre><code>{code}</code></pre>
    </section>
  );
}

function downloadText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function downloadCsv(table: ResultTable) {
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? "" : Array.isArray(v) ? v.join("; ") : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [table.columns.map(esc).join(","), ...table.rows.map((r) => r.map(esc).join(","))].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "datamyner-result.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <div className="field-label">{label}</div>
      <div>{children}</div>
    </div>
  );
}
