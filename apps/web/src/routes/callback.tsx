import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import { useEffect } from 'react'
import { onboardUser } from '@/server/functions'
import { USER_HOME } from '@/lib/constants'

export const Route = createFileRoute('/callback')({
  ssr: false,
  component: CallbackPage,
})

function CallbackPage() {
  const { user, loading } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    async function handleCallback() {
      if (loading) return

      if (user) {
        try {
          // Onboard the user (creates them in the API if they don't exist)
          await onboardUser({
            data: {
              userId: user.id,
              email: user.email ?? '',
            },
          })
        } catch (error) {
          console.error('Error onboarding user:', error)
        }

        // Navigate to the workspaces page
        navigate({ to: USER_HOME })
      }
    }

    handleCallback()
  }, [user, loading, navigate])

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-4">
        <div className="size-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        <p className="text-muted-foreground">Processing authentication...</p>
      </div>
    </div>
  )
}
