"use server";

import type { UserFeaturesResponse } from "@/interfaces/users";
import lazycloudApi from "@/server/lazycloud_api";
import polarService from "@/server/polar";
import { getAuthToken } from "./utils";

export async function getCurrentUserInternalId(): Promise<string> {
  const accessToken = await getAuthToken();
  const currentUser = await lazycloudApi.getCurrentUser(accessToken);
  return currentUser.id;
}

export async function getUserFeatures(): Promise<UserFeaturesResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getUserFeatures(accessToken);
}

export async function getUserSubscriptionTier(): Promise<string | undefined> {
  const accessToken = await getAuthToken();
  try {
    const customerState = await polarService.getCustomerStateExternal(accessToken);
    const activeSub = customerState.activeSubscriptions[0];
    if (!activeSub?.productId) return undefined;

    const products = await polarService.listProducts();
    const product = products.result.items.find(p => p.id === activeSub.productId);
    return product?.name;
  } catch {
    return undefined;
  }
}

