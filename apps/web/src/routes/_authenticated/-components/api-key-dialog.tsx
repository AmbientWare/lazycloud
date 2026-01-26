import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Separator } from '@/components/ui/separator'
import { CopyButton } from '@/components/shared/copy-button'
import { StyledTooltip } from '@/components/shared/styled-tooltip'
import { ExpandConfirmButton } from '@/components/shared/expand-confirm-button'
import { KeyRound, RefreshCw } from 'lucide-react'
import { regenerateApiKey } from '@/server/functions/api-keys'
import { toast } from 'sonner'
import type { ApiKey } from '@/interfaces/api-keys'

interface ApiKeyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  apiKey: ApiKey | null
  isLoading: boolean
  onApiKeyChange: (apiKey: ApiKey | null) => void
}

const isApiKeyExpired = (expiresAt: string) => {
  const expirationDate = new Date(expiresAt)
  // Year 9999 means never expires
  if (expirationDate.getFullYear() >= 9999) return false
  return expirationDate.getTime() < Date.now()
}

export function ApiKeyDialog({
  open,
  onOpenChange,
  apiKey,
  isLoading,
  onApiKeyChange,
}: ApiKeyDialogProps) {
  const maskApiKey = (key: string) => {
    const prefix = key.slice(0, 3)
    const lastFour = key.slice(-4)
    return `${prefix}${'*'.repeat(20)}${lastFour}`
  }

  const handleRegenerate = async () => {
    if (!apiKey?.id) return
    try {
      const newKey = await regenerateApiKey({
        data: { apiKeyId: apiKey.id },
      })
      onApiKeyChange(newKey)
      toast.success('API key regenerated successfully')
    } catch (err) {
      console.error('Failed to regenerate API key:', err)
      toast.error('Failed to regenerate API key')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="overflow-hidden sm:max-w-2xl"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="size-5" />
            API Key
          </DialogTitle>
          <DialogDescription>
            Use this key to authenticate with the LazyCloud CLI in CI/CD
            pipelines.
          </DialogDescription>
        </DialogHeader>

        <div className="mt-2 overflow-hidden">
          {isLoading ? (
            <div className="rounded-lg border p-4">
              <div className="mb-3 flex items-center justify-between">
                <Skeleton className="h-5 w-24" />
                <Skeleton className="h-5 w-16" />
              </div>
              <Separator className="mb-3" />
              <Skeleton className="mb-2 h-10 w-full" />
              <Skeleton className="size-48" />
            </div>
          ) : apiKey ? (
            <div className="overflow-hidden rounded-lg border border-l-4 border-l-emerald-500/50">
              {/* Header */}
              <div className="flex items-center justify-between bg-muted/30 px-4 py-3">
                <span className="text-sm font-medium">{apiKey.name}</span>
                <Badge
                  variant={
                    isApiKeyExpired(apiKey.expires_at)
                      ? 'destructive'
                      : 'outline'
                  }
                  className={`text-xs ${!isApiKeyExpired(apiKey.expires_at) ? 'border-green-500/50 text-green-500' : ''}`}
                >
                  {isApiKeyExpired(apiKey.expires_at) ? 'Expired' : 'Active'}
                </Badge>
              </div>

              <Separator />

              {/* Key Display */}
              <div className="space-y-3 p-4">
                <div className="flex items-center gap-2">
                  <code className="flex h-11 flex-1 items-center rounded-md border bg-muted/50 px-3 font-mono text-sm text-muted-foreground">
                    {maskApiKey(apiKey.value)}
                  </code>
                  <CopyButton
                    text={apiKey.value}
                    tooltipText="Copy"
                    className="size-11 shrink-0"
                  />
                  <StyledTooltip content="Regenerate">
                    <div>
                      <ExpandConfirmButton
                        onConfirm={handleRegenerate}
                        icon={RefreshCw}
                        color="yellow"
                        buttonClassName="text-yellow-500 hover:bg-yellow-500/10"
                      />
                    </div>
                  </StyledTooltip>
                </div>

                {/* Usage hint */}
                <div className="space-y-1 text-xs text-muted-foreground">
                  <p>Set these environment variables in your CI/CD:</p>
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    <code className="text-emerald-500">LAZYCLOUD_API_KEY</code>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-lg border p-6 text-center">
              <p className="text-sm text-muted-foreground">No API key found</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
