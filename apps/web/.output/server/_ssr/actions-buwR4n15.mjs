import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { i as isAuthConfigured, g as getRawAuthFromContext, r as refreshSession } from "./auth-helpers-JYeFctGZ.mjs";
import { c as createServerFn } from "./index.mjs";
import "./authkit-loader-BpUdXche.mjs";
import "../_chunks/_libs/@workos/authkit-session.mjs";
import "../_chunks/_libs/@workos-inc/node.mjs";
import "../_libs/iron-webcrypto.mjs";
import "../_libs/uint8array-extras.mjs";
import "../_libs/jose.mjs";
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
function sanitizeAuthForClient(auth) {
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
    impersonator: auth.impersonator
  };
}
const checkSessionAction_createServerFn_handler = createServerRpc({
  id: "beb901f994389c5c0c00008954f76450a5ca41609be3f01c90b84175fd68aa42",
  name: "checkSessionAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => checkSessionAction.__executeServer(opts, signal));
const checkSessionAction = createServerFn({
  method: "GET"
}).handler(checkSessionAction_createServerFn_handler, () => {
  if (!isAuthConfigured()) {
    return false;
  }
  try {
    const auth = getRawAuthFromContext();
    return auth.user !== null;
  } catch {
    return false;
  }
});
const getAuthAction_createServerFn_handler = createServerRpc({
  id: "aaededb146d2203d005fa36ab5b5d314484d9a1f7cd4fb8336e12f8d37987595",
  name: "getAuthAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => getAuthAction.__executeServer(opts, signal));
const getAuthAction = createServerFn({
  method: "GET"
}).handler(getAuthAction_createServerFn_handler, () => {
  const auth = getRawAuthFromContext();
  return sanitizeAuthForClient(auth);
});
const refreshAuthAction_createServerFn_handler = createServerRpc({
  id: "4f1ff4edac354c905dc42f8e439a2cf29eec106f2d70a7b754eda5d2ad3d4ba2",
  name: "refreshAuthAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => refreshAuthAction.__executeServer(opts, signal));
const refreshAuthAction = createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(refreshAuthAction_createServerFn_handler, async ({
  data: options
}) => {
  const result = await refreshSession(options?.organizationId);
  if (!result || !result.user) {
    return {
      user: null
    };
  }
  return sanitizeAuthForClient(result);
});
const getAccessTokenAction_createServerFn_handler = createServerRpc({
  id: "911e878d72283a859f0e4dbad7654607c84fc1939bc1977c98e4a0da7c848d2e",
  name: "getAccessTokenAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => getAccessTokenAction.__executeServer(opts, signal));
const getAccessTokenAction = createServerFn({
  method: "GET"
}).handler(getAccessTokenAction_createServerFn_handler, () => {
  if (!isAuthConfigured()) {
    return void 0;
  }
  try {
    const auth = getRawAuthFromContext();
    return auth.user ? auth.accessToken : void 0;
  } catch {
    return void 0;
  }
});
const refreshAccessTokenAction_createServerFn_handler = createServerRpc({
  id: "517e6bb4401c6c1a8babb3b48fc65f77f67f670cfdaee291291469cbec53eaf6",
  name: "refreshAccessTokenAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => refreshAccessTokenAction.__executeServer(opts, signal));
const refreshAccessTokenAction = createServerFn({
  method: "POST"
}).handler(refreshAccessTokenAction_createServerFn_handler, async () => {
  const result = await refreshSession();
  return result?.user ? result.accessToken : void 0;
});
const switchToOrganizationAction_createServerFn_handler = createServerRpc({
  id: "220bd13c17cec445e2ef88d43e50cc6cad12c3569461bec48b58138c4a82848d",
  name: "switchToOrganizationAction",
  filename: "node_modules/.pnpm/@workos+authkit-tanstack-react-start@0.5.0_@tanstack+react-router@1.157.13_react-dom@19_c4717ba3ca74a2edd7a46d9f60eb5c81/node_modules/@workos/authkit-tanstack-react-start/dist/server/actions.js"
}, (opts, signal) => switchToOrganizationAction.__executeServer(opts, signal));
const switchToOrganizationAction = createServerFn({
  method: "POST"
}).inputValidator((data) => data).handler(switchToOrganizationAction_createServerFn_handler, async ({
  data
}) => {
  const result = await refreshSession(data.organizationId);
  if (!result || !result.user) {
    return {
      user: null
    };
  }
  return sanitizeAuthForClient(result);
});
export {
  checkSessionAction_createServerFn_handler,
  getAccessTokenAction_createServerFn_handler,
  getAuthAction_createServerFn_handler,
  refreshAccessTokenAction_createServerFn_handler,
  refreshAuthAction_createServerFn_handler,
  switchToOrganizationAction_createServerFn_handler
};
