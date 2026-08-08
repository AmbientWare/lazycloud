export const SETTINGS_VIEWS = ["general", "compute", "domains"] as const;

export type SettingsView = (typeof SETTINGS_VIEWS)[number];

/** The settings section a search param names, or undefined when it names none.
 *
 * Undefined rather than null because absence is already spelled that way here: the
 * search param is dropped when settings closes, and two spellings of "not open" let
 * a comparison against the wrong one read as always-true.
 */
export function settingsView(value: unknown): SettingsView | undefined {
  return SETTINGS_VIEWS.includes(value as SettingsView) ? (value as SettingsView) : undefined;
}
