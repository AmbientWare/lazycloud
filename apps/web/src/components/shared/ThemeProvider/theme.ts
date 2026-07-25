import { createContext, useContext } from "react";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "lazycloud_web_theme";
export const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";

/**
 * Pre-hydration script injected into the document head. It applies the
 * resolved theme class before first paint so a stored dark preference never
 * flashes the light default under SPA prerendering.
 */
export const themeInitScript = `(function () {
  try {
    var stored = localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
    var dark =
      stored === "dark" ||
      (stored !== "light" && window.matchMedia(${JSON.stringify(DARK_SCHEME_QUERY)}).matches);
    document.documentElement.classList.toggle("dark", dark);
  } catch (error) {}
})();`;

export type ThemeContextValue = {
  theme: Theme;
  setTheme: (next: Theme) => void;
};

export const ThemeContext = createContext<ThemeContextValue | null>(null);

/** Resolved theme plus the explicit setter; requires `ThemeProvider`. */
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
