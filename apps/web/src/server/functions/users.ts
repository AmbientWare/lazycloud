import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import polarService from '../polar'
import type { UserFeaturesResponse } from '@/interfaces/users'

export const getCurrentUserInternalId = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<string> => {
    const currentUser = await lazycloudApi.getCurrentUser(context.accessToken)
    return currentUser.id
  })

export const getUserFeatures = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<UserFeaturesResponse> => {
    return lazycloudApi.getUserFeatures(context.accessToken)
  })

export const getUserSubscriptionTier = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<string | undefined> => {
    try {
      const customerState = await polarService.getCustomerStateExternal(
        context.accessToken,
      )
      const activeSub = customerState.activeSubscriptions[0]
      if (!activeSub?.productId) return undefined

      const products = await polarService.listProducts()
      const product = products.result.items.find(
        (p) => p.id === activeSub.productId,
      )
      return product?.name
    } catch {
      return undefined
    }
  })

export const hasActiveSubscription = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<boolean> => {
    try {
      const customerState = await polarService.getCustomerStateExternal(
        context.accessToken,
      )
      return (customerState?.activeSubscriptions?.length ?? 0) > 0
    } catch {
      // On error, fail open to avoid blocking users
      return true
    }
  })

export const onboardUser = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      userId: z.string(),
      name: z.string(),
      email: z.string().email(),
    }),
  )
  .handler(async ({ data }): Promise<{ success: boolean }> => {
    // Ensure the user exists in the API backend (uses admin API key)
    await lazycloudApi.onboardUser(data.userId, data.name, data.email)
    return { success: true }
  })
