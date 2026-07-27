import { a as getAuthKitContext, b as getAuthKitContextOrNull, g as getAuthkit } from "./authkit-loader-BpUdXche.mjs";
function getRawAuthFromContext() {
  const ctx = getAuthKitContext();
  return ctx.auth();
}
function isAuthConfigured() {
  return getAuthKitContextOrNull() !== null;
}
function getRedirectUriFromContext() {
  const ctx = getAuthKitContextOrNull();
  return ctx?.redirectUri;
}
async function getSessionWithRefreshToken() {
  const auth = getRawAuthFromContext();
  if (!auth.user || !auth.accessToken) {
    return null;
  }
  const ctx = getAuthKitContext();
  const authkit = await getAuthkit();
  const session = await authkit.getSession(ctx.request);
  if (!session?.refreshToken) {
    return null;
  }
  return {
    refreshToken: session.refreshToken,
    accessToken: auth.accessToken,
    user: auth.user,
    impersonator: auth.impersonator
  };
}
async function refreshSession(organizationId) {
  const sessionData = await getSessionWithRefreshToken();
  if (!sessionData) {
    return null;
  }
  const authkit = await getAuthkit();
  const { auth: result, encryptedSession } = await authkit.refreshSession({
    accessToken: sessionData.accessToken,
    refreshToken: sessionData.refreshToken,
    user: sessionData.user,
    impersonator: sessionData.impersonator
  }, organizationId);
  if (encryptedSession) {
    await authkit.saveSession(void 0, encryptedSession);
  }
  return result;
}
export {
  getRedirectUriFromContext as a,
  getRawAuthFromContext as g,
  isAuthConfigured as i,
  refreshSession as r
};
