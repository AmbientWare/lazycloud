import { e as env } from "./env-vgS3Y8Xp.mjs";
import { P as Polar } from "../_chunks/_libs/@polar-sh/sdk.mjs";
class PolarServiceClass {
  client;
  constructor() {
    this.client = new Polar({
      accessToken: env.POLAR_ACCESS_TOKEN,
      server: env.IS_POLAR_SANDBOX ? "sandbox" : "production"
    });
  }
  // Products Methods
  async listProducts(options) {
    return await this.client.products.list({
      isArchived: options?.isArchived ?? false
    });
  }
  // Customer Methods
  async getCustomerExternal(externalId) {
    return await this.client.customers.getExternal({
      externalId
    });
  }
  async getCustomerStateExternal(externalId) {
    return await this.client.customers.getStateExternal({
      externalId
    });
  }
  // Customer Session Methods
  async createCustomerSession(options) {
    return await this.client.customerSessions.create({
      customerId: options.customerId,
      returnUrl: options.returnUrl
    });
  }
  // Checkout Methods
  async createCheckout(options) {
    return await this.client.checkouts.create({
      products: options.products,
      externalCustomerId: options.externalCustomerId,
      successUrl: options.successUrl,
      returnUrl: options.returnUrl,
      customerBillingAddress: {
        country: "US"
      }
    });
  }
}
const polarService = new PolarServiceClass();
export {
  polarService as p
};
