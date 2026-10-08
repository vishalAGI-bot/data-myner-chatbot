import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { ResultTable } from "../api/journey";
import { esc, fileName, savePng, saveSvg, svgDocument, type ChartImage } from "./chartExport";

/** Each chart kind fills this in so the download buttons do not need to know which one is drawn. */
type Exporter = { current: (() => ChartImage | null) | null };

/* Categorical slots in fixed order, validated against the white result card (dataviz palette checker:
   adjacent CVD ΔE >= 9.1, normal-vision ΔE >= 19.6). Colour follows the entity; a 9th series never gets a
   generated hue, it stays in the table. Three slots are below 3:1 contrast, so every chart keeps its legend
   and the full table view right below it. */
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const MAX_BARS = 30;
const DEFAULT_LINES = 5;            // more lines are one legend click away (series ceiling is 8)
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/;
const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");
const PREFERRED = /(pct|share|rate|avg|average|mean|median|per_)/i;   // the measure a reader usually compares
const NOT_A_MEASURE = /(^id$|_id$|^step$)/i;
const NUMBER_LABEL = /(^|_)(hour|weekday|dow)(_|$)/i;                 // numeric, but a category (hour of day)
/* Dates that bound the span a ROW covers, rather than naming a point on a shared time axis.
   "Active users by app" returns one row per app with its own period_start/period_end, so plotting
   them as a time series draws 14 single-point lines - a chart that looks empty and answers nothing.
   These are the span halves of the backend's period-column list (analyst/agent.py _PERIOD_COLUMNS). */
const SPAN_COLUMN = /^(period_start|period_end|first_day|last_day|week_end|month_end|start_date|end_date)$/i;

type Spec =
  | { kind: "line"; period: number; groups: number[]; measures: number[] }
  | { kind: "bar"; labels: number[]; measures: number[] };

/** The chart a result supports: one line per group over a date column, else bars per category; null = none. */
export function chartSpec(table: ResultTable): Spec | null {
  const { columns, rows } = table;
  if (rows.length < 2) return null;
  const present = (i: number) => rows.map((r) => r[i]).filter((v) => v !== null && v !== undefined);
  const isNumber = (i: number) => present(i).length > 0 && present(i).every((v) => typeof v === "number");
  const isDate = (i: number) => present(i).length > 0
    && present(i).every((v) => typeof v === "string" && ISO_DAY.test(v));
  const all = columns.map((_, i) => i);
  const dates = all.filter(isDate);
  const numberLabels = all.filter((i) => isNumber(i) && NUMBER_LABEL.test(columns[i]));
  let measures = all.filter((i) => isNumber(i) && !NOT_A_MEASURE.test(columns[i]) && !numberLabels.includes(i));
  const varying = measures.filter((i) => new Set(present(i)).size > 1);      // drop constants such as window_hours
  if (varying.length) measures = varying;
  if (!measures.length) return null;
  const text = all.filter((i) => present(i).length > 0 && !isNumber(i) && !dates.includes(i));

  // A date column is a time AXIS only when the rows actually run along it: the same periods recur
  // across groups. One row per entity is a ranking, whatever its date columns say, so it gets bars.
  const groupKey = (r: ResultTable["rows"][number]) => text.map((i) => String(r[i])).join("\u0000");
  const rowsPerGroup = new Map<string, number>();
  for (const r of rows) rowsPerGroup.set(groupKey(r), (rowsPerGroup.get(groupKey(r)) ?? 0) + 1);
  const trend = Math.max(...rowsPerGroup.values()) >= 2;
  const period = dates.find((i) => !SPAN_COLUMN.test(columns[i])
    && new Set(rows.map((r) => String(r[i]))).size >= 2);
  if (trend && period !== undefined) {
    // Every (period, group) must be one row, otherwise a line would silently drop values.
    const keys = new Set(rows.map((r) => [period, ...text].map((i) => String(r[i])).join("\u0000")));
    return keys.size === rows.length ? { kind: "line", period, groups: text, measures } : null;
  }
  const labels = text.length ? text : numberLabels.slice(0, 1);
  return labels.length ? { kind: "bar", labels, measures } : null;
}

