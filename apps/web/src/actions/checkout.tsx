"use server";

import { env } from "@/env";
import polarService from "@/server/polar";
import { auth } from "@clerk/nextjs/server";
import { completeOnboarding } from "@/actions/onboarding";

export async function createCheckoutUrl(productId: string) {
  const { userId, sessionClaims } = await auth();

  if (!userId) {
    throw new Error("Unauthorized");
  }

  if (!sessionClaims?.metadata?.onboardingComplete) {
    const name = sessionClaims?.fullName as string;
    const email = sessionClaims?.email as string;

    if (name && email) {
      await completeOnboarding(userId, name, email);
    }
  }

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

