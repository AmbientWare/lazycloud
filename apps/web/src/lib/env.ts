/**
 * The destinations outside this app, read once at build time.
 *
 * The marketing pages link to the examples gallery and the documentation site.
 * The docs are published for every deployment, so that link always exists and a
 * build only names a different host when it has one. The examples gallery does
 * not exist in every deployment, so its entry point is left out entirely when
 * there is nothing behind it: a navigation item nobody can follow costs more
 * than a shorter menu.
 *
 * `import.meta.env` is replaced by Vite at build time, so these are constants in
 * the bundle rather than a runtime lookup — which is also why they are read here
 * once instead of at each call site.
 */

function configuredUrl(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** Where the runnable examples live, or `null` when this build has none. */
export const EXAMPLES_URL: string | null = configuredUrl(
  import.meta.env.VITE_LAZYCLOUD_EXAMPLES_URL,
);

/** Where the documentation lives. The published site unless this build names another. */
export const DOCS_URL: string =
  configuredUrl(import.meta.env.VITE_LAZYCLOUD_DOCS_URL) ?? "https://docs.lazycloud.dev";
