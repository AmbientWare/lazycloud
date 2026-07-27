import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { a as authMiddleware, l as lazycloudApi } from "./auth-CoRbzC04.mjs";
import { c as createServerFn } from "./index.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_libs/zod.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
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
const getBillingCycle_createServerFn_handler = createServerRpc({
  id: "83813ff7efadf0eefb5428c5c5383f8664e3546ce1c5448fffaf7aef4ff29359",
  name: "getBillingCycle",
  filename: "src/server/functions/billing.ts"
}, (opts, signal) => getBillingCycle.__executeServer(opts, signal));
const getBillingCycle = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getBillingCycle_createServerFn_handler, async ({
  context
}) => {
  return lazycloudApi.getBillingCycle(context.accessToken);
});
export {
  getBillingCycle_createServerFn_handler
};
