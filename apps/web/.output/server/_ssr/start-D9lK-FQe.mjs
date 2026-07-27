import { c as createMiddleware } from "./createMiddleware-CRzJRBrm.mjs";
import { g as getAuthkit, v as validateConfig } from "./authkit-loader-BpUdXche.mjs";
import "./index.mjs";
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
import "../_chunks/_libs/@workos/authkit-session.mjs";
import "../_chunks/_libs/@workos-inc/node.mjs";
import "../_libs/iron-webcrypto.mjs";
import "../_libs/uint8array-extras.mjs";
import "../_libs/jose.mjs";
function dedupeSerializationAdapters(deduped, serializationAdapters) {
  for (let i = 0, len = serializationAdapters.length; i < len; i++) {
    const current = serializationAdapters[i];
    if (!deduped.has(current)) {
      deduped.add(current);
      if (current.extends) {
        dedupeSerializationAdapters(deduped, current.extends);
      }
    }
  }
}
const createStart = (getOptions) => {
  return {
    getOptions: async () => {
      const options = await getOptions();
      if (options.serializationAdapters) {
        const deduped = /* @__PURE__ */ new Set();
        dedupeSerializationAdapters(
          deduped,
          options.serializationAdapters
        );
        options.serializationAdapters = Array.from(deduped);
      }
      return options;
    },
    createMiddleware
  };
};
let configValidated = false;
const authkitMiddleware = (options) => {
  return createMiddleware().server(async (args) => {
    const authkit = await getAuthkit();
    if (!configValidated) {
      await validateConfig();
      configValidated = true;
    }
    const { auth, refreshedSessionData } = await authkit.withAuth(args.request);
    const pendingHeaders = new Headers();
    const result = await args.next({
      context: {
        auth: () => auth,
        request: args.request,
        redirectUri: options?.redirectUri,
        __setPendingHeader: (key, value) => {
          if (key.toLowerCase() === "set-cookie") {
            pendingHeaders.append(key, value);
          } else {
            pendingHeaders.set(key, value);
          }
        }
      }
    });
    if (refreshedSessionData) {
      const { response: sessionResponse } = await authkit.saveSession(void 0, refreshedSessionData);
      const setCookieHeader = sessionResponse?.headers.get("Set-Cookie");
      if (setCookieHeader) {
        pendingHeaders.append("Set-Cookie", setCookieHeader);
      }
    }
    const headerEntries = [...pendingHeaders];
    if (headerEntries.length === 0) {
      return result;
    }
    const newResponse = new Response(result.response.body, {
      status: result.response.status,
      statusText: result.response.statusText,
      headers: result.response.headers
    });
    for (const [key, value] of headerEntries) {
      if (key.toLowerCase() === "set-cookie") {
        newResponse.headers.append(key, value);
      } else {
        newResponse.headers.set(key, value);
      }
    }
    return { ...result, response: newResponse };
  });
};
const startInstance = createStart(() => ({
  requestMiddleware: [authkitMiddleware()]
}));
export {
  startInstance
};
