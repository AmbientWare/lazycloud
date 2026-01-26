import { type ReactNode } from 'react'
import TextLogo from '@/components/shared/textLogo'

type HeaderBarProps = {
  children?: ReactNode
}

/**
 * HeaderBar - Sticky navigation header
 *
 * Design tokens used:
 * - Border: border-border/40 (subtle for floating header)
 * - Background: bg-background/80 with backdrop-blur-xl
 * - Radius: rounded-xl (consistent with card system)
 * - Shadow: shadow-sm (subtle elevation)
 */
export default function HeaderBar({ children }: HeaderBarProps) {
  return (
    <div className="sticky top-0 z-50 w-full px-4 pt-4">
      <div className="mx-auto max-w-7xl">
        <div className="flex h-14 items-center justify-between rounded-xl border border-border/40 bg-background/80 px-6 shadow-sm backdrop-blur-xl">
          <TextLogo />
          {children}
        </div>
      </div>
    </div>
  )
}
