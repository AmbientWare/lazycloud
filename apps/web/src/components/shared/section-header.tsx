import { forwardRef } from 'react'
import { cn } from '@/lib/utils'

interface SectionIndicatorProps extends React.ComponentPropsWithoutRef<'div'> {
  size?: 'sm' | 'md' | 'lg'
  variant?: 'default' | 'subtle'
}

export const SectionIndicator = forwardRef<
  HTMLDivElement,
  SectionIndicatorProps
>(({ size = 'md', variant = 'default', className, ...props }, ref) => {
  const sizeClasses = {
    sm: 'h-0.5 w-4',
    md: 'h-1 w-6',
    lg: 'h-1 w-8',
  }

  const variantClasses = {
    default: 'bg-lazycloud',
    subtle: 'bg-lazycloud/50',
  }

  return (
    <div
      ref={ref}
      className={cn(
        'flex-shrink-0 rounded-full',
        sizeClasses[size],
        variantClasses[variant],
        className,
      )}
      {...props}
    />
  )
})
SectionIndicator.displayName = 'SectionIndicator'

interface SectionHeaderProps extends React.ComponentPropsWithoutRef<'div'> {
  title: string
  description?: string
  indicator?: boolean
  indicatorSize?: 'sm' | 'md' | 'lg'
  indicatorVariant?: 'default' | 'subtle'
  titleSize?: 'sm' | 'md' | 'lg' | 'xl'
}

export const SectionHeader = forwardRef<HTMLDivElement, SectionHeaderProps>(
  (
    {
      title,
      description,
      indicator = true,
      indicatorSize = 'md',
      indicatorVariant = 'default',
      titleSize = 'lg',
      className,
      ...props
    },
    ref,
  ) => {
    const titleSizeClasses = {
      sm: 'text-sm',
      md: 'text-lg',
      lg: 'text-xl',
      xl: 'text-2xl',
    }

    return (
      <div
        ref={ref}
        className={cn('flex items-center gap-3 pb-2', className)}
        {...props}
      >
        {indicator && (
          <SectionIndicator size={indicatorSize} variant={indicatorVariant} />
        )}
        <div className="flex-1">
          <h2
            className={cn(
              'font-semibold tracking-tight',
              titleSizeClasses[titleSize],
            )}
          >
            {title}
          </h2>
          {description && (
            <p className="text-muted-foreground mt-1 text-sm">{description}</p>
          )}
        </div>
      </div>
    )
  },
)
SectionHeader.displayName = 'SectionHeader'
