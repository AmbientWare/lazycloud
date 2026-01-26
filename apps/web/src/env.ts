import { createEnv } from '@t3-oss/env-core'
import { z } from 'zod'

export const env = createEnv({
  server: {
    // App configuration
    APP_URL: z.url().default('http://localhost:3000'),
    NODE_ENV: z
      .enum(['development', 'test', 'production'])
      .default('development'),

    // API configuration
    API_URL: z.url(),
    API_PREFIX: z.string(),
    ADMIN_API_KEY: z.string(),

    // Database (if needed for direct DB access)
    DATABASE_URL: z.url().optional(),

    // Billing - Polar
    POLAR_ACCESS_TOKEN: z.string(),
    IS_POLAR_SANDBOX: z
      .string()
      .transform((val) => val === 'true')
      .pipe(z.boolean())
      .optional()
      .default(false),

    // Authentication - WorkOS
    WORKOS_CLIENT_ID: z.string(),
    WORKOS_API_KEY: z.string(),
    WORKOS_COOKIE_PASSWORD: z.string(),
    WORKOS_REDIRECT_URI: z.url(),

    // Email
    RESEND_API_KEY: z.string().optional(),
    SUPPORT_EMAIL: z.email(),

    // Rate limiting - Upstash Redis
    UPSTASH_REDIS_REST_URL: z.url().optional(),
    UPSTASH_REDIS_REST_TOKEN: z.string().optional(),
  },

  /**
   * The prefix that client-side variables must have. This is enforced both at
   * a type-level and at runtime.
   */
  clientPrefix: 'VITE_',

  client: {
    VITE_APP_TITLE: z.string().min(1).optional(),
    VITE_WORKOS_API_HOSTNAME: z.string().default('api.workos.com'),
    // Dev auth bypass - use a user's API key to skip WorkOS auth in dev
    VITE_DEV_API_KEY: z.string().optional(),
  },

  /**
   * What object holds the environment variables at runtime.
   * Vite only exposes VITE_ prefixed vars to import.meta.env on client.
   * Server vars need process.env. We merge both for full coverage.
   */
  runtimeEnv: {
    // Server variables from process.env
    APP_URL: process.env.APP_URL,
    NODE_ENV: process.env.NODE_ENV,
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
    VITE_APP_TITLE: import.meta.env.VITE_APP_TITLE,
    VITE_WORKOS_CLIENT_ID: import.meta.env.VITE_WORKOS_CLIENT_ID,
    VITE_WORKOS_API_HOSTNAME: import.meta.env.VITE_WORKOS_API_HOSTNAME,
    VITE_DEV_API_KEY: import.meta.env.VITE_DEV_API_KEY,
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
  skipValidation:
    typeof process !== 'undefined' && !!process.env?.SKIP_ENV_VALIDATION,
})
