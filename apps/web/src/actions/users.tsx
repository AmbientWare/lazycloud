"use server";

import type { UserFeaturesResponse } from "@/interfaces/users";
import lazycloudApi from "@/server/lazycloud_api";
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

