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
const getAggregatedUsage_createServerFn_handler = createServerRpc({
  id: "2243ab7cd7f6aff84f77e3571d2e804865ec5f968d9141cb3c70bc00e12ef664",
  name: "getAggregatedUsage",
  filename: "src/server/functions/usage.ts"
}, (opts, signal) => getAggregatedUsage.__executeServer(opts, signal));
const getAggregatedUsage = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional()
}).optional()).handler(getAggregatedUsage_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getAggregatedUsage(context.accessToken, data?.startDate, data?.endDate);
});
const getAggregatedDailyUsage_createServerFn_handler = createServerRpc({
  id: "74529d40a3644b4c3e040f5f938da6bb1c4e0104887e69007c5aa278f701dc02",
  name: "getAggregatedDailyUsage",
  filename: "src/server/functions/usage.ts"
}, (opts, signal) => getAggregatedDailyUsage.__executeServer(opts, signal));
const getAggregatedDailyUsage = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional(),
  timezone: string().optional()
}).optional()).handler(getAggregatedDailyUsage_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getAggregatedDailyUsage(context.accessToken, data?.startDate, data?.endDate, data?.timezone);
});
const getDeploymentCostBreakdown_createServerFn_handler = createServerRpc({
  id: "de779335b0fbffd148bb30caa69328003019d2da049a9a7ecb3c6d6d9d8d05fa",
  name: "getDeploymentCostBreakdown",
  filename: "src/server/functions/usage.ts"
}, (opts, signal) => getDeploymentCostBreakdown.__executeServer(opts, signal));
const getDeploymentCostBreakdown = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  deploymentId: string(),
  startDate: string().optional(),
  endDate: string().optional()
})).handler(getDeploymentCostBreakdown_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getDeploymentCostBreakdown(context.accessToken, data.deploymentId, data.startDate, data.endDate);
});
const getMeterPricing_createServerFn_handler = createServerRpc({
  id: "e64a2cb27fda0a3fa990d8d096e473613d6c08ef0e56a71d1b9079a723f1a55c",
  name: "getMeterPricing",
  filename: "src/server/functions/usage.ts"
}, (opts, signal) => getMeterPricing.__executeServer(opts, signal));
const getMeterPricing = createServerFn({
  method: "GET"
}).handler(getMeterPricing_createServerFn_handler, async () => {
  return lazycloudApi.getMeterPricing();
});
export {
  getAggregatedDailyUsage_createServerFn_handler,
  getAggregatedUsage_createServerFn_handler,
  getDeploymentCostBreakdown_createServerFn_handler,
  getMeterPricing_createServerFn_handler
};
