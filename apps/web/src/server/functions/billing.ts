import { createServerFn } from '@tanstack/react-start'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import type { BillingCycleResponse } from '@/interfaces/billing'

export const getBillingCycle = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<BillingCycleResponse> => {
    return lazycloudApi.getBillingCycle(context.accessToken)
  })
