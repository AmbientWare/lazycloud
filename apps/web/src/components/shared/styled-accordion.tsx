import { forwardRef } from 'react'
import {
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from '@/components/ui/accordion'
import { cn } from '@/lib/utils'

type StyledAccordionItemProps = React.ComponentPropsWithoutRef<
  typeof AccordionItem
>

export const StyledAccordionItem = forwardRef<
  React.ComponentRef<typeof AccordionItem>,
  StyledAccordionItemProps
>(({ className, ...props }, ref) => {
  return (
    <AccordionItem
      ref={ref}
      className={cn('border-none', className)}
      {...props}
    />
  )
})
StyledAccordionItem.displayName = 'StyledAccordionItem'

type StyledAccordionTriggerProps = React.ComponentPropsWithoutRef<
  typeof AccordionTrigger
>

export const StyledAccordionTrigger = forwardRef<
  React.ComponentRef<typeof AccordionTrigger>,
  StyledAccordionTriggerProps
>(({ className, children, ...props }, ref) => {
  return (
    <AccordionTrigger
      ref={ref}
      className={cn(
        'group border-border/50 bg-muted/30 hover:bg-muted/50 hover:border-lazycloud/30 data-[state=open]:bg-muted/50 data-[state=open]:border-lazycloud/40 cursor-pointer rounded-lg border px-4 py-3 transition-all hover:border-l-2 hover:no-underline data-[state=open]:border-l-2 data-[state=open]:shadow-sm',
        className,
      )}
      {...props}
    >
      {children}
    </AccordionTrigger>
  )
})
StyledAccordionTrigger.displayName = 'StyledAccordionTrigger'

interface StyledAccordionContentProps extends React.ComponentPropsWithoutRef<
  typeof AccordionContent
> {
  /**
   * Whether to wrap content in the styled card container
   */
  cardWrapper?: boolean
}

export const StyledAccordionContent = forwardRef<
  React.ComponentRef<typeof AccordionContent>,
  StyledAccordionContentProps
>(({ className, cardWrapper = true, children, ...props }, ref) => {
  return (
    <AccordionContent ref={ref} className={cn('', className)} {...props}>
      <div className="pt-4">
        {cardWrapper ? (
          <div className="border-border bg-card rounded-lg border-2 shadow-sm p-4">
            {children}
          </div>
        ) : (
          children
        )}
      </div>
    </AccordionContent>
  )
})
StyledAccordionContent.displayName = 'StyledAccordionContent'
