import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { LogOut, CreditCard, Key, MessageSquare } from 'lucide-react'
import { Spinner } from '@/components/shared/spinner'
import { useState, useEffect } from 'react'
import { getCustomerPortalUrl } from '@/server/functions/customer'
import { getUserSubscriptionTier } from '@/server/functions/users'
import { getApiKeys } from '@/server/functions/api-keys'
import { toast } from 'sonner'
import { ApiKeyDialog } from './api-key-dialog'
import { FeedbackDialog } from './feedback-dialog'
import { useRouteUser } from '@/hooks/useRouteUser'
import type { ApiKey } from '@/interfaces/api-keys'

interface CustomUserButtonProps {
  showDetails?: boolean
}

export function CustomUserButton({
  showDetails = false,
}: CustomUserButtonProps) {
  const user = useRouteUser()
  const { signOut } = useAuth()
  const [isLoadingPortal, setIsLoadingPortal] = useState(false)
  const [subscriptionTier, setSubscriptionTier] = useState<string | undefined>()
  const [apiKeyDialogOpen, setApiKeyDialogOpen] = useState(false)
  const [feedbackDialogOpen, setFeedbackDialogOpen] = useState(false)
  const [apiKey, setApiKey] = useState<ApiKey | null>(null)
  const [isLoadingApiKey, setIsLoadingApiKey] = useState(false)

  // Prefetch API key on mount so it's ready when dialog opens
  useEffect(() => {
    if (user) {
      setIsLoadingApiKey(true)
      getApiKeys()
        .then((keys) => {
          const defaultKey = keys.find((k) => k.name === 'default') ?? keys[0]
          setApiKey(defaultKey ?? null)
        })
        .catch((err) => {
          console.error('Failed to prefetch API key:', err)
        })
        .finally(() => {
          setIsLoadingApiKey(false)
        })
    }
  }, [user])

  useEffect(() => {
    if (showDetails && user) {
      getUserSubscriptionTier().then(setSubscriptionTier)
    }
  }, [showDetails, user])

  const handleSignOut = async () => {
    await signOut()
  }

  const handleCustomerPortal = async () => {
    setIsLoadingPortal(true)
    try {
      const result = await getCustomerPortalUrl()
      window.location.href = result.url
    } catch (error) {
      console.error('Error opening customer portal:', error)
      toast.error('Failed to open customer portal')
      setIsLoadingPortal(false)
    }
  }

  if (!user) {
    return null
  }

  const initials =
    user.firstName && user.lastName
      ? `${user.firstName[0]}${user.lastName[0]}`
      : (user.email?.[0]?.toUpperCase() ?? 'U')

  const displayName =
    user.firstName && user.lastName
      ? `${user.firstName} ${user.lastName}`
      : user.email

  const avatar = (
    <div className="relative flex size-8 shrink-0 overflow-hidden rounded-full">
      {user.profilePictureUrl ? (
        <img
          src={user.profilePictureUrl}
          alt={displayName || 'User'}
          width={32}
          height={32}
          className="size-full object-cover"
        />
      ) : (
        <div className="flex size-full items-center justify-center bg-muted">
          <span className="text-xs font-medium">{initials}</span>
        </div>
      )}
    </div>
  )

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="flex min-h-[44px] cursor-pointer items-center gap-3 rounded-lg p-1.5 transition-colors hover:bg-accent/50 focus:outline-none focus-visible:outline-none"
          aria-label="User menu"
        >
          {avatar}
          {showDetails && (
            <div className="flex flex-col items-start text-left">
              <span className="text-sm font-medium leading-tight">
                {displayName}
              </span>
              {subscriptionTier && (
                <span className="text-xs leading-tight text-muted-foreground">
                  {subscriptionTier}
                </span>
              )}
            </div>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem
          onClick={() => setApiKeyDialogOpen(true)}
          className="cursor-pointer"
        >
          <Key className="mr-2 size-4" />
          API Key
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault()
            handleCustomerPortal()
          }}
          disabled={isLoadingPortal}
          className="cursor-pointer"
        >
          {isLoadingPortal ? (
            <Spinner size="sm" className="mr-2" />
          ) : (
            <CreditCard className="mr-2 size-4" />
          )}
          Customer Portal
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={() => setFeedbackDialogOpen(true)}
          className="cursor-pointer"
        >
          <MessageSquare className="mr-2 size-4" />
          Feedback
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={handleSignOut}
          variant="destructive"
          className="cursor-pointer"
        >
          <LogOut className="mr-2 size-4" />
          Sign Out
        </DropdownMenuItem>
      </DropdownMenuContent>
      <ApiKeyDialog
        open={apiKeyDialogOpen}
        onOpenChange={setApiKeyDialogOpen}
        apiKey={apiKey}
        isLoading={isLoadingApiKey}
        onApiKeyChange={setApiKey}
      />
      <FeedbackDialog
        open={feedbackDialogOpen}
        onOpenChange={setFeedbackDialogOpen}
      />
    </DropdownMenu>
  )
}
