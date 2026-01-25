"use server";

import { invalidateSubscriptionCache } from "@/lib/subscription-cache";
import { getUser } from "./utils";

export async function invalidateSubscriptionCacheAction() {
  const user = await getUser();

  await invalidateSubscriptionCache(user.id);

  return { success: true };
}
