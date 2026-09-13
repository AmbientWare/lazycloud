/// <reference types="vite/client" />

/**
 * Build-time configuration this app reads, declared so a typo is a type error.
 *
 * Vite's own `ImportMetaEnv` allows any key, which means a misspelt variable
 * reads as `undefined` and the feature it gates silently stays off. Naming them
 * here is what turns that into a failed build instead.
 */
interface ImportMetaEnv {
  /** Where the documentation lives. Absent, the build links the published site. */
  readonly VITE_LAZYCLOUD_DOCS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
