/**
 * The destinations this deployment actually has, read once at build time.
 *
 * The marketing pages link to two places that live outside this app — the
 * examples gallery and the documentation site — and neither exists in every
 * deployment. Rather than render a dead control that says "coming soon", each
 * entry point asks here first and is left out entirely when there is nothing
 * behind it. A navigation item nobody can follow costs more than a shorter menu.
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

/** Where the documentation lives, or `null` when this build has none. */
export const DOCS_URL: string | null = configuredUrl(import.meta.env.VITE_LAZYCLOUD_DOCS_URL);
