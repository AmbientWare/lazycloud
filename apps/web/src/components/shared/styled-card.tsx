import { forwardRef } from 'react'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { cn } from '@/lib/utils'

interface StyledCardProps extends React.ComponentPropsWithoutRef<typeof Card> {
  variant?: 'default' | 'interactive' | 'minimal' | 'elevated' | 'static'
}

/**
 * StyledCard - A pre-styled Card component with consistent design across the app
 *
 * Design tokens used:
 * - Border: border-border/60 (standard), hover:border-lazycloud/30 (accent)
 * - Background: bg-card/90 with backdrop-blur-md
 * - Radius: rounded-xl (consistent across all variants)
 * - Shadow: shadow-sm (base), hover:shadow-md (interactive)
 *
 * @variant default - Standard card styling
 * @variant interactive - Default + hover lift and focus states
 * @variant minimal - Lighter styling for nested/list items
 * @variant elevated - Slightly more prominent with hover shadow
 * @variant static - Same as elevated but without hover effect
 */
export const StyledCard = forwardRef<HTMLDivElement, StyledCardProps>(
  ({ className, variant = 'default', ...props }, ref) => {
    return (
      <Card
        ref={ref}
        className={cn(
          // Base styles for all variants
          'relative overflow-hidden rounded-xl border-border/60 bg-card/90 backdrop-blur-md',
          // Variant-specific styles
          variant === 'default' && 'shadow-sm transition-all duration-300',
          variant === 'interactive' && [
            'shadow-sm transition-all duration-300',
            'hover:-translate-y-0.5 hover:shadow-md hover:border-lazycloud/30',
            'focus-visible:ring-2 focus-visible:ring-lazycloud/40 focus-visible:outline-none',
            'group',
          ],
          variant === 'elevated' && [
            'shadow-sm transition-all duration-300 hover:shadow-md',
          ],
          variant === 'static' && 'shadow-sm',
          variant === 'minimal' && [
            'rounded-lg border-border/40 bg-muted/60 shadow-sm',
          ],
          className,
        )}
        {...props}
      >
        {props.children}
      </Card>
    )
  },
)
StyledCard.displayName = 'StyledCard'

export const StyledCardHeader = CardHeader
export const StyledCardTitle = CardTitle
export const StyledCardDescription = CardDescription
export const StyledCardContent = CardContent
export const StyledCardFooter = CardFooter
