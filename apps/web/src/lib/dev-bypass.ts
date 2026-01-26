/**
 * Dev bypass utility for authentication
 *
 * When VITE_DEV_API_KEY is set in development mode, authentication is bypassed
 * to allow local development without WorkOS authentication.
 *
 * Security:
 * - Protected by import.meta.env.DEV check (false in production builds)
 * - Even if accidentally built with key, bypass won't activate in production
 * - Key retrieval on server uses process.env (runtime) as additional protection
 */

/**
 * Check if dev auth bypass is enabled.
 *
 * Client: Uses build-time import.meta.env (needed to skip auth redirect in dev)
 * Server: Uses runtime process.env (never bakes key into bundle)
 *
 * Safe because: import.meta.env.DEV is false in production builds,
 * so bypass never activates even if key was accidentally set during build.
 */
export const isDevBypass: boolean = (() => {
  // Production builds: DEV is false, bypass never enabled
  if (!import.meta.env.DEV) return false

  // Server: check runtime env (preferred)
  if (typeof process !== 'undefined' && process.env?.VITE_DEV_API_KEY) {
    return true
  }

  // Client: check build-time env (for dev UX only)
  return !!import.meta.env.VITE_DEV_API_KEY
})()

/**
 * Mock user for dev bypass mode.
 * Used to satisfy route auth checks when WorkOS auth is bypassed.
 */
export const DEV_USER = {
  id: 'dev-user',
  email: 'dev@localhost',
} as const

/**
 * Get the dev API key. Server-only, uses runtime process.env.
 * @throws Error if called on client or when dev bypass is not enabled
 */
export const getDevApiKey = (): string => {
  if (typeof process === 'undefined') {
    throw new Error('getDevApiKey can only be called on the server')
  }

  if (!import.meta.env.DEV) {
    throw new Error('Dev bypass is only available in development mode')
  }

  const key = process.env.VITE_DEV_API_KEY
  if (!key) {
    throw new Error('VITE_DEV_API_KEY is not set')
  }

  return key
}
