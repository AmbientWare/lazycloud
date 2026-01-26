import { forwardRef } from 'react'
import { cn } from '@/lib/utils'

interface SectionDividerProps extends React.ComponentPropsWithoutRef<'div'> {
  spacing?: 'sm' | 'md' | 'lg'
  children?: React.ReactNode
}

export const SectionDivider = forwardRef<HTMLDivElement, SectionDividerProps>(
  ({ spacing = 'md', className, children, ...props }, ref) => {
    const spacingClasses = {
      sm: 'pt-6',
      md: 'pt-8',
      lg: 'pt-10',
    }

    return (
      <div
        ref={ref}
        className={cn(spacingClasses[spacing], 'relative', className)}
        {...props}
      >
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-border/30 to-transparent" />
        {children}
      </div>
    )
  },
)
SectionDivider.displayName = 'SectionDivider'
