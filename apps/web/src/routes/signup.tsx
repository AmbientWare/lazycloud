import { createFileRoute, useSearch } from '@tanstack/react-router'
import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import { getSignUpUrl } from '@workos/authkit-tanstack-react-start'
import { useEffect } from 'react'
import { USER_HOME } from '@/lib/constants'
import { z } from 'zod'

const searchSchema = z.object({
  redirect: z.string().optional(),
})

export const Route = createFileRoute('/signup')({
  ssr: false,
  validateSearch: searchSchema,
  component: SignUpPage,
})

function SignUpPage() {
  const { loading, user } = useAuth()
  const { redirect } = useSearch({ from: '/signup' })

  useEffect(() => {
    if (!loading && !user) {
      const returnPath = redirect || USER_HOME
      getSignUpUrl({ data: returnPath }).then((url) => {
        window.location.href = url
      })
    }
  }, [loading, user, redirect])

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-4">
        <div className="size-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        <p className="text-muted-foreground">Redirecting to sign up...</p>
      </div>
    </div>
  )
}
