import { useRouteContext } from '@tanstack/react-router'

/**
 * Get the current user from route context.
 * User is populated by getAuth() in __root.tsx beforeLoad.
 *
 * This is the preferred way to access user in components as it:
 * - Works with SSR (user is fetched server-side)
 * - Doesn't require additional client-side auth checks
 * - Is available in all routes (inherited from root)
 */
export function useRouteUser() {
  const context = useRouteContext({ from: '__root__' })
  return context.user ?? null
}
