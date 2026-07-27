import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { a as authMiddleware, l as lazycloudApi } from "./auth-CoRbzC04.mjs";
import { c as createServerFn } from "./index.mjs";
import { j as object, k as string } from "../_libs/zod.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
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
const getApiKeys_createServerFn_handler = createServerRpc({
  id: "5d20e2c077eda69233dba919b1b2815dda0a123d2ed70cff9e492d94232379d8",
  name: "getApiKeys",
  filename: "src/server/functions/api-keys.ts"
}, (opts, signal) => getApiKeys.__executeServer(opts, signal));
const getApiKeys = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getApiKeys_createServerFn_handler, async ({
  context
}) => {
  return lazycloudApi.getApiKeys(context.accessToken);
});
const regenerateApiKey_createServerFn_handler = createServerRpc({
  id: "2f6fc0d8be65e9c3bba560f4f67557575a92bb1640b87648d00dc91c04df7808",
  name: "regenerateApiKey",
  filename: "src/server/functions/api-keys.ts"
}, (opts, signal) => regenerateApiKey.__executeServer(opts, signal));
const regenerateApiKey = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  apiKeyId: string()
})).handler(regenerateApiKey_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.regenerateApiKey(context.accessToken, data.apiKeyId);
});
export {
  getApiKeys_createServerFn_handler,
  regenerateApiKey_createServerFn_handler
};