export default function ResultChart({ table }: { table: ResultTable }) {
  const spec = useMemo(() => chartSpec(table), [table]);
  const exporter = useRef<(() => ChartImage | null) | null>(null);
  const [failed, setFailed] = useState("");
  // On a ranking, the measure the rows are already ordered by is the one the query ranked on, so it
  // is what the question asked for: "which apps has most users visited" charts users, not the
  // percentage sitting beside it. Lines are ordered by period, so no measure is monotonic there and
  // the PREFERRED rate still wins - which also keeps series colours stable across measure changes.
  const ranked = spec?.kind === "bar" ? spec.measures.find((i) => sortedBy(table.rows, i)) : undefined;
  const preferred = spec
    ? ranked ?? spec.measures.find((i) => PREFERRED.test(table.columns[i])) ?? spec.measures[0]
    : -1;
  const [measure, setMeasure] = useState(preferred);
  if (!spec) return null;
  const columns = table.columns;
  const groups = spec.kind === "line" && spec.groups.length
    ? new Set(table.rows.map((r) => spec.groups.map((g) => cellText(r[g])).join(" · "))) : new Set<string>();
  const by = spec.kind === "line" ? colLabel(columns[spec.period]) : spec.labels.map((i) => colLabel(columns[i])).join(" · ");
  // The saved image has to stand on its own, so it carries the same title the card shows and says
  // when it is not the whole result.
  const heading = colLabel(columns[measure]);
  const title = `${heading.charAt(0).toUpperCase()}${heading.slice(1)} by ${by}`;
  const caption = table.truncated
    ? "The query hit the row limit; the chart shows the returned rows only."
    : spec.kind === "bar" && table.rows.length > MAX_BARS
      ? `First ${MAX_BARS} of ${table.rows.length} rows, in the table's order.` : "";
  const name = fileName([colLabel(columns[measure]), "by", by]);
  return (
    <figure className="result-chart">
      <div className="chart-head">
        {spec.measures.length > 1 ? (
          <label>
            <span className="visually-hidden">Measure </span>
            <select value={measure} onChange={(e) => setMeasure(Number(e.target.value))}>
              {spec.measures.map((i) => <option key={i} value={i}>{colLabel(columns[i])}</option>)}
            </select>
          </label>
        ) : <span className="chart-measure">{colLabel(columns[measure])}</span>}
        <span>by {by}{groups.size === 1 ? ` · ${[...groups][0]}` : ""}</span>
        <span className="chart-actions">
          <DownloadButton format="png" title={title} caption={caption} name={name}
                          exporter={exporter} onError={setFailed} />
          <DownloadButton format="svg" title={title} caption={caption} name={name}
                          exporter={exporter} onError={setFailed} />
        </span>
      </div>
      {spec.kind === "line"
        ? <LineChart table={table} spec={spec} measure={measure} rankBy={preferred} exporter={exporter} />
        : <BarChart table={table} spec={spec} measure={measure} exporter={exporter} />}
      {failed && <figcaption className="chart-note warn-note">{failed}</figcaption>}
      {table.truncated && (
        <figcaption className="chart-note">The query hit the row limit; the chart shows the returned rows only.</figcaption>
      )}
    </figure>
  );
}

