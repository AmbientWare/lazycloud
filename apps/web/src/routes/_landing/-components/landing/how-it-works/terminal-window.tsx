import { cn } from '@/lib/utils'

interface TerminalWindowProps {
  children: React.ReactNode
  title?: string
  className?: string
  contentClassName?: string
}

export function TerminalWindow({
  children,
  title,
  className,
  contentClassName,
}: TerminalWindowProps) {
  return (
    <div className="relative">
      {/* Gradient background */}
      <div className="pointer-events-none absolute -inset-6 rounded-2xl bg-gradient-to-br from-lazycloud/30 via-lazycloud/10 to-lazycloud/5 opacity-60 blur-2xl" />

      <div
        className={cn(
          'relative flex flex-col overflow-hidden rounded-xl border border-border/60 bg-card/95 shadow-2xl backdrop-blur-sm',
          className,
        )}
      >
        {/* Header with dots */}
        {title !== undefined && (
          <div className="flex items-center gap-2 border-b border-border/40 bg-muted/60 px-3 py-2">
            <div className="flex gap-1.5">
              <div className="size-2 rounded-full bg-red-500/80" />
              <div className="size-2 rounded-full bg-yellow-500/80" />
              <div className="size-2 rounded-full bg-green-500/80" />
            </div>
            {title && (
              <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                {title}
              </span>
            )}
          </div>
        )}

        {/* Content */}
        <div className={cn('flex-1', contentClassName)}>{children}</div>
      </div>
    </div>
  )
}
