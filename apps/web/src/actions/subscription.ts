"use server";

import { withAuth } from "@workos-inc/authkit-nextjs";
import { invalidateSubscriptionCache } from "@/lib/subscription-cache";

export async function invalidateSubscriptionCacheAction() {
  const { user } = await withAuth({ ensureSignedIn: true });

  await invalidateSubscriptionCache(user.id);

  return { success: true };
}
