/* The 3D view of a result: two dimensions on the floor, the value as height.
 *
 * - One row per app (a ranking with several measures): apps x measures, each measure scaled to its own largest
 *   value so they can stand side by side - the tooltip and the table carry the exact figures.
 * - Two dimensions (segment x app, day x app): one along each floor axis, the chosen measure as height.
 *
 * Drawn in SVG with an orthographic (not perspective) projection, so equal heights look equal from every angle,
 * and painted back to front. It rotates (drag or arrow keys); any series can be hidden so a tall column never
 * hides a short one for good. Colours follow the entity, as in the 2D view.
 */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { ResultTable } from "../api/journey";
import { esc } from "./chartExport";
import {
  cellText, clip, colLabel, type Exporter, fmt, fmtPeriod, fmtTick, niceTicks, SERIES, type Spec, useWidth,
} from "./chartUtils";

const MAX_X = 24;                 // columns along the floor; more stay in the table
const MAX_MEASURES = SERIES.length;   // one validated colour per measure
const H = 360;
const GAP = 0.16;                 // share of a cell left empty around each column (the surface gap)
const HOME = { yaw: -38, pitch: 28 };

export interface Grid3D {
  xs: string[];
  zs: { name: string; color: string; column: string }[];
  values: (number | null)[][];          // [z][x] - the real figures
  heights: (number | null)[][];         // [z][x] - drawn height; 1 = the top tick
  ticks: { at: number; label: string }[];
  xTitle: string;
  zTitle: string;
  title: string;
  scaled: boolean;                      // several measures, each scaled to its own maximum
  notes: string[];
}

/** The two-dimensional view a result supports, or null when a 3D view would add nothing. */
export function buildGrid(table: ResultTable, spec: Spec, measure: number, rankBy: number,
                          palette: string[] = SERIES): Grid3D | null {
  const { columns, rows } = table;
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  if (spec.kind === "line" || spec.labels.length >= 2) {
    // Two dimensions: periods (or the first label) along x, groups (or the second label) as series.
    const xKey = (r: ResultTable["rows"][number]) =>
      spec.kind === "line" ? String(r[spec.period]) : cellText(r[spec.labels[0]]);
    const zKey = (r: ResultTable["rows"][number]) => spec.kind === "line"
      ? spec.groups.map((g) => cellText(r[g])).join(" · ")
      : spec.labels.slice(1).map((g) => cellText(r[g])).join(" · ");
    if (spec.kind === "line" && !spec.groups.length) return null;          // one line: nothing to add
    let xs = [...new Set(rows.map(xKey))];
    if (spec.kind === "line") xs = xs.sort().slice(-MAX_X);                 // the most recent periods
    const allX = xs.length;
    xs = xs.slice(0, MAX_X);
    // Series ranked the way the 2D view ranks them, so an app keeps its colour in both views.
    const weight = new Map<string, number>();
    for (const r of rows) weight.set(zKey(r), (weight.get(zKey(r)) ?? 0) + Math.abs(num(r[rankBy]) ?? 0));
    const ranked = [...weight.keys()].sort((a, b) => weight.get(b)! - weight.get(a)!);
    if (ranked.length < 2) return null;
    const zs = ranked.slice(0, palette.length).map((name, i) => ({ name, color: palette[i], column: columns[measure] }));
    const at = new Map(rows.map((r) => [`${zKey(r)}\u0000${xKey(r)}`, num(r[measure])]));
    const values = zs.map((z) => xs.map((x) => at.get(`${z.name}\u0000${x}`) ?? null));
    const flat = values.flat().filter((v): v is number => v !== null);
    const ticks = niceTicks(Math.min(0, ...flat), Math.max(0, ...flat, flat.length ? 0 : 1));
    const span = (ticks[ticks.length - 1] - ticks[0]) || 1;
    const xTitle = colLabel(columns[spec.kind === "line" ? spec.period : spec.labels[0]]);
    const zTitle = (spec.kind === "line" ? spec.groups : spec.labels.slice(1)).map((g) => colLabel(columns[g])).join(" · ");
    return {
      xs: spec.kind === "line" ? xs.map((x) => fmtPeriod(x)) : xs, zs, values,
      heights: values.map((row) => row.map((v) => (v === null ? null : v / span))),
      ticks: ticks.map((t) => ({ at: t / span, label: fmtTick(columns[measure], t) })),
      xTitle, zTitle, scaled: false,
      title: `${colLabel(columns[measure])} by ${xTitle} and ${zTitle}`,
      notes: [allX > MAX_X ? `${spec.kind === "line" ? "Last" : "First"} ${MAX_X} of ${allX} ${xTitle} values.` : "",
              ranked.length > zs.length ? `Top ${zs.length} of ${ranked.length} ${zTitle} values.` : ""]
        .filter(Boolean),
    };
  }
  // One row per entity with several measures: entities along x, measures as series.
  const measures = spec.measures.slice(0, MAX_MEASURES);
  if (measures.length < 2) return null;
  const shown = rows.slice(0, MAX_X);
  const xs = shown.map((r) => spec.labels.map((k) => cellText(r[k])).join(" · "));
  const zs = measures.map((m, i) => ({ name: colLabel(columns[m]), color: palette[i], column: columns[m] }));
  const values = measures.map((m) => shown.map((r) => num(r[m])));
  const heights = values.map((row) => {
    const top = Math.max(...row.map((v) => Math.abs(v ?? 0))) || 1;
    return row.map((v) => (v === null ? null : v / top));
  });
  const xTitle = spec.labels.map((k) => colLabel(columns[k])).join(" · ");
  return {
    xs, zs, values, heights, xTitle, zTitle: "measure", scaled: true,
    ticks: [0, 0.5, 1].map((at) => ({ at, label: at === 1 ? "max" : at ? "half" : "0" })),
    title: `${xTitle}: ${measures.length} measures side by side`,
    notes: ["Each measure is scaled to its own largest value so they can stand side by side; hover a column, or "
            + "read the table, for the exact figures.",
            rows.length > MAX_X ? `First ${MAX_X} of ${rows.length} rows.` : "",
            spec.measures.length > MAX_MEASURES ? `First ${MAX_MEASURES} of ${spec.measures.length} measures.` : ""]
      .filter(Boolean),
  };
}

