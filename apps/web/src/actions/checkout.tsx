"use server";

import { env } from "@/env";
import polarService from "@/server/polar";
import { getUser } from "./utils";

export async function createCheckoutUrl(productId: string) {
  const user = await getUser();
  const userId = user.id;

  try {
    const checkout = await polarService.createCheckout({
      products: [productId],
      externalCustomerId: userId,
      successUrl: `${env.APP_URL}/checkout/success`,
      returnUrl: `${env.APP_URL}/subscribe`,
    });

    return { url: checkout.url };
  } catch (error) {
    console.error("Checkout creation error:", error);
    throw new Error("Failed to create checkout session");
  }
}

