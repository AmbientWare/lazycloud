import { useSearchContext } from 'fumadocs-ui/contexts/search'
import { Button } from '@/components/ui/button'
import { useIsMac } from '@/hooks/use-is-mac'

export function DocsSearch() {
  const { setOpenSearch } = useSearchContext()
  const isMac = useIsMac()

  return (
    <Button
      variant="outline"
      className="relative h-9 w-full justify-start rounded-md pr-12 text-sm text-muted-foreground"
      onClick={() => setOpenSearch(true)}
    >
      <span className="truncate">Search...</span>
      <kbd className="pointer-events-none absolute right-1.5 top-1.5 hidden h-6 select-none items-center gap-1 rounded border bg-muted px-1.5 font-mono text-[10px] font-medium opacity-100 sm:flex">
        <span className="text-xs">{isMac ? '⌘' : 'Ctrl'}</span>K
      </kbd>
    </Button>
  )
}