// ------------------------------------------------------------------ geometry

type Pt = { x: number; y: number };

function shade(hex: string, factor: number): string {
  const n = parseInt(hex.slice(1), 16);
  const mix = (c: number) => Math.round(factor >= 1 ? c + (255 - c) * (factor - 1) : c * factor);
  return `rgb(${mix((n >> 16) & 255)},${mix((n >> 8) & 255)},${mix(n & 255)})`;
}

const poly = (pts: Pt[]) => pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

export default function Chart3D({ grid, exporter }: { grid: Grid3D; exporter: Exporter }) {
  const [view, setView] = useState(HOME);
  const [off, setOff] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<{ z: number; x: number; px: number; py: number } | null>(null);
  const [box, width] = useWidth<HTMLDivElement>();
  const drag = useRef<{ x: number; y: number; yaw: number; pitch: number } | null>(null);
  const plot = useRef<SVGSVGElement>(null);

  const visible = grid.zs.map((z, i) => ({ ...z, i })).filter((z) => !off.has(z.name));
  const nx = grid.xs.length;
  const nz = Math.max(1, visible.length);
  const t = (view.yaw * Math.PI) / 180;
  const p = (view.pitch * Math.PI) / 180;
  const cos = Math.cos(t), sin = Math.sin(t);

  // Fit the rotated floor and the tallest column into the box.
  const layout = useMemo(() => {
    const corners = [[0, 0], [nx, 0], [0, nz], [nx, nz]].map(([x, z]) => {
      const dx = x - nx / 2, dz = z - nz / 2;
      return { rx: dx * cos - dz * sin, rz: dx * sin + dz * cos };
    });
    const rxMin = Math.min(...corners.map((c) => c.rx)), rxMax = Math.max(...corners.map((c) => c.rx));
    const rzMin = Math.min(...corners.map((c) => c.rz)), rzMax = Math.max(...corners.map((c) => c.rz));
    const barPx = H * 0.4;
    const left = 64, right = 64, top = 26, bottom = 46;
    const scale = Math.min((width - left - right) / Math.max(rxMax - rxMin, 0.5),
                           (H - top - bottom - barPx * Math.cos(p)) / Math.max((rzMax - rzMin) * Math.sin(p), 0.5));
    const ox = left + (width - left - right) / 2 - ((rxMin + rxMax) / 2) * scale;
    const oy = H - bottom + rzMin * scale * Math.sin(p);
    return { scale, ox, oy, barPx };
  }, [nx, nz, cos, sin, p, width]);

  const proj = (x: number, y: number, z: number): Pt => {
    const dx = x - nx / 2, dz = z - nz / 2;
    const rx = dx * cos - dz * sin, rz = dx * sin + dz * cos;
    return { x: layout.ox + rx * layout.scale, y: layout.oy - y * layout.barPx * Math.cos(p) - rz * layout.scale * Math.sin(p) };
  };
  const depth = (x: number, z: number) => (x - nx / 2) * sin + (z - nz / 2) * cos;

  // Columns, painted back to front; the faces that face the viewer, then the top.
  const bars: { z: number; x: number; d: number }[] = [];
  visible.forEach((zv, k) => grid.heights[zv.i].forEach((h, x) => {
    if (h !== null) bars.push({ z: k, x, d: depth(x + 0.5, k + 0.5) });
  }));
  bars.sort((a, b) => b.d - a.d);

  const column = (k: number, x: number) => {
    const zv = visible[k];
    const h = grid.heights[zv.i][x]!;
    const [y0, y1] = h >= 0 ? [0, h] : [h, 0];
    const x0 = x + GAP, x1 = x + 1 - GAP, z0 = k + GAP, z1 = k + 1 - GAP;
    const on = hover?.z === zv.i && hover.x === x;
    const lift = on ? 1.22 : 1;
    const faces: { pts: Pt[]; fill: string }[] = [];
    const side = (pts: [number, number, number][], tone: number) =>
      faces.push({ pts: pts.map(([a, b, c]) => proj(a, b, c)), fill: shade(zv.color, tone * lift) });
    if (-cos < 0) side([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0]], 0.72);   // faces -z
    if (cos < 0) side([[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], 0.72);    // faces +z
    if (-sin < 0) side([[x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0]], 0.86);   // faces -x
    if (sin < 0) side([[x1, y0, z0], [x1, y0, z1], [x1, y1, z1], [x1, y1, z0]], 0.86);    // faces +x
    side([[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]], 1);
    return faces;
  };

  // Labels sit along the floor edges nearest the viewer. They are placed greedily - every row label first,
  // then the series labels (which the legend also names) - and a label that would overlap one already placed
  // is left out, so text never collides.
  const xEdge = depth(nx / 2, 0) < depth(nx / 2, nz) ? 0 : nz;
  const zEdge = depth(0, nz / 2) < depth(nx, nz / 2) ? 0 : nx;
  const centre = proj(nx / 2, 0, nz / 2);
  const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
  const edgeLabels = (prefix: string, at: (i: number) => Pt, names: string[]) => names.map((name, i) => {
    const a = at(i);
    const dx = a.x - centre.x, dy = a.y - centre.y, len = Math.hypot(dx, dy) || 1;
    const lx = a.x + (dx / len) * 12, ly = a.y + (dy / len) * 12 + 4;
    const anchor = Math.abs(dx / len) < 0.25 ? "middle" : dx > 0 ? "start" : "end";
    const text = clip(name, 110);
    const w = text.length * 5.9;
    const x0 = anchor === "start" ? lx : anchor === "end" ? lx - w : lx - w / 2;
    const b = { x0, x1: x0 + w, y0: ly - 8, y1: ly + 1 };          // the glyphs, not the line box
    if (placed.some((o) => b.x0 < o.x1 + 3 && o.x0 < b.x1 + 3 && b.y0 < o.y1 && o.y0 < b.y1)) return null;
    placed.push(b);
    return <text key={`${prefix}${i}`} className="chart-axis" x={lx} y={ly} textAnchor={anchor}>{text}</text>;
  });
  // The height axis stands at the leftmost floor corner: nothing lies further left, so its numbers never sit
  // over a column. Row and series labels are placed first; a tick that would touch one (usually the floor's
  // "0", which the floor itself already shows) is left out.
  const corners = [[0, 0], [nx, 0], [0, nz], [nx, nz]].map(([x, z]) => ({ x, z, pt: proj(x, 0, z) }));
  const axis = corners.reduce((a, b) => (b.pt.x < a.pt.x ? b : a));
  const rowLabels = edgeLabels("x", (i) => proj(i + 0.5, 0, xEdge), grid.xs);
  const seriesLabels = edgeLabels("z", (k) => proj(zEdge, 0, k + 0.5), visible.map((z) => z.name));
  // Seen from almost straight above the heights are flat, so the height axis would only be a pile of
  // numbers. Ticks are placed top first and skip any that would touch one already placed.
  const ticks = view.pitch > 70 ? [] : [...grid.ticks].reverse().filter((tick) => {
    const at = proj(axis.x, tick.at, axis.z);
    const w = tick.label.length * 5.9;
    const b = { x0: at.x - 7 - w, x1: at.x, y0: at.y - 7, y1: at.y + 3 };
    if (placed.some((o) => b.x0 < o.x1 && o.x0 < b.x1 && b.y0 < o.y1 && o.y0 < b.y1)) return false;
    placed.push(b);
    return true;
  });

  const begin = (e: PointerEvent<SVGSVGElement>) => {
    drag.current = { x: e.clientX, y: e.clientY, ...view };
    e.currentTarget.setPointerCapture(e.pointerId);
    setHover(null);
  };
  const move = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    setView({ yaw: d.yaw + (e.clientX - d.x) * 0.45,
              pitch: Math.max(8, Math.min(82, d.pitch - (e.clientY - d.y) * 0.3)) });
  };
  const end = () => { drag.current = null; };
  const keys = (e: KeyboardEvent<SVGSVGElement>) => {
    const turn: Record<string, [number, number]> = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, 6], ArrowDown: [0, -6] };
    if (turn[e.key]) {
      e.preventDefault();
      setView((v) => ({ yaw: v.yaw + turn[e.key][0], pitch: Math.max(8, Math.min(82, v.pitch + turn[e.key][1])) }));
    } else if (e.key === "Escape") setHover(null);
  };
  const toggle = (name: string) => setOff((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name);
    else if (grid.zs.length - next.size > 1) next.add(name);             // keep at least one series
    return next;
  });

  // The download clones what is on screen (without the hover layer) and adds a legend.
  useEffect(() => {
    exporter.current = () => {
      const node = plot.current;
      if (!node) return null;
      const clone = node.cloneNode(true) as SVGSVGElement;
      clone.querySelectorAll("[data-ui]").forEach((el) => el.remove());
      let lx = 0, ly = 0;
      const legend = visible.map((z) => {
        const w = z.name.length * 6.2 + 26;
        if (lx > 0 && lx + w > width) { lx = 0; ly += 18; }         // wrap rather than run off the image
        const part = `<rect x="${lx}" y="${ly - 8}" width="10" height="10" rx="2" fill="${z.color}"/>`
          + `<text class="x-label" x="${lx + 15}" y="${ly + 1}">${esc(clip(z.name, w))}</text>`;
        lx += w;
        return part;
      }).join("");
      return { body: clone.innerHTML + `<g transform="translate(0,${H + 16})">${legend}</g>`, width,
               height: H + 24 + ly };
    };
    return () => { exporter.current = null; };
  }, [exporter, visible, width]);

  const tipZ = hover ? grid.zs[hover.z] : null;
  return (
    <>
      <div className="chart-3d-tools" data-ui="tools">
        <span>Drag or use the arrow keys to turn it.</span>
        <button type="button" onClick={() => setView((v) => ({ ...v, yaw: v.yaw - 45 }))}>Turn left</button>
        <button type="button" onClick={() => setView((v) => ({ ...v, yaw: v.yaw + 45 }))}>Turn right</button>
        <button type="button" onClick={() => setView((v) => ({ ...v, pitch: 65 }))}>From above</button>
        <button type="button" onClick={() => setView(HOME)}>Reset view</button>
      </div>
      <div className="chart-plot" ref={box}>
        <svg height={H} ref={plot} tabIndex={0} role="group" className="chart-3d" onKeyDown={keys}
          onPointerDown={begin} onPointerMove={move} onPointerUp={end} onPointerCancel={end}
          aria-label={`3D chart: ${grid.title}. ${nx} by ${visible.length} columns. Arrow keys turn it; the table `
            + "below has every value."}>
          {/* floor */}
          {Array.from({ length: nx + 1 }, (_, i) => {
            const a = proj(i, 0, 0), b = proj(i, 0, nz);
            return <line key={`x${i}`} className={i === 0 || i === nx ? "chart-base" : "chart-grid"} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />;
          })}
          {Array.from({ length: nz + 1 }, (_, k) => {
            const a = proj(0, 0, k), b = proj(nx, 0, k);
            return <line key={`z${k}`} className={k === 0 || k === nz ? "chart-base" : "chart-grid"} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />;
          })}
          {/* height axis at the leftmost corner (its numbers are drawn after the columns) */}
          {ticks.length > 0 && (() => {
            const a = proj(axis.x, grid.ticks[0].at, axis.z), b = proj(axis.x, grid.ticks[grid.ticks.length - 1].at, axis.z);
            return <line className="chart-base" x1={a.x} y1={a.y} x2={b.x} y2={b.y} />;
          })()}
          {rowLabels}
          {seriesLabels}
          {/* columns */}
          {bars.map(({ z, x }) => {
            const zv = visible[z];
            return (
              <g key={`${zv.i}-${x}`} onPointerEnter={(e) => !drag.current && setHover({
                z: zv.i, x, ...localPoint(e, box.current) })}
                onPointerMove={(e) => !drag.current && setHover({ z: zv.i, x, ...localPoint(e, box.current) })}
                onPointerLeave={() => setHover(null)}>
                {column(z, x).map((f, i) => (
                  <polygon key={i} points={poly(f.pts)} fill={f.fill} stroke={f.fill} strokeWidth={0.5}
                    strokeLinejoin="round" />
                ))}
              </g>
            );
          })}
          {ticks.map((tick) => {
            const at = proj(axis.x, tick.at, axis.z);
            return (
              <g key={tick.label}>
                <line className="chart-base" x1={at.x - 4} x2={at.x} y1={at.y} y2={at.y} />
                <text className="chart-axis chart-ring" x={at.x - 7} y={at.y + 3.5} textAnchor="end" strokeWidth={3}
                  strokeLinejoin="round" paintOrder="stroke">{tick.label}</text>
              </g>
            );
          })}
        </svg>
        {hover && tipZ && (
          <div className="chart-tip" role="status"
            style={hover.px > width * 0.6 ? { right: width - hover.px + 12, top: Math.max(0, hover.py - 20) }
              : { left: hover.px + 12, top: Math.max(0, hover.py - 20) }}>
            <div className="tip-title">{grid.xs[hover.x]}</div>
            {(grid.scaled ? grid.zs : [tipZ]).map((z, i) => {
              const zi = grid.scaled ? i : hover.z;
              return (
                <div className="tip-row" key={z.name}>
                  <span className="tip-key" style={{ background: z.color }} />
                  <strong>{fmt(z.column, grid.values[zi][hover.x])}</strong>
                  <span>{grid.scaled ? z.name : `${z.name} · ${colLabel(z.column)}`}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className="chart-legend">
        {grid.zs.map((z) => (
          <button type="button" key={z.name} className="legend-chip" aria-pressed={!off.has(z.name)}
            onClick={() => toggle(z.name)} title={off.has(z.name) ? "Show these columns" : "Hide these columns"}>
            <span className="legend-key" style={{ background: z.color }} />{z.name}
          </button>
        ))}
      </div>
      {grid.notes.map((note) => <div className="chart-note" key={note}>{note}</div>)}
    </>
  );
}

function localPoint(e: PointerEvent<SVGGElement>, el: HTMLElement | null): { px: number; py: number } {
  const r = el?.getBoundingClientRect();
  return { px: e.clientX - (r?.left ?? 0), py: e.clientY - (r?.top ?? 0) };
}
