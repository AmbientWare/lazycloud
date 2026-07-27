import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { j as jsxRuntimeExports } from "../_chunks/_libs/react.mjs";
import { R as Resend } from "../_libs/resend.mjs";
import { e as env } from "./env-vgS3Y8Xp.mjs";
import { L as LAZYCLOUD_DOMAIN } from "./constants-Cg_QsTl0.mjs";
import { d as distExports } from "../_chunks/_libs/@upstash/ratelimit.mjs";
import { r as redis } from "./redis-BTsw1DHd.mjs";
import { c as createServerFn } from "./index.mjs";
import { H as Html } from "../_chunks/_libs/@react-email/html.mjs";
import { H as Head } from "../_chunks/_libs/@react-email/head.mjs";
import { B as Body } from "../_chunks/_libs/@react-email/body.mjs";
import { H as Heading } from "../_chunks/_libs/@react-email/heading.mjs";
import { T as Text } from "../_chunks/_libs/@react-email/text.mjs";
import { j as object, k as string } from "../_libs/zod.mjs";
import "../_libs/svix.mjs";
import "../_libs/uuid.mjs";
import "node:crypto";
import "../_libs/standardwebhooks.mjs";
import "../_chunks/_libs/@stablelib/base64.mjs";
import "../_libs/fast-sha256.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_chunks/_libs/@upstash/core-analytics.mjs";
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
import "../_chunks/_libs/@tanstack/react-router.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
function RequestAccessEmailTemplate({
  title,
  body
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(Html, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(Head, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(
      Body,
      {
        style: {
          fontFamily: "Arial, sans-serif",
          lineHeight: 1.6,
          color: "#333",
          maxWidth: "600px",
          margin: "0 auto",
          padding: "20px"
        },
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(
            Heading,
            {
              style: {
                color: "#2563eb",
                borderBottom: "2px solid #2563eb",
                paddingBottom: "10px"
              },
              children: title
            }
          ),
          /* @__PURE__ */ jsxRuntimeExports.jsx(Text, { style: { fontSize: "16px", marginTop: "20px" }, children: body })
        ]
      }
    )
  ] });
}
class ResendService {
  _resend = null;
  _supportEmail = env.SUPPORT_EMAIL;
  get resend() {
    this._resend ??= new Resend(env.RESEND_API_KEY);
    return this._resend;
  }
  async emailSupport(subject, title, body) {
    const { data, error } = await this.resend.emails.send({
      to: this._supportEmail,
      from: `LazyCloud Admin <admin@${LAZYCLOUD_DOMAIN}>`,
      subject,
      react: RequestAccessEmailTemplate({ title, body })
    });
    if (error) {
      throw new Error(error.message);
    }
    return data;
  }
}
const resendService = new ResendService();
const ephemeralCache = /* @__PURE__ */ new Map();
redis ? new distExports.Ratelimit({
  redis,
  limiter: distExports.Ratelimit.slidingWindow(300, "1 m"),
  ephemeralCache,
  analytics: false
}) : null;
const supportRatelimit = redis ? new distExports.Ratelimit({
  redis,
  limiter: distExports.Ratelimit.slidingWindow(3, "1 h"),
  ephemeralCache,
  analytics: false
}) : null;
async function checkEmailRateLimit(email) {
  if (!supportRatelimit) {
    return {
      allowed: true
    };
  }
  const identifier = `email:${email.toLowerCase()}`;
  const {
    success
  } = await supportRatelimit.limit(identifier);
  if (!success) {
    return {
      allowed: false,
      message: "Too many email requests. Please wait before submitting another request."
    };
  }
  return {
    allowed: true
  };
}
const sendSupportEmail_createServerFn_handler = createServerRpc({
  id: "1c2040b575cf992c1f1f3e380e99a82378e3ccc63a2b068965443701d0145bcb",
  name: "sendSupportEmail",
  filename: "src/server/functions/email.ts"
}, (opts, signal) => sendSupportEmail.__executeServer(opts, signal));
const sendSupportEmail = createServerFn({
  method: "POST"
}).inputValidator(object({
  email: string().email(),
  description: string().min(10).max(5e3).refine((val) => {
    const suspiciousPatterns = [/(http|https):\/\//gi, /\[url\]/gi, /<script/gi, /javascript:/gi];
    return !suspiciousPatterns.some((pattern) => pattern.test(val));
  }, {
    message: "Description contains invalid content"
  })
})).handler(sendSupportEmail_createServerFn_handler, async ({
  data
}) => {
  const rateLimit = await checkEmailRateLimit(data.email);
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }
  const subject = "SUPPORT REQUESTED";
  const title = "The following user has requested support:";
  const body = `Email: ${data.email}
Description: ${data.description}`;
  return resendService.emailSupport(subject, title, body);
});
const sendEnterpriseInquiry_createServerFn_handler = createServerRpc({
  id: "e74e87ad4e46f4c9ce1a8c813b6879b1311e092b49cf2d54f7c7889a85ebfa88",
  name: "sendEnterpriseInquiry",
  filename: "src/server/functions/email.ts"
}, (opts, signal) => sendEnterpriseInquiry.__executeServer(opts, signal));
const sendEnterpriseInquiry = createServerFn({
  method: "POST"
}).inputValidator(object({
  email: string().email(),
  name: string().optional(),
  workosId: string().optional(),
  message: string().optional()
})).handler(sendEnterpriseInquiry_createServerFn_handler, async ({
  data
}) => {
  const rateLimit = await checkEmailRateLimit(data.email);
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }
  const subject = "ENTERPRISE INQUIRY";
  const title = "The following user has requested Enterprise plan information:";
  let body = `Email: ${data.email}`;
  if (data.name) {
    body += `
Name: ${data.name}`;
  }
  if (data.workosId) {
    body += `
WorkOS ID: ${data.workosId}`;
  }
  if (data.message?.trim()) {
    body += `

Message:
${data.message.trim()}`;
  }
  return resendService.emailSupport(subject, title, body);
});
export {
  sendEnterpriseInquiry_createServerFn_handler,
  sendSupportEmail_createServerFn_handler
};
