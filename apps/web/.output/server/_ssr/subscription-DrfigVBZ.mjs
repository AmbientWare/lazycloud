import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { u as userMiddleware } from "./auth-CoRbzC04.mjs";
import { r as redis } from "./redis-BTsw1DHd.mjs";
import { c as createServerFn } from "./index.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_libs/zod.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@upstash/redis.mjs";
import "../_libs/uncrypto.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_chunks/_libs/react.mjs";
import "../_chunks/_libs/@tanstack/react-router.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
const SUBSCRIPTION_CACHE_PREFIX = "subscription:";
function getSubscriptionCacheKey(userId) {
  return `${SUBSCRIPTION_CACHE_PREFIX}${userId}`;
}
async function invalidateSubscriptionCache(userId) {
  if (!redis) return;
  const cacheKey = getSubscriptionCacheKey(userId);
  await redis.del(cacheKey);
}
const invalidateSubscriptionCacheAction_createServerFn_handler = createServerRpc({
  id: "6ad0ff3572bc6e0b010247dbe8ef53ab138ecc1ec51a8603e96eeead2c48f669",
  name: "invalidateSubscriptionCacheAction",
  filename: "src/server/functions/subscription.ts"
}, (opts, signal) => invalidateSubscriptionCacheAction.__executeServer(opts, signal));
const invalidateSubscriptionCacheAction = createServerFn({
  method: "POST"
}).middleware([userMiddleware]).handler(invalidateSubscriptionCacheAction_createServerFn_handler, async ({
  context
}) => {
  await invalidateSubscriptionCache(context.userId);
  return {
    success: true
  };
});
export {
  invalidateSubscriptionCacheAction_createServerFn_handler
};
