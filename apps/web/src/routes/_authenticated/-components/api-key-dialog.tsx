import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { Skeleton } from '@/components/ui/skeleton'
import { Separator } from '@/components/ui/separator'
import { CopyButton } from '@/components/shared/copy-button'
import { ExpandConfirmButton } from '@/components/shared/expand-confirm-button'
import { Eye, EyeOff, KeyRound, RefreshCw } from 'lucide-react'
import { regenerateApiKey } from '@/server/functions/api-keys'
import { toast } from 'sonner'
import { useState } from 'react'
import type { ApiKey } from '@/interfaces/api-keys'
import { useIsMobile } from '@/hooks/use-mobile'

interface ApiKeyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  apiKey: ApiKey | null
  isLoading: boolean
  onApiKeyChange: (apiKey: ApiKey | null) => void
}

function ApiKeyContent({
  apiKey,
  isLoading,
  onApiKeyChange,
}: {
  apiKey: ApiKey | null
  isLoading: boolean
  onApiKeyChange: (apiKey: ApiKey | null) => void
}) {
  const [showKey, setShowKey] = useState(false)

  const maskApiKey = (key: string) =>
    `${key.slice(0, 3)}${'*'.repeat(20)}${key.slice(-4)}`

  const handleRegenerate = async () => {
    if (!apiKey?.id) return
    try {
      const newKey = await regenerateApiKey({ data: { apiKeyId: apiKey.id } })
      onApiKeyChange(newKey)
      toast.success('API key regenerated successfully')
    } catch (err) {
      console.error('Failed to regenerate API key:', err)
      toast.error('Failed to regenerate API key')
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-3 rounded-lg border p-4">
        <div className="flex items-center justify-between">
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-5 w-16" />
        </div>
        <Skeleton className="h-11 w-full" />
      </div>
    )
  }

  if (!apiKey) {
    return (
      <div className="rounded-lg border p-6 text-center">
        <p className="text-sm text-muted-foreground">No API key found</p>
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-l-4 border-l-emerald-500/50">
      <div className="flex items-center justify-between bg-muted/30 px-4 py-2">
        <span className="text-sm font-medium">{apiKey.name}</span>
        <div className="flex gap-1">
          <CopyButton text={apiKey.value} size="icon-sm" />
          <ExpandConfirmButton
            onConfirm={handleRegenerate}
            icon={RefreshCw}
            color="yellow"
            size="icon-sm"
          />
        </div>
      </div>

      <Separator />

      <div className="space-y-3 p-4">
        <code className="flex h-11 items-center justify-between gap-2 rounded-md border bg-muted/50 px-4 font-mono text-sm text-muted-foreground">
          <span className={showKey ? 'overflow-x-auto' : 'truncate'}>
            {showKey ? apiKey.value : maskApiKey(apiKey.value)}
          </span>
          <button
            type="button"
            onClick={() => setShowKey(!showKey)}
            className="hover:cursor-pointer shrink-0 text-muted-foreground/60 hover:text-muted-foreground"
          >
            {showKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
        </code>

        <p className="text-xs text-muted-foreground">
          Set <code className="text-emerald-500">LAZYCLOUD_API_KEY</code> in
          your CI/CD environment.
        </p>
      </div>
    </div>
  )
}

export function ApiKeyDialog({
  open,
  onOpenChange,
  apiKey,
  isLoading,
  onApiKeyChange,
}: ApiKeyDialogProps) {
  const isMobile = useIsMobile()

  const content = (
    <ApiKeyContent
      apiKey={apiKey}
      isLoading={isLoading}
      onApiKeyChange={onApiKeyChange}
    />
  )

  if (isMobile) {
    return (
      <Drawer open={open} onOpenChange={onOpenChange}>
        <DrawerContent>
          <DrawerHeader className="text-left">
            <DrawerTitle className="flex items-center gap-2">
              <KeyRound className="size-5" />
              API Key
            </DrawerTitle>
            <DrawerDescription>
              Authenticate with the LazyCloud CLI in CI/CD pipelines.
            </DrawerDescription>
          </DrawerHeader>
          <div className="px-4 pb-8">{content}</div>
        </DrawerContent>
      </Drawer>
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="size-5" />
            API Key
          </DialogTitle>
          <DialogDescription>
            Authenticate with the LazyCloud CLI in CI/CD pipelines.
          </DialogDescription>
        </DialogHeader>
        {content}
      </DialogContent>
    </Dialog>
  )
}
