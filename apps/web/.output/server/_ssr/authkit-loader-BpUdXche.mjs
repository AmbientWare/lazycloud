import { g as getStartContext } from "./index.mjs";
import { v as validateConfig$1, c as createAuthService, C as CookieSessionStorage } from "../_chunks/_libs/@workos/authkit-session.mjs";
const getGlobalStartContext = () => {
  const context = getStartContext().contextAfterGlobalMiddlewares;
  if (!context) {
    throw new Error(`Global context not set yet, you are calling getGlobalStartContext() before the global middlewares are applied.`);
  }
  return context;
};
const MIDDLEWARE_NOT_CONFIGURED_ERROR = `AuthKit middleware is not configured.

Add authkitMiddleware() to your app.tsx file:

import { authkitMiddleware } from '@workos/authkit-tanstack-start';

export default createRouter({
  routeTree,
  context: { ... },
  middleware: [authkitMiddleware()],
});

See the documentation for more details: https://github.com/workos/authkit-tanstack-start`;
function getAuthKitContext() {
  const ctx = getGlobalStartContext();
  if (!ctx?.auth || !ctx?.request) {
    throw new Error(MIDDLEWARE_NOT_CONFIGURED_ERROR);
  }
  return ctx;
}
function getAuthKitContextOrNull() {
  try {
    const ctx = getGlobalStartContext();
    return ctx?.auth && ctx?.request ? ctx : null;
  } catch {
    return null;
  }
}
class TanStackStartCookieSessionStorage extends CookieSessionStorage {
  async getSession(request) {
    const cookieHeader = request.headers.get("cookie");
    if (!cookieHeader)
      return null;
    const cookies = this.parseCookies(cookieHeader);
    const value = cookies[this.cookieName];
    return value ? decodeURIComponent(value) : null;
  }
  async applyHeaders(response, headers) {
    const ctx = getAuthKitContextOrNull();
    if (ctx?.__setPendingHeader) {
      Object.entries(headers).forEach(([key, value]) => ctx.__setPendingHeader(key, value));
      return { response: response ?? new Response() };
    }
    const newResponse = response ? new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: new Headers(response.headers)
    }) : new Response();
    Object.entries(headers).forEach(([key, value]) => newResponse.headers.append(key, value));
    return { response: newResponse };
  }
  parseCookies(cookieHeader) {
    return Object.fromEntries(cookieHeader.split(";").map((cookie) => {
      const [key, ...valueParts] = cookie.trim().split("=");
      return [key, valueParts.join("=")];
    }));
  }
}
let authkitInstance;
async function getAuthkit() {
  if (!authkitInstance) {
    authkitInstance = createAuthService({
      sessionStorageFactory: (config) => new TanStackStartCookieSessionStorage(config)
    });
  }
  return authkitInstance;
}
async function validateConfig() {
  return validateConfig$1();
}
export {
  getAuthKitContext as a,
  getAuthKitContextOrNull as b,
  getAuthkit as g,
  validateConfig as v
};
