import { useState, useEffect } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { hasActiveSubscription } from '@/server/functions/users'
import { useRouteUser } from '@/hooks/useRouteUser'

export function SubscriptionBanner() {
  const user = useRouteUser()
  const [hasSubscription, setHasSubscription] = useState<boolean | null>(null)

  useEffect(() => {
    if (!user) return

    hasActiveSubscription()
      .then(setHasSubscription)
      .catch(() => {
        // On error, don't show banner to avoid blocking users
        setHasSubscription(true)
      })
  }, [user])

  // Don't render while loading or if user has subscription
  if (hasSubscription === null || hasSubscription) {
    return null
  }

  return (
    <div className="flex items-center justify-between gap-4 rounded-lg border border-yellow-500/50 bg-yellow-500/10 px-4 py-3">
      <div className="flex items-center gap-3">
        <AlertTriangle className="size-5 shrink-0 text-yellow-400" />
        <p className="text-sm text-yellow-200">
          <span className="font-semibold">Payment method required.</span> Add a
          payment method to deploy your applications.
        </p>
      </div>
      <Button
        asChild
        size="sm"
        className="shrink-0 bg-yellow-500 text-black hover:bg-yellow-600"
      >
        <Link to="/subscribe">Add Payment</Link>
      </Button>
    </div>
  )
}
