import { createServerFn } from '@tanstack/react-start'
import { userMiddleware } from '../middleware/auth'
import polarService from '../polar'
import { env } from '@/env'

export const getCustomerPortalUrl = createServerFn({ method: 'GET' })
  .middleware([userMiddleware])
  .handler(async ({ context }): Promise<{ url: string }> => {
    try {
      const customer = await polarService.getCustomerExternal(context.userId)

      if (!customer) {
        throw new Error('Customer not found')
      }

      const session = await polarService.createCustomerSession({
        customerId: customer.id,
        returnUrl: `${env.APP_URL}/workspaces`,
      })

      return { url: session.customerPortalUrl }
    } catch (error) {
      console.error('Customer portal creation error:', error)
      throw new Error('Failed to create customer portal session')
    }
  })
