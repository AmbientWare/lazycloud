import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { u as userMiddleware } from "./auth-CoRbzC04.mjs";
import { p as polarService } from "./polar-DEJBJJXZ.mjs";
import { e as env } from "./env-vgS3Y8Xp.mjs";
import { c as createServerFn } from "./index.mjs";
import { j as object, k as string } from "../_libs/zod.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
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
const createCheckoutUrl_createServerFn_handler = createServerRpc({
  id: "c202ff8b37cad6a414a6b98aaff1d8abb2a87fe0bf4b2a41d8d4a03a7cd91e78",
  name: "createCheckoutUrl",
  filename: "src/server/functions/checkout.ts"
}, (opts, signal) => createCheckoutUrl.__executeServer(opts, signal));
const createCheckoutUrl = createServerFn({
  method: "POST"
}).middleware([userMiddleware]).inputValidator(object({
  productId: string()
})).handler(createCheckoutUrl_createServerFn_handler, async ({
  context,
  data
}) => {
  try {
    const checkout = await polarService.createCheckout({
      products: [data.productId],
      externalCustomerId: context.userId,
      successUrl: `${env.APP_URL}/checkout/success`,
      returnUrl: `${env.APP_URL}/subscribe`
    });
    return {
      url: checkout.url
    };
  } catch (error) {
    console.error("Checkout creation error:", error);
    throw new Error("Failed to create checkout session");
  }
});
export {
  createCheckoutUrl_createServerFn_handler
};
