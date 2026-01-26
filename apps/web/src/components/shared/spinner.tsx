import { cn } from '@/lib/utils'

interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

export function Spinner({ size = 'sm', className }: SpinnerProps) {
  const sizeClasses = {
    sm: 'size-4',
    md: 'size-6',
    lg: 'size-8',
  }

  return (
    <div className={cn('relative', sizeClasses[size], className)}>
      <div
        className="absolute inset-0 animate-spin"
        style={{ animationDuration: '2s' }}
      >
        {Array.from({ length: 6 }).map((_, i) => (
          <div
            key={i}
            className="bg-lazycloud absolute left-1/2 top-0 h-[18%] w-[4px] -translate-x-1/2 rounded-full"
            style={{
              transform: `rotate(${i * 60}deg) translateY(0)`,
              transformOrigin: '50% 250%',
            }}
          />
        ))}
      </div>
    </div>
  )
}
