import ReactMarkdown from 'react-markdown'
import { Check, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { StyledCard, StyledCardContent } from '@/components/shared/styled-card'
import { type PolarProduct } from '@/server/functions'
import { cn } from '@/lib/utils'
import {
  createCheckoutUrl,
  getCustomerPortalUrl,
  sendEnterpriseInquiry,
} from '@/server/functions'
import { useTransition } from 'react'
import { toast } from 'sonner'
import { useRouteUser } from '@/hooks/useRouteUser'
import { useNavigate } from '@tanstack/react-router'

function formatPrice(priceAmount: number): string {
  return `$${(priceAmount / 100).toFixed(0)}`
}

export function ProductCard({
  product,
  isEnterprise,
  summary = false,
  onButtonClick,
  buttonText,
}: {
  product: PolarProduct
  isEnterprise?: boolean
  summary?: boolean
  /** Custom click handler - if provided, overrides the default checkout/portal behavior */
  onButtonClick?: (product: PolarProduct) => void
  /** Custom button text - if provided, overrides the default text */
  buttonText?: string
}) {
  const fixedPrice = product.prices.find((p) => p.amountType === 'fixed')
  const isFree = !fixedPrice || fixedPrice.priceAmount === 0
  const isPro = product.name.toLowerCase() === 'pro'
  const isMostPopular = isPro && !summary
  const monthlyPrice = fixedPrice ? formatPrice(fixedPrice.priceAmount) : null
  const [isPending, startTransition] = useTransition()
  const user = useRouteUser()
  const userId = user?.id
  const navigate = useNavigate()

  // Extract key features for summary mode (first 3 bullet points)
  const getSummaryFeatures = (
    description: string | null | undefined,
  ): string[] => {
    if (!description) return []
    const lines = description.split('\n').filter((line) => line.trim())
    const bulletPoints = lines
      .filter(
        (line) => line.trim().startsWith('-') || line.trim().startsWith('*'),
      )
      .map((line) => line.replace(/^[-*]\s*/, '').trim())
      .slice(0, 3)
    return bulletPoints
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()

    if (!user) {
      navigate({ to: '/signup' })
      return
    }

    // Use custom handler if provided
    if (onButtonClick) {
      onButtonClick(product)
      return
    }

    if (isEnterprise) {
      startTransition(async () => {
        const toastId = toast.loading('Sending inquiry...')
        try {
          const email = user.email
          const name =
            `${user.firstName ?? ''} ${user.lastName ?? ''}`.trim() || undefined

          await sendEnterpriseInquiry({
            data: { email, name, workosId: userId },
          })
          toast.success("Inquiry sent! We'll be in touch soon.", {
            id: toastId,
          })
        } catch (error) {
          const errorMessage =
            error instanceof Error ? error.message : String(error)
          toast.error(errorMessage || 'Failed to send inquiry', { id: toastId })
        }
      })
      return
    }

    startTransition(async () => {
      // For paid subscriptions, redirect to customer portal
      if (!isFree) {
        const toastId = toast.loading('Redirecting to customer portal...')
        try {
          const result = await getCustomerPortalUrl()

          if (!result?.url) {
            throw new Error('No portal URL returned')
          }

          toast.success('Redirecting...', { id: toastId })
          window.location.replace(result.url)
        } catch (error) {
          const errorMessage =
            error instanceof Error ? error.message : String(error)
          toast.error(errorMessage || 'Failed to access customer portal', {
            id: toastId,
          })
        }
        return
      }

      // For free tier, create checkout
      const toastId = toast.loading('Creating checkout session...')
      try {
        const result = await createCheckoutUrl({
          data: { productId: product.id },
        })

        if (!result?.url) {
          throw new Error('No checkout URL returned')
        }

        toast.success('Redirecting to checkout...', { id: toastId })
        window.location.replace(result.url)
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error)
        toast.error(errorMessage || 'Failed to create checkout session', {
          id: toastId,
        })
      }
    })
  }

  // Summary mode - simplified card for landing page preview
  if (summary) {
    const features = getSummaryFeatures(product.description)
    const isPopular = isPro

    return (
      <StyledCard
        variant="interactive"
        className={cn(
          'relative h-full transition-all',
          isPopular && 'border-lazycloud/40',
        )}
      >
        {isPopular && (
          <div className="absolute -top-3 left-1/2 -translate-x-1/2">
            <Badge className="border-0 bg-lazycloud text-xs font-medium text-white">
              Most Popular
            </Badge>
          </div>
        )}
        <StyledCardContent className="flex h-full flex-col p-6">
          {/* Header */}
          <div className="mb-4">
            <h3 className="text-lg font-bold">{product.name}</h3>
          </div>

          {/* Price */}
          <div className="mb-5">
            <div className="flex items-baseline gap-1">
              <span className="text-3xl font-bold">{monthlyPrice ?? '$0'}</span>
              <span className="text-muted-foreground text-sm">/mo</span>
            </div>
            <div className="text-muted-foreground mt-1 text-xs">
              + usage-based pricing
            </div>
          </div>

          {/* Features list */}
          {features.length > 0 && (
            <ul className="space-y-2.5 text-sm">
              {features.map((feature, idx) => (
                <li key={idx} className="flex items-start gap-2">
                  <Check className="mt-0.5 size-4 shrink-0 text-lazycloud" />
                  <span className="text-muted-foreground">{feature}</span>
                </li>
              ))}
            </ul>
          )}
        </StyledCardContent>
      </StyledCard>
    )
  }

  return (
    <StyledCard
      variant="interactive"
      className={cn(
        'relative h-full transition-all',
        (isMostPopular || isEnterprise) &&
          'overflow-visible border-lazycloud/40 shadow-md',
      )}
    >
      {isMostPopular && (
        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
          <Badge className="border-0 bg-lazycloud text-xs font-semibold text-white">
            Most Popular
          </Badge>
        </div>
      )}
      {isEnterprise && (
        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
          <Badge className="border-0 bg-lazycloud text-xs font-semibold text-white">
            Custom Pricing
          </Badge>
        </div>
      )}
      <StyledCardContent className="flex h-full flex-col gap-4 p-4 sm:p-5 lg:p-6">
        <div className="space-y-4">
          <div className="space-y-2">
            <h3 className="text-xl font-bold lg:text-2xl">{product.name}</h3>
            <div className="space-y-1">
              {isEnterprise ? (
                <div className="text-2xl font-bold sm:text-3xl lg:text-4xl">
                  Contact Us
                </div>
              ) : (
                <div className="flex items-baseline gap-1.5">
                  <span className="text-2xl font-bold sm:text-3xl lg:text-4xl">
                    {monthlyPrice ?? '$0'}
                  </span>
                  <span className="text-muted-foreground text-sm">/month</span>
                </div>
              )}
              {!isEnterprise && (
                <div className="text-muted-foreground text-xs">
                  + pay-as-you-go pricing
                </div>
              )}
            </div>
          </div>
          <div className="text-muted-foreground leading-relaxed">
            <ReactMarkdown
              components={{
                h1: () => null,
                h2: ({ className, ...props }) => (
                  <h2
                    className={cn(
                      'text-foreground mb-3 text-lg font-semibold',
                      className,
                    )}
                    {...props}
                  />
                ),
                h3: () => null,
                p: ({ className, ...props }) => (
                  <p
                    className={cn(
                      'text-muted-foreground mb-5 text-sm leading-relaxed',
                      className,
                    )}
                    {...props}
                  />
                ),
                ul: ({ className, ...props }) => (
                  <ul
                    className={cn(
                      'text-foreground my-4 space-y-2.5',
                      className,
                    )}
                    {...props}
                  />
                ),
                li: ({ className, children, ...props }) => (
                  <li
                    className={cn(
                      'flex items-start gap-3 text-sm leading-relaxed',
                      className,
                    )}
                    {...props}
                  >
                    <div className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full bg-emerald-700 dark:bg-emerald-600">
                      <Check className="size-3 text-black" />
                    </div>
                    <span>{children}</span>
                  </li>
                ),
              }}
            >
              {product.description ?? ''}
            </ReactMarkdown>
          </div>
        </div>

        <div className="flex flex-1 flex-col justify-end">
          <form onSubmit={handleSubmit}>
            <Button
              type="submit"
              size="lg"
              variant={isMostPopular || isEnterprise ? 'default' : 'outline'}
              className="w-full"
              disabled={isPending}
            >
              {isPending ? (
                <>
                  <Loader2 className="mr-2 size-4 animate-spin" />
                  Loading...
                </>
              ) : buttonText ? (
                buttonText
              ) : isEnterprise ? (
                'Contact Sales'
              ) : isFree ? (
                'Get Started'
              ) : (
                'Subscribe'
              )}
            </Button>
          </form>
        </div>
      </StyledCardContent>
    </StyledCard>
  )
}
