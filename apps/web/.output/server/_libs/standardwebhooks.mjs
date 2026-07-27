import { r as requireBase64 } from "../_chunks/_libs/@stablelib/base64.mjs";
import { r as requireSha256 } from "./fast-sha256.mjs";
var dist = {};
var timing_safe_equal = {};
var hasRequiredTiming_safe_equal;
function requireTiming_safe_equal() {
  if (hasRequiredTiming_safe_equal) return timing_safe_equal;
  hasRequiredTiming_safe_equal = 1;
  Object.defineProperty(timing_safe_equal, "__esModule", { value: true });
  timing_safe_equal.timingSafeEqual = void 0;
  function assert(expr, msg = "") {
    if (!expr) {
      throw new Error(msg);
    }
  }
  function timingSafeEqual(a, b) {
    if (a.byteLength !== b.byteLength) {
      return false;
    }
    if (!(a instanceof DataView)) {
      a = new DataView(ArrayBuffer.isView(a) ? a.buffer : a);
    }
    if (!(b instanceof DataView)) {
      b = new DataView(ArrayBuffer.isView(b) ? b.buffer : b);
    }
    assert(a instanceof DataView);
    assert(b instanceof DataView);
    const length = a.byteLength;
    let out = 0;
    let i = -1;
    while (++i < length) {
      out |= a.getUint8(i) ^ b.getUint8(i);
    }
    return out === 0;
  }
  timing_safe_equal.timingSafeEqual = timingSafeEqual;
  return timing_safe_equal;
}
var hasRequiredDist;
function requireDist() {
  if (hasRequiredDist) return dist;
  hasRequiredDist = 1;
  Object.defineProperty(dist, "__esModule", { value: true });
  dist.Webhook = dist.WebhookVerificationError = void 0;
  const timing_safe_equal_1 = /* @__PURE__ */ requireTiming_safe_equal();
  const base64 = /* @__PURE__ */ requireBase64();
  const sha256 = /* @__PURE__ */ requireSha256();
  const WEBHOOK_TOLERANCE_IN_SECONDS = 5 * 60;
  class ExtendableError extends Error {
    constructor(message) {
      super(message);
      Object.setPrototypeOf(this, ExtendableError.prototype);
      this.name = "ExtendableError";
      this.stack = new Error(message).stack;
    }
  }
  class WebhookVerificationError extends ExtendableError {
    constructor(message) {
      super(message);
      Object.setPrototypeOf(this, WebhookVerificationError.prototype);
      this.name = "WebhookVerificationError";
    }
  }
  dist.WebhookVerificationError = WebhookVerificationError;
  class Webhook {
    constructor(secret, options) {
      if (!secret) {
        throw new Error("Secret can't be empty.");
      }
      if ((options === null || options === void 0 ? void 0 : options.format) === "raw") {
        if (secret instanceof Uint8Array) {
          this.key = secret;
        } else {
          this.key = Uint8Array.from(secret, (c) => c.charCodeAt(0));
        }
      } else {
        if (typeof secret !== "string") {
          throw new Error("Expected secret to be of type string");
        }
        if (secret.startsWith(Webhook.prefix)) {
          secret = secret.substring(Webhook.prefix.length);
        }
        this.key = base64.decode(secret);
      }
    }
    verify(payload, headers_) {
      const headers = {};
      for (const key of Object.keys(headers_)) {
        headers[key.toLowerCase()] = headers_[key];
      }
      const msgId = headers["webhook-id"];
      const msgSignature = headers["webhook-signature"];
      const msgTimestamp = headers["webhook-timestamp"];
      if (!msgSignature || !msgId || !msgTimestamp) {
        throw new WebhookVerificationError("Missing required headers");
      }
      const timestamp = this.verifyTimestamp(msgTimestamp);
      const computedSignature = this.sign(msgId, timestamp, payload);
      const expectedSignature = computedSignature.split(",")[1];
      const passedSignatures = msgSignature.split(" ");
      const encoder = new globalThis.TextEncoder();
      for (const versionedSignature of passedSignatures) {
        const [version, signature] = versionedSignature.split(",");
        if (version !== "v1") {
          continue;
        }
        if ((0, timing_safe_equal_1.timingSafeEqual)(encoder.encode(signature), encoder.encode(expectedSignature))) {
          return JSON.parse(payload.toString());
        }
      }
      throw new WebhookVerificationError("No matching signature found");
    }
    sign(msgId, timestamp, payload) {
      if (typeof payload === "string") ;
      else if (payload.constructor.name === "Buffer") {
        payload = payload.toString();
      } else {
        throw new Error("Expected payload to be of type string or Buffer.");
      }
      const encoder = new TextEncoder();
      const timestampNumber = Math.floor(timestamp.getTime() / 1e3);
      const toSign = encoder.encode(`${msgId}.${timestampNumber}.${payload}`);
      const expectedSignature = base64.encode(sha256.hmac(this.key, toSign));
      return `v1,${expectedSignature}`;
    }
    verifyTimestamp(timestampHeader) {
      const now = Math.floor(Date.now() / 1e3);
      const timestamp = parseInt(timestampHeader, 10);
      if (isNaN(timestamp)) {
        throw new WebhookVerificationError("Invalid Signature Headers");
      }
      if (now - timestamp > WEBHOOK_TOLERANCE_IN_SECONDS) {
        throw new WebhookVerificationError("Message timestamp too old");
      }
      if (timestamp > now + WEBHOOK_TOLERANCE_IN_SECONDS) {
        throw new WebhookVerificationError("Message timestamp too new");
      }
      return new Date(timestamp * 1e3);
    }
  }
  dist.Webhook = Webhook;
  Webhook.prefix = "whsec_";
  return dist;
}
export {
  requireDist as r
};
