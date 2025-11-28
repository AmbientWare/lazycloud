"use server";

import polarService from "@/server/polar";
import { auth } from "@clerk/nextjs/server";
import { env } from "@/env";

export async function getCustomerPortalUrl() {
  const { userId } = await auth();

  if (!userId) {
    throw new Error("Unauthorized");
  }

  try {
    const customer = await polarService.getCustomerExternal(userId);

    if (!customer) {
      throw new Error("Customer not found");
    }

    const session = await polarService.createCustomerSession({
      customerId: customer.id,
      returnUrl: `${env.APP_URL}/workspaces`,
    });

    return { url: session.customerPortalUrl };
  } catch (error) {
    console.error("Customer portal creation error:", error);
    throw new Error("Failed to create customer portal session");
  }
}

