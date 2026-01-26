import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { userMiddleware } from '../middleware/auth'
import polarService from '../polar'
import { env } from '@/env'

export const createCheckoutUrl = createServerFn({ method: 'POST' })
  .middleware([userMiddleware])
  .inputValidator(
    z.object({
      productId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<{ url: string }> => {
    try {
      const checkout = await polarService.createCheckout({
        products: [data.productId],
        externalCustomerId: context.userId,
        successUrl: `${env.APP_URL}/checkout/success`,
        returnUrl: `${env.APP_URL}/subscribe`,
      })

      return { url: checkout.url }
    } catch (error) {
      console.error('Checkout creation error:', error)
      throw new Error('Failed to create checkout session')
    }
  })
