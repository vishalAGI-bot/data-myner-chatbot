// Per-browser preferences only (depth, time limit, toggles). Storage can be
// unavailable (private mode, blocked cookies), so every access is guarded.

export function load<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(`datamyner:${key}`);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function save(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(`datamyner:${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}
