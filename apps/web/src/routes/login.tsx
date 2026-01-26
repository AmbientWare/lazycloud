import { createFileRoute } from '@tanstack/react-router'
import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import { useEffect } from 'react'

export const Route = createFileRoute('/login')({
  ssr: false,
  component: LoginPage,
})

function LoginPage() {
  const { getAuth, loading, user } = useAuth()

  useEffect(() => {
    if (!loading && !user) {
      getAuth({ ensureSignedIn: true })
    }
  }, [loading, user, getAuth])

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-4">
        <div className="size-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        <p className="text-muted-foreground">Redirecting to login...</p>
      </div>
    </div>
  )
}
