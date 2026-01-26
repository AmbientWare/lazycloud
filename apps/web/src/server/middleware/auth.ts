import { createMiddleware } from '@tanstack/react-start'
import { getAuth } from '@workos/authkit-tanstack-react-start'
import { isDevBypass, getDevApiKey } from '@/lib/dev-bypass'
import lazycloudApi from '@/server/lazycloud-api'

// Cache for dev user to avoid repeated API calls
let cachedDevUser: { id: string } | null = null

/**
 * Get the dev user ID from the cached value or fetch it
 */
async function getDevUserId(): Promise<string> {
  if (!cachedDevUser) {
    const currentUser = await lazycloudApi.getCurrentUser(getDevApiKey())
    cachedDevUser = { id: currentUser.workos_id }
  }
  return cachedDevUser.id
}

/**
 * Auth middleware that gets accessToken from WorkOS session and passes it via context.
 * In dev mode with DEV_API_KEY set, it bypasses authentication.
 *
 * No input required - reads from the authkitMiddleware request context.
 */
export const authMiddleware = createMiddleware({ type: 'function' }).server(
  async ({ next }) => {
    // Dev mode bypass
    if (isDevBypass) {
      return next({
        context: {
          accessToken: getDevApiKey(),
        },
      })
    }

    // Get auth from WorkOS session (set by authkitMiddleware in start.ts)
    const auth = await getAuth()

    if (!auth.user) {
      throw new Error('Not authenticated')
    }

    return next({
      context: {
        accessToken: auth.accessToken,
      },
    })
  },
)

/**
 * User middleware that gets both accessToken and userId from WorkOS session.
 * Used for endpoints that need user context (billing, checkout, etc.)
 *
 * No input required - reads from the authkitMiddleware request context.
 */
export const userMiddleware = createMiddleware({ type: 'function' }).server(
  async ({ next }) => {
    // Dev mode bypass
    if (isDevBypass) {
      const devUserId = await getDevUserId()
      return next({
        context: {
          accessToken: getDevApiKey(),
          userId: devUserId,
        },
      })
    }

    // Get auth from WorkOS session
    const auth = await getAuth()

    if (!auth.user) {
      throw new Error('Not authenticated')
    }

    return next({
      context: {
        accessToken: auth.accessToken,
        userId: auth.user.id,
      },
    })
  },
)
