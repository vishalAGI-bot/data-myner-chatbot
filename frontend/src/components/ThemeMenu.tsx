/* The colour theme picker in the header (both pages): Light, Dark, or System - which follows this
 * device's own setting and changes with it. See theme.ts. */

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { THEME_CHOICES, type Theme, type ThemeChoice } from "../theme";

const ICONS: Record<ThemeChoice, ReactNode> = {
  light: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" />
    </svg>
  ),
  dark: (
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 9.6A5.6 5.6 0 0 1 6.4 2.5a5.6 5.6 0 1 0 7.1 7.1z" /></svg>
  ),
  system: (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <rect x="1.8" y="2.5" width="12.4" height="8.6" rx="1.4" /><path d="M5.5 13.8h5M8 11.1v2.7" />
    </svg>
  ),
};

export default function ThemeMenu({ choice, theme, onChoose }:
  { choice: ThemeChoice; theme: Theme; onChoose: (choice: ThemeChoice) => void }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const current = THEME_CHOICES.find((c) => c.value === choice)!;

  useEffect(() => {
    if (!open) return;
    items.current[THEME_CHOICES.findIndex((c) => c.value === choice)]?.focus();
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open, choice]);

  const move = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const last = THEME_CHOICES.length - 1;
    const to = e.key === "ArrowDown" ? (i === last ? 0 : i + 1) : e.key === "ArrowUp" ? (i === 0 ? last : i - 1)
      : e.key === "Home" ? 0 : e.key === "End" ? last : null;
    if (to === null) return;
    e.preventDefault();
    items.current[to]?.focus();
  };

  const pick = (value: ThemeChoice) => {
    onChoose(value);
    setOpen(false);
    button.current?.focus();
  };

  return (
    <div className="theme-menu" ref={wrap}>
      <button ref={button} type="button" className="new-chat theme-btn" aria-haspopup="menu" aria-expanded={open}
              aria-label={`Theme: ${current.label}`} title={`Theme: ${current.label}`} onClick={() => setOpen(!open)}>
        {ICONS[choice]}
      </button>
      {open && (
        <div className="theme-list" role="menu" aria-label="Theme">
          {THEME_CHOICES.map((c, i) => (
            <button key={c.value} ref={(el) => { items.current[i] = el; }} type="button" role="menuitemradio"
                    aria-checked={c.value === choice} onKeyDown={(e) => move(e, i)} onClick={() => pick(c.value)}>
              {ICONS[c.value]}
              <span className="theme-text">
                {c.label}
                {c.value === "system" && <small>Follows this device · now {theme}</small>}
              </span>
              {c.value === choice && (
                <svg className="theme-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" /></svg>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
