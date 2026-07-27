import { c as createEnv } from "../_chunks/_libs/@t3-oss/env-core.mjs";
import { k as string, x as url, q as boolean, _ as _enum, y as email } from "../_libs/zod.mjs";
const env = createEnv({
  server: {
    // App configuration
    APP_URL: url().default("http://localhost:3000"),
    NODE_ENV: _enum(["development", "test", "production"]).default("development"),
    // API configuration
    API_URL: url(),
    API_PREFIX: string(),
    ADMIN_API_KEY: string(),
    // Database (if needed for direct DB access)
    DATABASE_URL: url().optional(),
    // Billing - Polar
    POLAR_ACCESS_TOKEN: string(),
    IS_POLAR_SANDBOX: string().transform((val) => val === "true").pipe(boolean()).optional().default(false),
    // Authentication - WorkOS
    WORKOS_CLIENT_ID: string(),
    WORKOS_API_KEY: string(),
    WORKOS_COOKIE_PASSWORD: string(),
    WORKOS_REDIRECT_URI: url(),
    // Email
    RESEND_API_KEY: string().optional(),
    SUPPORT_EMAIL: email(),
    // Rate limiting - Upstash Redis
    UPSTASH_REDIS_REST_URL: url().optional(),
    UPSTASH_REDIS_REST_TOKEN: string().optional()
  },
  /**
   * The prefix that client-side variables must have. This is enforced both at
   * a type-level and at runtime.
   */
  clientPrefix: "VITE_",
  client: {
    VITE_APP_TITLE: string().min(1).optional(),
    VITE_WORKOS_CLIENT_ID: string(),
    VITE_WORKOS_API_HOSTNAME: string().default("api.workos.com"),
    // Dev auth bypass - use a user's API key to skip WorkOS auth in dev
    VITE_DEV_API_KEY: string().optional()
  },
  /**
   * What object holds the environment variables at runtime.
   * Vite only exposes VITE_ prefixed vars to import.meta.env on client.
   * Server vars need process.env. We merge both for full coverage.
   */
  runtimeEnv: {
    // Server variables from process.env
    APP_URL: process.env.APP_URL,
    NODE_ENV: "production",
    API_URL: process.env.API_URL,
    API_PREFIX: process.env.API_PREFIX,
    ADMIN_API_KEY: process.env.ADMIN_API_KEY,
    DATABASE_URL: process.env.DATABASE_URL,
    POLAR_ACCESS_TOKEN: process.env.POLAR_ACCESS_TOKEN,
    IS_POLAR_SANDBOX: process.env.IS_POLAR_SANDBOX,
    WORKOS_CLIENT_ID: process.env.WORKOS_CLIENT_ID,
    WORKOS_API_KEY: process.env.WORKOS_API_KEY,
    WORKOS_COOKIE_PASSWORD: process.env.WORKOS_COOKIE_PASSWORD,
    WORKOS_REDIRECT_URI: process.env.WORKOS_REDIRECT_URI,
    RESEND_API_KEY: process.env.RESEND_API_KEY,
    SUPPORT_EMAIL: process.env.SUPPORT_EMAIL,
    UPSTASH_REDIS_REST_URL: process.env.UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN: process.env.UPSTASH_REDIS_REST_TOKEN,
    // Client variables from import.meta.env (VITE_ prefix)
    VITE_APP_TITLE: void 0,
    VITE_WORKOS_CLIENT_ID: "client_01KC01716899961TAXH344SHJG",
    VITE_WORKOS_API_HOSTNAME: "api.workos.com",
    VITE_DEV_API_KEY: "sk_3666749f7f704f05562a1a1da6f748fc09189b1b1eb9c86bab70d366fe3775b2"
  },
  /**
   * By default, this library will feed the environment variables directly to
   * the Zod validator.
   *
   * This means that if you have an empty string for a value that is supposed
   * to be a number (e.g. `PORT=` in a ".env" file), Zod will incorrectly flag
   * it as a type mismatch violation. Additionally, if you have an empty string
   * for a value that is supposed to be a string with a default value (e.g.
   * `DOMAIN=` in an ".env" file), the default value will never be applied.
   *
   * In order to solve these issues, we recommend that all new projects
   * explicitly specify this option as true.
   */
  emptyStringAsUndefined: true,
  /**
   * Skip validation during build. This is useful for Docker builds where
   * env vars are injected at runtime.
   */
  skipValidation: typeof process !== "undefined" && !!process.env?.SKIP_ENV_VALIDATION
});
export {
  env as e
};
