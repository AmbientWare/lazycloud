import { useCallback, useEffect, useState, type ReactNode } from "react";

import {
  DARK_SCHEME_QUERY,
  THEME_STORAGE_KEY,
  ThemeContext,
  type Theme,
} from "@/components/shared/ThemeProvider/theme";

function storedTheme(): Theme | null {
  const value = localStorage.getItem(THEME_STORAGE_KEY);
  return value === "dark" || value === "light" ? value : null;
}

function systemTheme(): Theme {
  return window.matchMedia(DARK_SCHEME_QUERY).matches ? "dark" : "light";
}

/**
 * Light-default theme state backed by a `dark` class on the document element.
 * First visits follow `prefers-color-scheme`; an explicit toggle persists to
 * localStorage and wins over the system preference from then on.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [explicit, setExplicit] = useState<Theme | null>(() =>
    typeof window === "undefined" ? null : storedTheme(),
  );
  const [system, setSystem] = useState<Theme>(() =>
    typeof window === "undefined" ? "light" : systemTheme(),
  );
  const theme = explicit ?? system;

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    if (explicit !== null) return;
    const query = window.matchMedia(DARK_SCHEME_QUERY);
    const followSystem = () => setSystem(query.matches ? "dark" : "light");
    query.addEventListener("change", followSystem);
    return () => query.removeEventListener("change", followSystem);
  }, [explicit]);

  const setTheme = useCallback((next: Theme) => {
    setExplicit(next);
    localStorage.setItem(THEME_STORAGE_KEY, next);
  }, []);

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}
