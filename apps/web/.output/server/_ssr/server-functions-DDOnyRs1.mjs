import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { E as redirect } from "../_chunks/_libs/@tanstack/router-core.mjs";
import { g as getRawAuthFromContext, a as getRedirectUriFromContext, r as refreshSession } from "./auth-helpers-JYeFctGZ.mjs";
import { g as getAuthkit } from "./authkit-loader-BpUdXche.mjs";
import { c as createServerFn } from "./index.mjs";
import "../_libs/cookie-es.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "../_chunks/_libs/@workos/authkit-session.mjs";
import "../_chunks/_libs/@workos-inc/node.mjs";
import "../_libs/iron-webcrypto.mjs";
import "../_libs/uint8array-extras.mjs";
import "../_libs/jose.mjs";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_chunks/_libs/react.mjs";
import "../_chunks/_libs/@tanstack/react-router.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
const getSignOutUrl_createServerFn_handler = createServerRpc({
  id: "7be4f3f67f835c7721278639b4cf286335026dc9c8df2d003bd35bccb299b3aa",
  name: "getSignOutUrl",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => getSignOutUrl.__executeServer(opts, signal));
const getSignOutUrl = createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(getSignOutUrl_createServerFn_handler, async ({
  data
}) => {
  const auth = getAuthFromContext();
  if (!auth.user || !auth.sessionId) {
    return {
      url: null
    };
  }
  const authkit = await getAuthkit();
  const {
    logoutUrl
  } = await authkit.signOut(auth.sessionId, {
    returnTo: data?.returnTo
  });
  return {
    url: logoutUrl
  };
});
const signOut_createServerFn_handler = createServerRpc({
  id: "2771d306e92d595151b6182fd5cf0871e36cdcfb9fe6b4342da1e3d463652a42",
  name: "signOut",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => signOut.__executeServer(opts, signal));
const signOut = createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(signOut_createServerFn_handler, async ({
  data
}) => {
  const auth = getAuthFromContext();
  if (!auth.user || !auth.sessionId) {
    throw redirect({
      to: data?.returnTo || "/",
      throw: true,
      reloadDocument: true
    });
  }
  const authkit = await getAuthkit();
  const {
    logoutUrl,
    headers: headersBag
  } = await authkit.signOut(auth.sessionId, {
    returnTo: data?.returnTo
  });
  const headers = new Headers();
  if (headersBag) {
    for (const [key, value] of Object.entries(headersBag)) {
      if (Array.isArray(value)) {
        value.forEach((v) => headers.append(key, v));
      } else {
        headers.set(key, value);
      }
    }
  }
  throw redirect({
    href: logoutUrl,
    throw: true,
    reloadDocument: true,
    headers
  });
});
function getAuthFromContext() {
  const auth = getRawAuthFromContext();
  if (!auth.user) {
    return {
      user: null
    };
  }
  return {
    user: auth.user,
    sessionId: auth.sessionId,
    organizationId: auth.claims?.org_id,
    role: auth.claims?.role,
    roles: auth.claims?.roles,
    permissions: auth.claims?.permissions,
    entitlements: auth.claims?.entitlements,
    featureFlags: auth.claims?.feature_flags,
    impersonator: auth.impersonator,
    accessToken: auth.accessToken
  };
}
const getAuth_createServerFn_handler = createServerRpc({
  id: "4ecbfcb5f42faeafc3cdb09e6a6439b914ef8dfe50140cdf7b4a3ec86bcbb687",
  name: "getAuth",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => getAuth.__executeServer(opts, signal));
const getAuth = createServerFn({
  method: "GET"
}).handler(getAuth_createServerFn_handler, () => {
  return getAuthFromContext();
});
const getAuthorizationUrl_createServerFn_handler = createServerRpc({
  id: "b798b2ce6f01778c45e438761fc4a97a0c74e9956f750ec647c187278a85a637",
  name: "getAuthorizationUrl",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => getAuthorizationUrl.__executeServer(opts, signal));
const getAuthorizationUrl = createServerFn({
  method: "GET"
}).inputValidator((options) => options).handler(getAuthorizationUrl_createServerFn_handler, async ({
  data: options = {}
}) => {
  const authkit = await getAuthkit();
  const contextRedirectUri = getRedirectUriFromContext();
  if (contextRedirectUri && !options.redirectUri) {
    return authkit.getAuthorizationUrl({
      ...options,
      redirectUri: contextRedirectUri
    });
  }
  return authkit.getAuthorizationUrl(options);
});
const getSignInUrl_createServerFn_handler = createServerRpc({
  id: "cd661a1c0e05868ca5865a5775b777eac01f13ed0648161834275b0b4faaf479",
  name: "getSignInUrl",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => getSignInUrl.__executeServer(opts, signal));
const getSignInUrl = createServerFn({
  method: "GET"
}).inputValidator((data) => data).handler(getSignInUrl_createServerFn_handler, async ({
  data
}) => {
  const options = typeof data === "string" ? {
    returnPathname: data
  } : data;
  const contextRedirectUri = getRedirectUriFromContext();
  const authkit = await getAuthkit();
  if (contextRedirectUri && !options?.redirectUri) {
    return authkit.getSignInUrl({
      ...options,
      redirectUri: contextRedirectUri
    });
  }
  return authkit.getSignInUrl(options);
});
const getSignUpUrl_createServerFn_handler = createServerRpc({
  id: "8a1b7363180c62efb62991a3faa87594c774ba4b5a9de9b5ec117d2ce007fddb",
  name: "getSignUpUrl",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => getSignUpUrl.__executeServer(opts, signal));
const getSignUpUrl = createServerFn({
  method: "GET"
}).inputValidator((data) => data).handler(getSignUpUrl_createServerFn_handler, async ({
  data
}) => {
  const options = typeof data === "string" ? {
    returnPathname: data
  } : data;
  const contextRedirectUri = getRedirectUriFromContext();
  const authkit = await getAuthkit();
  if (contextRedirectUri && !options?.redirectUri) {
    return authkit.getSignUpUrl({
      ...options,
      redirectUri: contextRedirectUri
    });
  }
  return authkit.getSignUpUrl(options);
});
const switchToOrganization_createServerFn_handler = createServerRpc({
  id: "5f24552b9f049332d1d320fde6fc92f2a0a67aa3c8e2d907d49c1d3207c1d457",
  name: "switchToOrganization",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/server-functions.js"
}, (opts, signal) => switchToOrganization.__executeServer(opts, signal));
const switchToOrganization = createServerFn({
  method: "POST"
}).inputValidator((data) => data).handler(switchToOrganization_createServerFn_handler, async ({
  data
}) => {
  const auth = getAuthFromContext();
  if (!auth.user) {
    throw redirect({
      to: data.returnTo || "/"
    });
  }
  const result = await refreshSession(data.organizationId);
  if (!result?.user) {
    throw redirect({
      to: data.returnTo || "/"
    });
  }
  return {
    user: result.user,
    sessionId: result.sessionId,
    organizationId: result.claims?.org_id,
    role: result.claims?.role,
    roles: result.claims?.roles,
    permissions: result.claims?.permissions,
    entitlements: result.claims?.entitlements,
    featureFlags: result.claims?.feature_flags,
    impersonator: result.impersonator,
    accessToken: result.accessToken
  };
});
export {
  getAuth_createServerFn_handler,
  getAuthorizationUrl_createServerFn_handler,
  getSignInUrl_createServerFn_handler,
  getSignOutUrl_createServerFn_handler,
  getSignUpUrl_createServerFn_handler,
  signOut_createServerFn_handler,
  switchToOrganization_createServerFn_handler
};
