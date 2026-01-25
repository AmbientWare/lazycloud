import { redis } from "./redis";

const SUBSCRIPTION_CACHE_PREFIX = "subscription:";

function getSubscriptionCacheKey(userId: string): string {
  return `${SUBSCRIPTION_CACHE_PREFIX}${userId}`;
}

export async function invalidateSubscriptionCache(userId: string): Promise<void> {
  if (!redis) return;

  const cacheKey = getSubscriptionCacheKey(userId);
  await redis.del(cacheKey);
}
