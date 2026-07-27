import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { p as polarService } from "./polar-DEJBJJXZ.mjs";
import { c as createServerFn } from "./index.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_libs/zod.mjs";
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
const getProducts_createServerFn_handler = createServerRpc({
  id: "6170953db43debea209362278583bf05180f81afe75c48b7a05ab67c217b4f38",
  name: "getProducts",
  filename: "src/server/functions/products.ts"
}, (opts, signal) => getProducts.__executeServer(opts, signal));
const getProducts = createServerFn({
  method: "GET"
}).handler(getProducts_createServerFn_handler, async () => {
  try {
    const {
      result
    } = await polarService.listProducts({
      isArchived: false
    });
    if (!result?.items) {
      throw new Error("Invalid response from Polar API: missing items");
    }
    const products = result.items.sort((a, b) => {
      const aIsBasic = a.name.toLowerCase().includes("basic");
      const bIsBasic = b.name.toLowerCase().includes("basic");
      if (aIsBasic && !bIsBasic) return -1;
      if (!aIsBasic && bIsBasic) return 1;
      const aPrice = a.prices[0]?.amountType === "fixed" ? a.prices[0]?.priceAmount ?? 0 : 0;
      const bPrice = b.prices[0]?.amountType === "fixed" ? b.prices[0]?.priceAmount ?? 0 : 0;
      return aPrice - bPrice;
    });
    return products;
  } catch (error) {
    console.error("Error fetching products:", {
      error,
      message: error instanceof Error ? error.message : String(error)
    });
    return [];
  }
});
export {
  getProducts_createServerFn_handler
};
