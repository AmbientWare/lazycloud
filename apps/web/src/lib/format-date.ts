import { format, startOfMonth } from 'date-fns'

export function formatShortDate(dateStr: string | null | undefined): string {
  if (!dateStr) return ''
  try {
    return format(new Date(dateStr), 'MMM d, yyyy')
  } catch {
    return ''
  }
}

/**
 * Convert a local date to UTC ISO string.
 * Used for API requests where we need consistent date handling.
 */
export function dateToUrlString(date: Date, isEnd: boolean): string {
  const year = date.getFullYear()
  const month = date.getMonth()
  const day = date.getDate()

  if (isEnd) {
    // End of local calendar day
    const localEndOfDay = new Date(year, month, day, 23, 59, 59, 999)
    return localEndOfDay.toISOString()
  } else {
    // Start of local calendar day
    const localMidnight = new Date(year, month, day, 0, 0, 0)
    return localMidnight.toISOString()
  }
}

/**
 * Get default date range (start of current month to now) as ISO strings.
 */
export function getDefaultDateRange(): { start: string; end: string } {
  const from = startOfMonth(new Date())
  const to = new Date()
  return {
    start: dateToUrlString(from, false),
    end: dateToUrlString(to, true),
  }
}
