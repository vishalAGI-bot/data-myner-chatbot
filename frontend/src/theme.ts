/* Colour theme: Light, Dark, or System (follow this device's setting, and change with it).
 *
 * The choice is a per-browser preference like depth and time limit (storage.ts); the working page and /ask
 * keep their own, as /ask never reads or changes the working page's settings. The resolved theme is set as
 * <html data-theme="light|dark">, which styles.css themes through its tokens. index.html sets the same
 * attribute before the first paint, so a dark page never flashes light while the app loads - keep its key
 * and logic in step with this file.
 */

import { createContext, useContext, useEffect, useState } from "react";
import { load, save } from "./storage";

export type ThemeChoice = "system" | "light" | "dark";
export type Theme = "light" | "dark";

export const THEME_CHOICES: { value: ThemeChoice; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

const DARK_QUERY = "(prefers-color-scheme: dark)";

function deviceTheme(): Theme {
  try {
    return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function valid(value: unknown): ThemeChoice {
  return value === "light" || value === "dark" ? value : "system";
}

/** The saved choice for one page (`theme:work` or `theme:ask`), the device's own setting, and what is shown. */
export function useThemeChoice(key: string): { choice: ThemeChoice; setChoice: (c: ThemeChoice) => void; theme: Theme } {
  const [choice, setChoice] = useState<ThemeChoice>(() => valid(load(key, "system")));
  const [device, setDevice] = useState<Theme>(deviceTheme);
  const theme = choice === "system" ? device : choice;

  useEffect(() => {
    let query: MediaQueryList;
    try {
      query = window.matchMedia(DARK_QUERY);
    } catch {
      return;
    }
    const follow = () => setDevice(query.matches ? "dark" : "light");
    query.addEventListener("change", follow);
    return () => query.removeEventListener("change", follow);
  }, []);

  // Another tab of this page changed the theme: follow it here too.
  useEffect(() => {
    const other = (e: StorageEvent) => {
      if (e.key !== `datamyner:${key}`) return;
      try {
        setChoice(valid(JSON.parse(e.newValue ?? "null")));
      } catch {
        setChoice("system");
      }
    };
    window.addEventListener("storage", other);
    return () => window.removeEventListener("storage", other);
  }, [key]);

  useEffect(() => { save(key, choice); }, [key, choice]);
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);

  return { choice, setChoice, theme };
}

/** The theme on screen, for the parts that pick colours in code (the charts). */
export const ThemeContext = createContext<Theme>("light");

export function useTheme(): Theme {
  return useContext(ThemeContext);
}
