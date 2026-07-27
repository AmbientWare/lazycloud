import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { u as userMiddleware } from "./auth-CoRbzC04.mjs";
import { p as polarService } from "./polar-DEJBJJXZ.mjs";
import { e as env } from "./env-vgS3Y8Xp.mjs";
import { c as createServerFn } from "./index.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_libs/zod.mjs";
import "../_chunks/_libs/@polar-sh/sdk.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
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
const getCustomerPortalUrl_createServerFn_handler = createServerRpc({
  id: "db455e3bb812894428b5b527838a053ea02f57c0ca904032869c0b7a83b291e2",
  name: "getCustomerPortalUrl",
  filename: "src/server/functions/customer.ts"
}, (opts, signal) => getCustomerPortalUrl.__executeServer(opts, signal));
const getCustomerPortalUrl = createServerFn({
  method: "GET"
}).middleware([userMiddleware]).handler(getCustomerPortalUrl_createServerFn_handler, async ({
  context
}) => {
  try {
    const customer = await polarService.getCustomerExternal(context.userId);
    if (!customer) {
      throw new Error("Customer not found");
    }
    const session = await polarService.createCustomerSession({
      customerId: customer.id,
      returnUrl: `${env.APP_URL}/workspaces`
    });
    return {
      url: session.customerPortalUrl
    };
  } catch (error) {
    console.error("Customer portal creation error:", error);
    throw new Error("Failed to create customer portal session");
  }
});
export {
  getCustomerPortalUrl_createServerFn_handler
};
