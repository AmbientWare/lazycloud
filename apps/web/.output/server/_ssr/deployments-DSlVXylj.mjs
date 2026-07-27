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
const getDeploymentStatus_createServerFn_handler = createServerRpc({
  id: "f915f4f0f965f5cb6ce53ed9393f618178b75dd4e74fb5ac9e8ba738fe3c7722",
  name: "getDeploymentStatus",
  filename: "src/server/functions/deployments.ts"
}, (opts, signal) => getDeploymentStatus.__executeServer(opts, signal));
const getDeploymentStatus = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  deploymentId: string()
})).handler(getDeploymentStatus_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getDeploymentStatus(context.accessToken, data.deploymentId);
});
export {
  getDeploymentStatus_createServerFn_handler
};
