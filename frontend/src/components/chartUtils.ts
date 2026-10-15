/* Pieces both chart views share - colours, number and label formatting, sizing - so the 2D and 3D views of
 * one result always agree on colour, units and wording. */

import { useEffect, useRef, useState } from "react";
import { useTheme } from "../theme";
import type { ChartImage } from "./chartExport";

/** Each chart kind fills this in so the download buttons do not need to know which one is drawn. */
export type Exporter = { current: (() => ChartImage | null) | null };

/* Categorical slots in fixed order, validated against the white result card (dataviz palette checker:
   adjacent CVD ΔE >= 9.1, normal-vision ΔE >= 19.6). Colour follows the entity; a 9th series never gets a
   generated hue, it stays in the table. Three slots are below 3:1 contrast, so every chart keeps its legend
   and the full table view right below it. */
export const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];

/* The same eight hues, slot for slot, stepped for the dark card (--chart-bg #16211c) - the dataviz reference
   palette's dark column, not a flip of the light one. Validator against that surface: every slot >= 3:1, worst
   adjacent CVD ΔE 8.4 (protan), worst normal-vision ΔE 19.3 - all checks pass. A series keeps its slot in
   both themes, so an app is the same hue family whichever theme is on. */
export const SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];

/** The series colours for the theme on screen. */
export function usePalette(): string[] {
  return useTheme() === "dark" ? SERIES_DARK : SERIES;
}

export const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/;
export const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");

export type Spec =
  | { kind: "line"; period: number; groups: number[]; measures: number[] }
  | { kind: "bar"; labels: number[]; measures: number[] };

export function useWidth<T extends HTMLElement>() {
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
export function niceTicks(min: number, max: number, count = 4): number[] {
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

/** Trim a label to the pixels it is given, since SVG text does not clip itself. */
export function clip(text: string, pixels: number): string {
  const max = Math.max(4, Math.floor(pixels / 6.2));
  return text.length > max ? `${text.slice(0, max - 1)}\u2026` : text;
}

export const isPct = (col: string) => /(^|_)pct($|_)|percent/i.test(col);

export function colLabel(col: string): string {
  return col.replace(/_pct$/i, " %").replace(/_/g, " ");
}

export function cellText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "Unknown";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return Array.isArray(v) ? v.join(", ") : String(v);
}

export function fmt(col: string, v: unknown): string {
  if (typeof v !== "number") return v === null || v === undefined ? "–" : String(v);
  const text = v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 });
  return isPct(col) ? `${text}%` : text;
}

export function fmtTick(col: string, v: number): string {
  const text = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(v);
  return isPct(col) ? `${text}%` : text;
}

/** "2026-09-07" -> "Sep 7" (string parsing, so no time-zone shift); long form names the week or month. */
export function fmtPeriod(value: string, col = "", long = false): string {
  const m = ISO_DAY.exec(value);
  if (!m) return value;
  const time = m[4] && `${m[4]}:${m[5]}` !== "00:00" ? ` ${m[4]}:${m[5]}` : "";
  if (long && /^month/i.test(col)) return `${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
  const day = `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}${time}`;
  if (!long) return day;
  return /^week/i.test(col) ? `Week of ${day}, ${m[1]}` : `${day}, ${m[1]}`;
}
