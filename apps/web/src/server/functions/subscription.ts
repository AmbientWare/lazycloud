import { createServerFn } from '@tanstack/react-start'
import { userMiddleware } from '../middleware/auth'
import { invalidateSubscriptionCache } from '@/lib/subscription-cache'

export const invalidateSubscriptionCacheAction = createServerFn({
  method: 'POST',
})
  .middleware([userMiddleware])
  .handler(async ({ context }): Promise<{ success: boolean }> => {
    await invalidateSubscriptionCache(context.userId)
    return { success: true }
  })