function DownloadButton({ format, title, caption, name, exporter, onError }: {
  format: "png" | "svg"; title: string; caption: string; name: string;
  exporter: Exporter; onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const save = async () => {
    const image = exporter.current?.();
    if (!image) return onError("The chart is not ready to download yet. Try again in a moment.");
    const svg = svgDocument(image, title, caption);
    setBusy(true);
    onError("");
    try {
      if (format === "svg") saveSvg(svg, name);
      else await savePng(svg, name);
    } catch (e) {
      onError(e instanceof Error ? e.message : "The chart could not be saved.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <button type="button" className="chart-dl" disabled={busy} onClick={save}
            title={`Download this chart as ${format.toUpperCase()}`}>
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8M4.5 7L8 10.5 11.5 7M3 13h10" /></svg>
      {format.toUpperCase()}
      <span className="visually-hidden"> - download the chart as {format.toUpperCase()}</span>
    </button>
  );
}

// ------------------------------------------------------------------ line chart

interface Series { name: string; values: (number | null)[]; weight: number; color: string }

function LineChart({ table, spec, measure, rankBy, exporter }:
  { table: ResultTable; spec: Extract<Spec, { kind: "line" }>; measure: number; rankBy: number;
    exporter: Exporter }) {
  const { columns, rows } = table;
  const col = columns[measure];
  const periodCol = columns[spec.period];
  const periods = useMemo(() => [...new Set(rows.map((r) => String(r[spec.period])))].sort(), [rows, spec.period]);

  // Series are ranked once, by the default measure, so switching the measure never repaints an app.
  const { series, total } = useMemo(() => {
    const index = new Map(periods.map((p, i) => [p, i]));
    const byName = new Map<string, Omit<Series, "color">>();
    for (const r of rows) {
      const name = spec.groups.length ? spec.groups.map((g) => cellText(r[g])).join(" · ") : colLabel(col);
      let s = byName.get(name);
      if (!s) byName.set(name, s = { name, values: periods.map(() => null), weight: 0 });
      const v = r[measure];
      const w = r[rankBy];
      s.values[index.get(String(r[spec.period]))!] = typeof v === "number" ? v : null;
      if (typeof w === "number") s.weight += Math.abs(w);
    }
    const ranked = [...byName.values()].sort((a, b) => b.weight - a.weight);
    return { series: ranked.slice(0, SERIES.length).map((s, i) => ({ ...s, color: SERIES[i] })), total: ranked.length };
  }, [rows, spec, periods, measure, rankBy, col]);

  const [off, setOff] = useState<Set<string>>(() => new Set(series.slice(DEFAULT_LINES).map((s) => s.name)));
  const [hover, setHover] = useState<number | null>(null);
  const [box, width] = useWidth<HTMLDivElement>();
  const visible = series.filter((s) => !off.has(s.name));
  const single = series.length === 1;

  const nums = visible.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const ticks = niceTicks(Math.min(0, ...nums), Math.max(0, ...nums, nums.length ? 0 : 1));
  const yMin = ticks[0];
  const yMax = ticks[ticks.length - 1];
  const tickText = ticks.map((t) => fmtTick(col, t));
  const H = 250, top = 12, bottom = 26;
  const left = 12 + Math.max(...tickText.map((t) => t.length)) * 6.4;
  const right = single ? 58 : 18;
  const plotW = Math.max(60, width - left - right);
  const plotH = H - top - bottom;
  const x = (i: number) => left + (i * plotW) / (periods.length - 1);
  const y = (v: number) => top + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;
  const labelW = Math.max(42, ...periods.map((p) => fmtPeriod(p).length * 6.6)) + 14;
  const step = Math.ceil(periods.length / Math.max(2, Math.floor(plotW / labelW)));
  const dots = (single && periods.length <= 40) || periods.length <= 8;

  const toggle = (name: string) => setOff((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name);
    else if (series.length - next.size > 1) next.add(name);           // keep at least one line
    return next;
  });
  const pick = (e: PointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
    const i = Math.round(((e.clientX - svg.left - left) / plotW) * (periods.length - 1));
    setHover(Math.max(0, Math.min(periods.length - 1, i)));
  };
  const keys = (e: KeyboardEvent<SVGSVGElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const delta = e.key === "ArrowRight" ? 1 : -1;
      setHover((h) => Math.max(0, Math.min(periods.length - 1, (h ?? periods.length - 1) + delta)));
    } else if (e.key === "Escape") setHover(null);
  };
  const end = single ? lastIndex(series[0].values) : -1;
  const others = spec.measures.filter((i) => i !== measure);
  const rowAt = useMemo(() => new Map(rows.map((r) => [String(r[spec.period]), r])), [rows, spec.period]);
  const covered = useMemo(() => {
    const first = columns.indexOf("first_day");
    const last = columns.indexOf("last_day");
    const out = new Map<string, [string, string]>();
    if (first < 0 || last < 0) return out;
    for (const r of rows) {
      const key = String(r[spec.period]);
      const [a, b] = out.get(key) ?? [String(r[first]), String(r[last])];
      out.set(key, [String(r[first]) < a ? String(r[first]) : a, String(r[last]) > b ? String(r[last]) : b]);
    }
    return out;
  }, [rows, columns, spec.period]);
  const hx = hover === null ? 0 : x(hover);

  // Exporting clones the live SVG rather than redrawing it, so the saved image cannot drift from
  // what is on screen. Only the interactive overlays are dropped, and a legend is added because
  // the on-screen one is HTML outside the SVG.
  const plot = useRef<SVGSVGElement>(null);
  useEffect(() => {
    exporter.current = () => {
      const node = plot.current;
      if (!node) return null;
      const clone = node.cloneNode(true) as SVGSVGElement;
      clone.querySelectorAll("[data-ui]").forEach((el) => el.remove());
      const legend = visible.length > 1 ? legendSvg(visible, width) : "";
      return { body: clone.innerHTML + legend, width, height: H + (legend ? 24 : 4) };
    };
    return () => { exporter.current = null; };
  }, [exporter, visible, width, H]);

  return (
    <>
      <div className="chart-plot" ref={box}>
        <svg height={H} ref={plot} tabIndex={0} role="group" onKeyDown={keys} onBlur={() => setHover(null)}
          onFocus={() => setHover((h) => h ?? periods.length - 1)}
          aria-label={`Line chart of ${colLabel(col)} by ${colLabel(periodCol)}: ${periods.length} periods, `
            + `${visible.length} line${visible.length === 1 ? "" : "s"}. Arrow keys read each period.`}>
          {ticks.map((t, i) => (
            <g key={t}>
              <line className={t === 0 ? "chart-base" : "chart-grid"} x1={left} x2={left + plotW} y1={y(t)} y2={y(t)} />
              <text className="chart-axis" x={left - 8} y={y(t) + 3.5} textAnchor="end">{tickText[i]}</text>
            </g>
          ))}
          {periods.map((p, i) => (i % step === 0 || i === periods.length - 1) && (i === periods.length - 1 || periods.length - 1 - i >= step / 2) && (
            <text key={p} className="chart-axis" x={x(i)} y={H - 8}
              textAnchor={i === 0 ? "start" : i === periods.length - 1 ? "end" : "middle"}>{fmtPeriod(p)}</text>
          ))}
          {single && !series[0].values.includes(null) && (
            <path d={`${linePath(series[0].values, x, y)}L${x(periods.length - 1)},${y(Math.max(yMin, 0))}`
              + `L${x(0)},${y(Math.max(yMin, 0))}Z`} fill={series[0].color} opacity={0.1} />
          )}
          {visible.map((s) => (
            <g key={s.name}>
              <path d={linePath(s.values, x, y)} fill="none" stroke={s.color} strokeWidth={2}
                strokeLinejoin="round" strokeLinecap="round" />
              {dots && s.values.map((v, i) => v !== null && (
                <circle key={i} cx={x(i)} cy={y(v)} r={4} fill={s.color} stroke="#fff" strokeWidth={2} />
              ))}
            </g>
          ))}
          {single && end >= 0 && hover === null && (
            <text className="chart-end" x={x(end) + 9} y={y(series[0].values[end]!) + 4}>{fmt(col, series[0].values[end])}</text>
          )}
          {hover !== null && (
            <g pointerEvents="none" data-ui="hover">
              <line className="chart-cross" x1={hx} x2={hx} y1={top} y2={top + plotH} />
              {visible.map((s) => s.values[hover] !== null && (
                <circle key={s.name} cx={hx} cy={y(s.values[hover]!)} r={4.5} fill={s.color} stroke="#fff" strokeWidth={2} />
              ))}
            </g>
          )}
          <rect x={left} y={top} width={plotW} height={plotH} fill="transparent" data-ui="hit"
            onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setHover(null)} />
        </svg>
        {hover !== null && (
          <div className="chart-tip" style={hx > width * 0.6 ? { right: width - hx + 12, top } : { left: hx + 12, top }}
            role="status">
            <div className="tip-title">{fmtPeriod(periods[hover], periodCol, true)}</div>
            {covered.has(periods[hover]) && (
              <div className="tip-title">data {covered.get(periods[hover])!.map((d) => fmtPeriod(d)).join(" – ")}</div>
            )}
            {visible.map((s) => ({ s, v: s.values[hover] }))
              .sort((a, b) => (b.v ?? -Infinity) - (a.v ?? -Infinity))
              .map(({ s, v }) => (
                <div className="tip-row" key={s.name}>
                  <span className="tip-key" style={{ background: s.color }} />
                  <strong>{fmt(col, v)}</strong>
                  {single ? <span>{colLabel(col)}</span> : <span>{s.name}</span>}
                </div>
              ))}
            {single && others.map((k) => (
              <div className="tip-row" key={k}>
                <span className="tip-key" />
                <strong>{fmt(columns[k], rowAt.get(periods[hover])?.[k])}</strong><span>{colLabel(columns[k])}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      {series.length > 1 && (
        <div className="chart-legend">
          {series.map((s) => (
            <button type="button" key={s.name} className="legend-chip" aria-pressed={!off.has(s.name)}
              onClick={() => toggle(s.name)} title={off.has(s.name) ? "Show this line" : "Hide this line"}>
              <span className="legend-key" style={{ background: s.color }} />{s.name}
            </button>
          ))}
        </div>
      )}
      {series.length > DEFAULT_LINES && (
        <div className="chart-note">
          {total > series.length ? `Top ${series.length} of ${total}` : `All ${total}`}{" "}
          {spec.groups.map((g) => colLabel(columns[g])).join(" · ")} values by {colLabel(columns[rankBy])}; click a name
          to show or hide its line. Every row is in the table below.
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------------- bar chart

function BarChart({ table, spec, measure, exporter }:
  { table: ResultTable; spec: Extract<Spec, { kind: "bar" }>; measure: number; exporter: Exporter }) {
  const { columns } = table;
  const col = columns[measure];
  const rows = table.rows.slice(0, MAX_BARS);
  const [hover, setHover] = useState<number | null>(null);
  const values = rows.map((r) => (typeof r[measure] === "number" ? (r[measure] as number) : null));
  const nums = values.filter((v): v is number => v !== null);
  const lo = Math.min(0, ...nums);
  const span = Math.max(0, ...nums) - lo || 1;
  const zero = (-lo / span) * 100;
  const arrow = spec.labels.length === 2 && /^from/i.test(columns[spec.labels[0]]) && /^to/i.test(columns[spec.labels[1]]);
  const others = spec.measures.filter((i) => i !== measure);
  const [box, width] = useWidth<HTMLDivElement>();

  // Bars are HTML + CSS, so unlike the line chart there is no SVG node to clone: the saved image is
  // drawn from the same rows and the same zero/span scale the bars on screen use.
  const names = rows.map((r) => spec.labels.map((k) => cellText(r[k])).join(arrow ? " \u2192 " : " · "));
  useEffect(() => {
    exporter.current = () => {
      const rowH = 26;
      const labelW = Math.min(230, Math.max(84, Math.round(width * 0.26)));   // .bar-row grid column
      const trackX = labelW + 10;
      const trackW = Math.max(60, width - trackX - 78);
      const body = rows.map((_r, i) => {
        const v = values[i];
        const w = v === null ? 0 : (Math.abs(v) / span) * trackW;
        const negative = v !== null && v < 0;
        const x0 = trackX + (zero / 100) * trackW - (negative ? w : 0);
        const y = i * rowH;
        return `<text class="x-label" x="0" y="${y + 17}">${esc(clip(names[i], labelW))}</text>`
          + `<rect x="${x0.toFixed(1)}" y="${y + 6}" width="${Math.max(w, v === null ? 0 : 1).toFixed(1)}" `
          + `height="16" rx="3" fill="${SERIES[0]}"/>`
          + `<text class="x-value" x="${(negative ? x0 - 6 : x0 + w + 6).toFixed(1)}" y="${y + 18}"`
          + `${negative ? ' text-anchor="end"' : ""}>${esc(fmt(col, v))}</text>`;
      }).join("");
      return { body, width, height: rows.length * rowH + 6 };
    };
    return () => { exporter.current = null; };
  }, [exporter, rows, values, names, span, zero, width, col]);

  return (
    <>
      <div className="bar-chart" role="list" ref={box} aria-label={`Bar chart of ${colLabel(col)}`}>
        {rows.map((r, i) => {
          const v = values[i];
          const name = spec.labels.map((k) => cellText(r[k])).join(arrow ? " → " : " · ");
          const w = v === null ? 0 : (Math.abs(v) / span) * 100;
          const negative = v !== null && v < 0;
          const start = negative ? zero - w : zero;
          return (
            <div key={i} role="listitem" tabIndex={0} className={`bar-row${hover === i ? " on" : ""}`}
              aria-label={`${name}: ${fmt(col, v)}`}
              onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)}
              onFocus={() => setHover(i)} onBlur={() => setHover(null)}>
              <span className="bar-label" title={name}>{name}</span>
              <span className="bar-track">
                <span className={`bar${negative ? " neg" : ""}`}
                  style={{ left: `${start}%`, width: `${w}%`, background: SERIES[0] }} />
                <span className="bar-value" style={negative
                  ? { right: `calc(${100 - start}% + 6px)` } : { left: `calc(${start + w}% + 6px)` }}>{fmt(col, v)}</span>
              </span>
              {hover === i && others.length > 0 && (
                <div className="chart-tip bar-tip" role="status">
                  <div className="tip-title">{name}</div>
                  {[measure, ...others].map((k) => (
                    <div className="tip-row" key={k}><strong>{fmt(columns[k], r[k])}</strong><span>{colLabel(columns[k])}</span></div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {table.rows.length > MAX_BARS && (
        <div className="chart-note">First {MAX_BARS} of {table.rows.length} rows, in the table's order.</div>
      )}
    </>
  );
}

// ----------------------------------------------------------------------- utils

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.floor(entry.contentRect.width))));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Round axis ticks (0 / 5 / 10 ...) covering [min, max]. */
function niceTicks(min: number, max: number, count = 4): number[] {
  const span = max - min || 1;
  const magnitude = 10 ** Math.floor(Math.log10(span / count));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => span / s <= count) ?? 10 * magnitude;
  const ticks: number[] = [];
  for (let v = Math.floor(min / step) * step; ; v += step) {
    ticks.push(Number(v.toPrecision(12)));
    if (v >= max - step * 1e-9) break;
  }
  return ticks;
}

/** One row of swatches for the exported line chart; the on-screen legend is HTML, not SVG. */
function legendSvg(series: Series[], width: number): string {
  let x = 0;
  const parts: string[] = [];
  for (const s of series) {
    const w = s.name.length * 6.2 + 26;
    if (x + w > width && parts.length) break;
    parts.push(`<rect x="${x}" y="${-4}" width="14" height="3" rx="1.5" fill="${s.color}"/>`
      + `<text class="x-label" x="${x + 19}" y="0">${esc(clip(s.name, w))}</text>`);
    x += w;
  }
  return `<g transform="translate(0,${250 + 18})">${parts.join("")}</g>`;
}

/** Trim a label to the pixels it is given, since SVG text does not clip itself. */
function clip(text: string, pixels: number): string {
  const max = Math.max(4, Math.floor(pixels / 6.2));
  return text.length > max ? `${text.slice(0, max - 1)}\u2026` : text;
}

/** Whether every row moves the same way on this column, i.e. the table is ordered by it. */
export function sortedBy(rows: ResultTable["rows"], i: number): boolean {
  const values = rows.map((r) => r[i]);
  if (values.some((v) => typeof v !== "number") || new Set(values).size < 2) return false;
  const nums = values as number[];
  return nums.every((v, k) => k === 0 || v <= nums[k - 1]) || nums.every((v, k) => k === 0 || v >= nums[k - 1]);
}

function linePath(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string {
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (v === null) { pen = false; return; }                         // a missing period breaks the line
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

function lastIndex(values: (number | null)[]): number {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] !== null) return i;
  return -1;
}

const isPct = (col: string) => /(^|_)pct($|_)|percent/i.test(col);

function colLabel(col: string): string {
  return col.replace(/_pct$/i, " %").replace(/_/g, " ");
}

function cellText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "Unknown";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return Array.isArray(v) ? v.join(", ") : String(v);
}

function fmt(col: string, v: unknown): string {
  if (typeof v !== "number") return v === null || v === undefined ? "–" : String(v);
  const text = v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 });
  return isPct(col) ? `${text}%` : text;
}

function fmtTick(col: string, v: number): string {
  const text = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(v);
  return isPct(col) ? `${text}%` : text;
}

/** "2026-09-07" -> "Sep 7" (string parsing, so no time-zone shift); long form names the week or month. */
function fmtPeriod(value: string, col = "", long = false): string {
  const m = ISO_DAY.exec(value);
  if (!m) return value;
  const time = m[4] && `${m[4]}:${m[5]}` !== "00:00" ? ` ${m[4]}:${m[5]}` : "";
  if (long && /^month/i.test(col)) return `${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
  const day = `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}${time}`;
  if (!long) return day;
  return /^week/i.test(col) ? `Week of ${day}, ${m[1]}` : `${day}, ${m[1]}`;
}
