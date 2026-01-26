import { createFileRoute } from '@tanstack/react-router'
import { handleCallbackRoute } from '@workos/authkit-tanstack-react-start'
import lazycloudApi from '@/server/lazycloud-api'

export const Route = createFileRoute('/api/auth/callback')({
  server: {
    handlers: {
      GET: handleCallbackRoute({
        onSuccess: async ({ user }) => {
          // Onboard new users to the API backend
          const name =
            [user.firstName, user.lastName].filter(Boolean).join(' ') ||
            user.email ||
            'User'
          await lazycloudApi.onboardUser(user.id, name, user.email || '')
        },
      }),
    },
  },
})
