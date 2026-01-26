import { createFileRoute, redirect, useNavigate } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { invalidateSubscriptionCacheAction } from '@/server/functions'
import { USER_HOME } from '@/lib/constants'

export const Route = createFileRoute('/checkout/success')({
  beforeLoad: async ({ context }) => {
    if (!context.user) {
      throw redirect({ to: '/login' })
    }
  },
  component: CheckoutSuccessPage,
})

function CheckoutSuccessPage() {
  const navigate = useNavigate()
  const [isProcessing, setIsProcessing] = useState(true)

  useEffect(() => {
    async function processSuccess() {
      try {
        // Invalidate subscription cache so the new subscription shows up
        await invalidateSubscriptionCacheAction()
      } catch (error) {
        console.error('Error invalidating cache:', error)
      }

      setIsProcessing(false)

      // Redirect to workspaces after a short delay
      setTimeout(() => {
        navigate({ to: USER_HOME })
      }, 3000)
    }

    processSuccess()
  }, [navigate])

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="text-center max-w-md mx-auto p-8">
        <div className="mb-6">
          <div className="w-16 h-16 bg-green-500/10 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg
              className="size-8 text-green-500"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M5 13l4 4L19 7"
              />
            </svg>
          </div>
          <h1 className="text-3xl font-bold mb-2">Thank You!</h1>
          <p className="text-muted-foreground">
            {isProcessing
              ? 'Processing your subscription...'
              : 'Your subscription has been activated. Redirecting to your workspaces...'}
          </p>
        </div>

        {isProcessing && (
          <div className="size-6 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto" />
        )}
      </div>
    </div>
  )
}
