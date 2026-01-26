import { useState, useEffect, useCallback, useMemo } from 'react'

interface UseScrollSpyOptions {
  sectionIds: string[]
  // Offset from element's document position to trigger activation
  offset?: number
}

interface UseScrollSpyReturn {
  activeId: string
  scrollTo: (id: string) => void
}

export function useScrollSpy({
  sectionIds,
  offset = 150, // Activate slightly before reaching the element
}: UseScrollSpyOptions): UseScrollSpyReturn {
  const [activeId, setActiveId] = useState(sectionIds[0] || '')

  // Memoize sectionIds to prevent unnecessary re-renders
  const ids = useMemo(() => sectionIds, [sectionIds.join(',')])

  useEffect(() => {
    let ticking = false

    const handleScroll = () => {
      if (ticking) return

      ticking = true
      requestAnimationFrame(() => {
        const scrollY = window.scrollY + offset
        let currentSection = ids[0] || ''

        for (const id of ids) {
          const element = document.getElementById(id)
          if (element) {
            // Use offsetTop to get the element's position in the document
            // This doesn't change when the element becomes sticky
            const elementTop = element.offsetTop

            // Find the nearest parent with position relative/absolute for accurate offset
            let parent = element.offsetParent as HTMLElement | null
            let totalOffset = elementTop
            while (parent) {
              totalOffset += parent.offsetTop
              parent = parent.offsetParent as HTMLElement | null
            }

            // Card is active when we've scrolled past its original position
            if (scrollY >= totalOffset) {
              currentSection = id
            }
          }
        }

        setActiveId(currentSection)
        ticking = false
      })
    }

    // Initial check
    handleScroll()

    window.addEventListener('scroll', handleScroll, { passive: true })
    return () => window.removeEventListener('scroll', handleScroll)
  }, [ids, offset])

  const scrollTo = useCallback((id: string) => {
    const element = document.getElementById(id)
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [])

  return { activeId, scrollTo }
}
