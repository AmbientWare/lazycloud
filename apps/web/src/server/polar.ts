import { Polar } from '@polar-sh/sdk'
import { env } from '@/env'

class PolarServiceClass {
  private client: Polar

  constructor() {
    this.client = new Polar({
      accessToken: env.POLAR_ACCESS_TOKEN,
      server: env.IS_POLAR_SANDBOX ? 'sandbox' : 'production',
    })
  }

  // Products Methods
  async listProducts(options?: { isArchived?: boolean }) {
    return await this.client.products.list({
      isArchived: options?.isArchived ?? false,
    })
  }

  // Customer Methods
  async getCustomerExternal(externalId: string) {
    return await this.client.customers.getExternal({
      externalId,
    })
  }

  async getCustomerStateExternal(externalId: string) {
    return await this.client.customers.getStateExternal({
      externalId,
    })
  }

  // Customer Session Methods
  async createCustomerSession(options: {
    customerId: string
    returnUrl: string
  }) {
    return await this.client.customerSessions.create({
      customerId: options.customerId,
      returnUrl: options.returnUrl,
    })
  }

  // Checkout Methods
  async createCheckout(options: {
    products: string[]
    externalCustomerId: string
    successUrl: string
    returnUrl: string
  }) {
    return await this.client.checkouts.create({
      products: options.products,
      externalCustomerId: options.externalCustomerId,
      successUrl: options.successUrl,
      returnUrl: options.returnUrl,
      customerBillingAddress: {
        country: 'US',
      },
    })
  }
}

const polarService = new PolarServiceClass()
export default polarService
