"use server";

import type { UserFeaturesResponse } from "@/interfaces/users";
import lazycloudApi from "@/server/lazycloud_api";
import polarService from "@/server/polar";
import { getUserId } from "./utils";

export async function getCurrentUserInternalId(): Promise<string> {
  const userId = await getUserId();
  const currentUser = await lazycloudApi.getCurrentUser(userId);
  return currentUser.id;
}

export async function getUserFeatures(): Promise<UserFeaturesResponse> {
  const userId = await getUserId();
  return lazycloudApi.getUserFeatures(userId);
}

export async function getUserSubscriptionTier(): Promise<string | undefined> {
  const userId = await getUserId();
  try {
    const customerState = await polarService.getCustomerStateExternal(userId);
    const activeSub = customerState.activeSubscriptions[0];
    if (!activeSub?.productId) return undefined;

    const products = await polarService.listProducts();
    const product = products.result.items.find(p => p.id === activeSub.productId);
    return product?.name;
  } catch {
    return undefined;
  }
}

