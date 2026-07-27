import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { a as authMiddleware, l as lazycloudApi } from "./auth-CoRbzC04.mjs";
import { p as polarService } from "./polar-DEJBJJXZ.mjs";
import { c as createServerFn } from "./index.mjs";
import { j as object, k as string } from "../_libs/zod.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@polar-sh/sdk.mjs";
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
const getCurrentUserInternalId_createServerFn_handler = createServerRpc({
  id: "bf6f6b753ffbbef8fb966d9928d5dd70ef2a95488a03a6b823d362f3966d125f",
  name: "getCurrentUserInternalId",
  filename: "src/server/functions/users.ts"
}, (opts, signal) => getCurrentUserInternalId.__executeServer(opts, signal));
const getCurrentUserInternalId = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getCurrentUserInternalId_createServerFn_handler, async ({
  context
}) => {
  const currentUser = await lazycloudApi.getCurrentUser(context.accessToken);
  return currentUser.id;
});
const getUserFeatures_createServerFn_handler = createServerRpc({
  id: "7b23d5640ffa3fd48b749b8412f1abd288ecde1b2f97d44937e517c7d45126a9",
  name: "getUserFeatures",
  filename: "src/server/functions/users.ts"
}, (opts, signal) => getUserFeatures.__executeServer(opts, signal));
const getUserFeatures = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getUserFeatures_createServerFn_handler, async ({
  context
}) => {
  return lazycloudApi.getUserFeatures(context.accessToken);
});
const getUserSubscriptionTier_createServerFn_handler = createServerRpc({
  id: "3a5e2b7559f8d84267016a577656121481437ef5e6da35cd1da731b0bdb5d6c9",
  name: "getUserSubscriptionTier",
  filename: "src/server/functions/users.ts"
}, (opts, signal) => getUserSubscriptionTier.__executeServer(opts, signal));
const getUserSubscriptionTier = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getUserSubscriptionTier_createServerFn_handler, async ({
  context
}) => {
  try {
    const customerState = await polarService.getCustomerStateExternal(context.accessToken);
    const activeSub = customerState.activeSubscriptions[0];
    if (!activeSub?.productId) return void 0;
    const products = await polarService.listProducts();
    const product = products.result.items.find((p) => p.id === activeSub.productId);
    return product?.name;
  } catch {
    return void 0;
  }
});
const hasActiveSubscription_createServerFn_handler = createServerRpc({
  id: "0782e69e7da02cf4f2c20d7552805e250710ae2d7907d0061d209b07a89faec5",
  name: "hasActiveSubscription",
  filename: "src/server/functions/users.ts"
}, (opts, signal) => hasActiveSubscription.__executeServer(opts, signal));
const hasActiveSubscription = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(hasActiveSubscription_createServerFn_handler, async ({
  context
}) => {
  try {
    const customerState = await polarService.getCustomerStateExternal(context.accessToken);
    return (customerState?.activeSubscriptions?.length ?? 0) > 0;
  } catch {
    return true;
  }
});
const onboardUser_createServerFn_handler = createServerRpc({
  id: "5025ec9062301c8c41f2d639357616668c5d6204ebd18f9756ffc5e7cd90afe4",
  name: "onboardUser",
  filename: "src/server/functions/users.ts"
}, (opts, signal) => onboardUser.__executeServer(opts, signal));
const onboardUser = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  userId: string(),
  email: string().email()
})).handler(onboardUser_createServerFn_handler, async ({
  context,
  data
}) => {
  await lazycloudApi.onboardUser(context.accessToken, data.userId, data.email);
  return {
    success: true
  };
});
export {
  getCurrentUserInternalId_createServerFn_handler,
  getUserFeatures_createServerFn_handler,
  getUserSubscriptionTier_createServerFn_handler,
  hasActiveSubscription_createServerFn_handler,
  onboardUser_createServerFn_handler
};
