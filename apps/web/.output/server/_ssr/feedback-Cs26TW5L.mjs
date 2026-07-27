import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { a as authMiddleware, l as lazycloudApi } from "./auth-CoRbzC04.mjs";
import { c as createServerFn } from "./index.mjs";
import { j as object, k as string, _ as _enum } from "../_libs/zod.mjs";
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
const feedbackTypes = ["bug", "feature", "other"];
const submitFeedback_createServerFn_handler = createServerRpc({
  id: "0d5a9560ddf4d1f5c5942f2829f145fc1a9bdcd600f9d3e0d57d71deb34caaa3",
  name: "submitFeedback",
  filename: "src/server/functions/feedback.ts"
}, (opts, signal) => submitFeedback.__executeServer(opts, signal));
const submitFeedback = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  feedbackType: _enum(feedbackTypes),
  message: string().min(10, "Message must be at least 10 characters").max(5e3, "Message is too long (max 5000 characters)")
})).handler(submitFeedback_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.submitFeedback(context.accessToken, data.feedbackType, data.message, "web");
});
export {
  submitFeedback_createServerFn_handler
};
