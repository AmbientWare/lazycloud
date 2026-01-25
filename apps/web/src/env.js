import { createEnv } from "@t3-oss/env-nextjs";
import { z } from "zod";

export const env = createEnv({
  /**
   * Specify your server-side environment variables schema here. This way you can ensure the app
   * isn't built with invalid env vars.
   */
  server: {
    APP_URL: z.url(),
    POLAR_ACCESS_TOKEN: z.string(),
    IS_POLAR_SANDBOX: z.boolean().default(false),
    DATABASE_URL: z.url(),
    API_URL: z.url(),
    API_PREFIX: z.string(),
    ADMIN_API_KEY: z.string(),
    RESEND_API_KEY: z.string(),
    SUPPORT_EMAIL: z.email(),
    WORKOS_CLIENT_ID: z.string(),
    WORKOS_API_KEY: z.string(),
    WORKOS_COOKIE_PASSWORD: z.string(),
    WORKOS_REDIRECT_URI: z.url(),
    UPSTASH_REDIS_REST_URL: z.url().optional(),
    UPSTASH_REDIS_REST_TOKEN: z.string().optional(),
    NODE_ENV: z
      .enum(["development", "test", "production"])
      .default("development"),
    // Dev auth bypass - use a user's API key to skip WorkOS auth
    DEV_API_KEY: z.string().optional(),
  },

  /**
   * Specify your client-side environment variables schema here. This way you can ensure the app
   * isn't built with invalid env vars. To expose them to the client, prefix them with
   * `NEXT_PUBLIC_`.
   */
  client: {},

  /**
   * You can't destruct `process.env` as a regular object in the Next.js edge runtimes (e.g.
   * middlewares) or client-side so we need to destruct manually.
   */
  runtimeEnv: {
    APP_URL: process.env.APP_URL ?? "http://localhost:3000",
    POLAR_ACCESS_TOKEN: process.env.POLAR_ACCESS_TOKEN,
    IS_POLAR_SANDBOX: process.env.IS_POLAR_SANDBOX === "true",
    DATABASE_URL: process.env.DATABASE_URL,
    NODE_ENV: process.env.NODE_ENV,
    API_URL: process.env.API_URL,
    API_PREFIX: process.env.API_PREFIX,
    ADMIN_API_KEY: process.env.ADMIN_API_KEY,
    RESEND_API_KEY: process.env.RESEND_API_KEY,
    SUPPORT_EMAIL: process.env.SUPPORT_EMAIL,
    WORKOS_CLIENT_ID: process.env.WORKOS_CLIENT_ID,
    WORKOS_API_KEY: process.env.WORKOS_API_KEY,
    WORKOS_COOKIE_PASSWORD: process.env.WORKOS_COOKIE_PASSWORD,
    WORKOS_REDIRECT_URI: process.env.WORKOS_REDIRECT_URI,
    UPSTASH_REDIS_REST_URL: process.env.UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN: process.env.UPSTASH_REDIS_REST_TOKEN,
    DEV_API_KEY: process.env.DEV_API_KEY,
  },
  /**
   * Run `build` or `dev` with `SKIP_ENV_VALIDATION` to skip env validation. This is especially
   * useful for Docker builds.
   */
  skipValidation: !!process.env.SKIP_ENV_VALIDATION,
  /**
   * Makes it so that empty strings are treated as undefined. `SOME_VAR: z.string()` and
   * `SOME_VAR=''` will throw an error.
   */
  emptyStringAsUndefined: true,
});
