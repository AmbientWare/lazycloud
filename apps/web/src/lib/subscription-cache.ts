import { redis } from "./redis";

const SUBSCRIPTION_CACHE_PREFIX = "subscription:";
const SUBSCRIPTION_CACHE_TTL = 300; // 5 minutes

export function getSubscriptionCacheKey(userId: string): string {
  return `${SUBSCRIPTION_CACHE_PREFIX}${userId}`;
}

export async function invalidateSubscriptionCache(userId: string): Promise<void> {
  if (!redis) return;

  const cacheKey = getSubscriptionCacheKey(userId);
  await redis.del(cacheKey);
}

export async function getSubscriptionStatus(userId: string): Promise<boolean | null> {
  if (!redis) return null;

  const cacheKey = getSubscriptionCacheKey(userId);
  const cached = await redis.get<{ active: boolean }>(cacheKey);

  return cached?.active ?? null;
}

export async function setSubscriptionStatus(
  userId: string,
  active: boolean
): Promise<void> {
  if (!redis) return;

  const cacheKey = getSubscriptionCacheKey(userId);
  await redis.set(cacheKey, { active }, { ex: SUBSCRIPTION_CACHE_TTL });
}
