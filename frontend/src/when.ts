/* Dates and times of messages and saved conversations, in the reader's own locale and time zone
 * (when they asked - not the data's time zone, which only matters for the figures). */

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** "Today", "Yesterday", "Mon, 12 Oct", or "Mon, 12 Oct 2025" for another year. */
export function dayLabel(ms: number, now: number = Date.now()): string {
  const day = new Date(ms);
  const today = new Date(now);
  if (sameDay(day, today)) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameDay(day, yesterday)) return "Yesterday";
  return day.toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short",
    ...(day.getFullYear() !== today.getFullYear() ? { year: "numeric" } : {}),
  });
}

/** "3:42 PM" */
export function timeLabel(ms: number): string {
  return new Date(ms).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** "Wednesday, 14 October 2026 at 3:42 PM" - for tooltips. */
export function fullLabel(ms: number): string {
  return new Date(ms).toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" });
}

/** For a <time dateTime> attribute. */
export function isoLabel(ms: number): string {
  return new Date(ms).toISOString();
}

export function isSameDay(a: number, b: number): boolean {
  return sameDay(new Date(a), new Date(b));
}
