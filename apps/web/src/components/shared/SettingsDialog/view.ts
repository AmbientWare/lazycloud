export const SETTINGS_VIEWS = ["general", "compute", "domains"] as const;

export type SettingsView = (typeof SETTINGS_VIEWS)[number];

/** The settings section a search param names, or null when it names none. */
export function settingsView(value: unknown): SettingsView | null {
  return SETTINGS_VIEWS.includes(value as SettingsView) ? (value as SettingsView) : null;
}
