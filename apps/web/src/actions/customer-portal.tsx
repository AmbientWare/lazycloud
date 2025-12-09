"use server";

import polarService from "@/server/polar";
import { withAuth } from "@workos-inc/authkit-nextjs";
import { env } from "@/env";

export async function getCustomerPortalUrl() {
  const { user } = await withAuth({ ensureSignedIn: true });
  const userId = user.id;

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

