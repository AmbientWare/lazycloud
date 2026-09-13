function configuredUrl(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** Where the documentation lives. The published site unless this build names another. */
export const DOCS_URL: string =
  configuredUrl(import.meta.env.VITE_LAZYCLOUD_DOCS_URL) ?? "https://docs.lazycloud.dev";

export const EXAMPLES_URL = `${DOCS_URL.replace(/\/$/, "")}/examples`;
