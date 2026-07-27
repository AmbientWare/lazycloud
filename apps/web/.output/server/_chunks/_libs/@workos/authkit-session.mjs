import { W as WorkOSNode } from "../@workos-inc/node.mjs";
import { s as seal, u as unseal } from "../../../_libs/iron-webcrypto.mjs";
import { createRemoteJWKSet, jwtVerify, decodeJwt } from "../../../_libs/jose.mjs";
function once(fn) {
  let called = false;
  let result;
  return (...args) => {
    if (!called) {
      result = fn(...args);
      called = true;
    }
    return result;
  };
}
class AuthKitError extends Error {
  data;
  constructor(message, cause, data) {
    super(message);
    this.name = "AuthKitError";
    this.cause = cause;
    this.data = data;
  }
}
class SessionEncryptionError extends AuthKitError {
  constructor(message, cause) {
    super(message, cause);
    this.name = "SessionEncryptionError";
  }
}
class TokenRefreshError extends AuthKitError {
  userId;
  sessionId;
  constructor(message, cause, context) {
    super(message, cause);
    this.name = "TokenRefreshError";
    this.userId = context?.userId;
    this.sessionId = context?.sessionId;
  }
}
class AuthKitCore {
  config;
  client;
  encryption;
  clientId;
  constructor(config, client, encryption) {
    this.config = config;
    this.client = client;
    this.encryption = encryption;
    this.clientId = config.clientId;
  }
  /**
   * JWKS public key fetcher - cached for performance
   */
  getPublicKey = once(() => createRemoteJWKSet(new URL(this.client.userManagement.getJwksUrl(this.clientId))));
  /**
   * Verify a JWT access token against WorkOS JWKS.
   *
   * @param token - The JWT access token to verify
   * @returns true if valid, false otherwise
   */
  async verifyToken(token) {
    try {
      await jwtVerify(token, this.getPublicKey());
      return true;
    } catch {
      return false;
    }
  }
  /**
   * Check if a token is expiring soon.
   *
   * @param token - The JWT access token
   * @param buffer - How many seconds before expiry to consider "expiring" (default: 60)
   * @returns true if token expires within buffer period
   */
  isTokenExpiring(token, buffer = 10) {
    const expiryTime = this.getTokenExpiryTime(token);
    if (!expiryTime) {
      return false;
    }
    const currentTime = Math.floor(Date.now() / 1e3);
    return expiryTime - currentTime <= buffer;
  }
  /**
   * Get the expiry time from a token's claims.
   *
   * @param token - The JWT access token
   * @returns Unix timestamp of expiry, or null if not present
   */
  getTokenExpiryTime(token) {
    const claims = this.parseTokenClaims(token);
    return claims.exp;
  }
  /**
   * Parse JWT claims from an access token.
   *
   * @param token - The JWT access token
   * @returns Decoded token claims
   * @throws Error if token is invalid
   */
  parseTokenClaims(token) {
    try {
      return decodeJwt(token);
    } catch (error) {
      throw new Error("Invalid token");
    }
  }
  /**
   * Encrypt a session object into a string suitable for cookie storage.
   *
   * @param session - The session to encrypt
   * @returns Encrypted session string
   * @throws SessionEncryptionError if encryption fails
   */
  async encryptSession(session) {
    try {
      const encryptedSession = await this.encryption.sealData(session, {
        password: this.config.cookiePassword,
        ttl: 0
      });
      return encryptedSession;
    } catch (error) {
      throw new SessionEncryptionError("Failed to encrypt session", error);
    }
  }
  /**
   * Decrypt an encrypted session string back into a session object.
   *
   * @param encryptedSession - The encrypted session string
   * @returns Decrypted session object
   * @throws SessionEncryptionError if decryption fails
   */
  async decryptSession(encryptedSession) {
    try {
      const session = await this.encryption.unsealData(encryptedSession, { password: this.config.cookiePassword });
      return session;
    } catch (error) {
      throw new SessionEncryptionError("Failed to decrypt session", error);
    }
  }
  /**
   * Refresh tokens using WorkOS API.
   *
   * @param refreshToken - The refresh token
   * @param organizationId - Optional organization ID to switch to
   * @param context - Optional context for error reporting (userId, sessionId)
   * @returns New access token, refresh token, user, and impersonator
   * @throws TokenRefreshError if refresh fails
   */
  async refreshTokens(refreshToken, organizationId, context) {
    try {
      const result = await this.client.userManagement.authenticateWithRefreshToken({
        refreshToken,
        clientId: this.clientId,
        organizationId
      });
      return {
        accessToken: result.accessToken,
        refreshToken: result.refreshToken,
        user: result.user,
        impersonator: result.impersonator
      };
    } catch (error) {
      throw new TokenRefreshError("Failed to refresh tokens", error, context);
    }
  }
  /**
   * Validate a session and refresh if needed.
   *
   * Only refreshes when token is invalid (expired) or force is true.
   *
   * @param session - The current session with access and refresh tokens
   * @param options - Optional settings
   * @param options.force - Force refresh even if token is valid (for org switching)
   * @param options.organizationId - Organization ID to switch to during refresh
   * @returns Validation result with refreshed session if needed
   * @throws TokenRefreshError if refresh fails
   */
  async validateAndRefresh(session, options) {
    const { accessToken } = session;
    const { force = false, organizationId: explicitOrgId } = options ?? {};
    const isValid = await this.verifyToken(accessToken);
    if (isValid && !force) {
      const claims = this.parseTokenClaims(accessToken);
      return { valid: true, refreshed: false, session, claims };
    }
    let organizationId = explicitOrgId;
    if (!organizationId && isValid) {
      try {
        const oldClaims = this.parseTokenClaims(accessToken);
        organizationId = oldClaims.org_id;
      } catch {
      }
    }
    let sessionId;
    try {
      sessionId = this.parseTokenClaims(accessToken).sid;
    } catch {
    }
    const newSession = await this.refreshTokens(session.refreshToken, organizationId, { userId: session.user?.id, sessionId });
    const newClaims = this.parseTokenClaims(newSession.accessToken);
    return {
      valid: true,
      refreshed: true,
      session: newSession,
      claims: newClaims
    };
  }
}
class AuthOperations {
  core;
  client;
  config;
  constructor(core, client, config) {
    this.core = core;
    this.client = client;
    this.config = config;
  }
  /**
   * Get the WorkOS logout URL.
   *
   * This only handles the WorkOS API part. Session clearing is handled
   * by the storage layer in AuthService.
   *
   * @param sessionId - The session ID to terminate
   * @param options - Optional return URL
   * @returns Logout URL
   */
  getLogoutUrl(sessionId, options) {
    return this.client.userManagement.getLogoutUrl({
      sessionId,
      returnTo: options?.returnTo
    });
  }
  /**
   * Switch to a different organization.
   *
   * This is a convenience wrapper around refreshSession() that enforces
   * an organization ID must be provided.
   *
   * @param session - Current session
   * @param organizationId - Organization ID to switch to (required)
   * @returns Auth result and encrypted session data
   */
  async switchOrganization(session, organizationId) {
    return this.refreshSession(session, organizationId);
  }
  /**
   * Refresh session operation.
   *
   * Forces a token refresh (for org switching or manual refresh),
   * encrypts the new session, and returns the auth result.
   *
   * @param session - Current session with refresh token
   * @param organizationId - Optional organization ID to switch to during refresh
   * @returns Auth result and encrypted session data
   */
  async refreshSession(session, organizationId) {
    const { session: newSession, claims } = await this.core.validateAndRefresh(session, { force: true, organizationId });
    const encryptedSession = await this.core.encryptSession(newSession);
    const auth = {
      user: newSession.user,
      sessionId: claims.sid,
      impersonator: newSession.impersonator,
      accessToken: newSession.accessToken,
      refreshToken: newSession.refreshToken,
      claims,
      organizationId: claims.org_id,
      role: claims.role,
      roles: claims.roles,
      permissions: claims.permissions,
      entitlements: claims.entitlements,
      featureFlags: claims.feature_flags
    };
    return {
      auth,
      encryptedSession
    };
  }
  /**
   * Get authorization URL for WorkOS authentication.
   *
   * State encoding format: `{internal}.{userState}` where internal is URL-safe
   * base64 encoded JSON containing returnPathname. This allows customers to
   * pass their own state through the OAuth flow.
   *
   * @param options - Authorization URL options (returnPathname, screenHint, state, etc.)
   * @returns The authorization URL
   */
  async getAuthorizationUrl(options = {}) {
    const internalState = options.returnPathname ? btoa(JSON.stringify({ returnPathname: options.returnPathname })).replace(/\+/g, "-").replace(/\//g, "_") : null;
    const state = internalState && options.state ? `${internalState}.${options.state}` : internalState || options.state || void 0;
    return this.client.userManagement.getAuthorizationUrl({
      provider: "authkit",
      redirectUri: options.redirectUri ?? this.config.redirectUri,
      screenHint: options.screenHint,
      organizationId: options.organizationId,
      loginHint: options.loginHint,
      prompt: options.prompt,
      clientId: this.config.clientId,
      state
    });
  }
  /**
   * Convenience method: Get sign-in URL.
   */
  async getSignInUrl(options = {}) {
    return this.getAuthorizationUrl({
      ...options,
      screenHint: "sign-in"
    });
  }
  /**
   * Convenience method: Get sign-up URL.
   */
  async getSignUpUrl(options = {}) {
    return this.getAuthorizationUrl({
      ...options,
      screenHint: "sign-up"
    });
  }
}
class AuthService {
  core;
  operations;
  storage;
  config;
  client;
  constructor(config, storage, client, encryption) {
    this.config = config;
    this.storage = storage;
    this.client = client;
    this.core = new AuthKitCore(config, client, encryption);
    this.operations = new AuthOperations(this.core, client, config);
  }
  /**
   * Main authentication check method.
   *
   * This method:
   * 1. Reads encrypted session from request (via storage)
   * 2. Validates and potentially refreshes the session (via core)
   * 3. Returns auth result + optionally refreshed session data
   *
   * @param request - Framework-specific request object
   * @returns Auth result and optional refreshed session data
   */
  async withAuth(request) {
    try {
      const encryptedSession = await this.storage.getSession(request);
      if (!encryptedSession) {
        return { auth: { user: null } };
      }
      const { claims, session, refreshed } = await this.core.validateAndRefresh(await this.core.decryptSession(encryptedSession));
      const auth = {
        refreshToken: session.refreshToken,
        user: session.user,
        claims,
        impersonator: session.impersonator,
        accessToken: session.accessToken,
        sessionId: claims.sid,
        organizationId: claims.org_id,
        role: claims.role,
        roles: claims.roles,
        permissions: claims.permissions,
        entitlements: claims.entitlements,
        featureFlags: claims.feature_flags
      };
      if (refreshed) {
        const refreshedSessionData = await this.core.encryptSession(session);
        return { auth, refreshedSessionData };
      }
      return { auth };
    } catch {
      return { auth: { user: null } };
    }
  }
  /**
   * Get a session from a request.
   *
   * @param request - Framework-specific request object
   * @returns Decrypted session or null
   */
  async getSession(request) {
    const encryptedSession = await this.storage.getSession(request);
    if (!encryptedSession) {
      return null;
    }
    return this.core.decryptSession(encryptedSession);
  }
  /**
   * Save a session to storage.
   *
   * @param response - Framework-specific response object (may be undefined)
   * @param sessionData - Encrypted session string
   * @returns Updated response and/or headers
   */
  async saveSession(response, sessionData) {
    return this.storage.saveSession(response, sessionData);
  }
  /**
   * Clear a session from storage.
   *
   * @param response - Framework-specific response object
   * @returns Updated response and/or headers
   */
  async clearSession(response) {
    return this.storage.clearSession(response);
  }
  /**
   * Sign out operation.
   *
   * Gets the WorkOS logout URL and clears the session via storage.
   * Returns the URL plus whatever the storage returns (headers and/or response).
   *
   * @param sessionId - The session ID to terminate
   * @param options - Optional return URL
   * @returns Logout URL and storage clear result (headers and/or response)
   */
  async signOut(sessionId, options) {
    const logoutUrl = this.operations.getLogoutUrl(sessionId, options);
    const clearResult = await this.storage.clearSession(void 0);
    return { logoutUrl, ...clearResult };
  }
  /**
   * Switch organization - delegates to AuthOperations.
   */
  async switchOrganization(session, organizationId) {
    return this.operations.switchOrganization(session, organizationId);
  }
  /**
   * Refresh session - delegates to AuthOperations.
   */
  async refreshSession(session, organizationId) {
    return this.operations.refreshSession(session, organizationId);
  }
  /**
   * Get authorization URL - delegates to AuthOperations.
   */
  async getAuthorizationUrl(options = {}) {
    return this.operations.getAuthorizationUrl(options);
  }
  /**
   * Convenience: Get sign-in URL.
   */
  async getSignInUrl(options = {}) {
    return this.operations.getSignInUrl(options);
  }
  /**
   * Convenience: Get sign-up URL.
   */
  async getSignUpUrl(options = {}) {
    return this.operations.getSignUpUrl(options);
  }
  /**
   * Get the WorkOS client instance.
   * Useful for direct API calls not covered by AuthKit.
   */
  getWorkOS() {
    return this.client;
  }
  /**
   * Handle OAuth callback.
   * This creates a new session after successful authentication.
   *
   * @param request - Framework-specific request (not currently used)
   * @param response - Framework-specific response
   * @param options - OAuth callback options (code, state)
   * @returns Updated response, return pathname, and auth response
   */
  async handleCallback(_request, response, options) {
    const authResponse = await this.client.userManagement.authenticateWithCode({
      code: options.code,
      clientId: this.config.clientId
    });
    const session = {
      accessToken: authResponse.accessToken,
      refreshToken: authResponse.refreshToken,
      user: authResponse.user,
      impersonator: authResponse.impersonator
    };
    const encryptedSession = await this.core.encryptSession(session);
    const { response: updatedResponse, headers } = await this.saveSession(response, encryptedSession);
    let returnPathname = "/";
    let customState;
    if (options.state) {
      if (options.state.includes(".")) {
        const [internal, ...rest] = options.state.split(".");
        customState = rest.join(".");
        try {
          const decoded = (internal ?? "").replace(/-/g, "+").replace(/_/g, "/");
          const parsed = JSON.parse(atob(decoded));
          returnPathname = parsed.returnPathname || "/";
        } catch {
        }
      } else {
        try {
          const parsed = JSON.parse(atob(options.state));
          if (parsed.returnPathname) {
            returnPathname = parsed.returnPathname;
          } else {
            customState = options.state;
          }
        } catch {
          customState = options.state;
        }
      }
    }
    return {
      response: updatedResponse,
      headers,
      returnPathname,
      state: customState,
      authResponse
    };
  }
}
const defaultSource = (key) => {
  try {
    const processEnv = globalThis?.process?.env;
    return processEnv?.[key];
  } catch {
    return void 0;
  }
};
class ConfigurationProvider {
  config = {
    cookieName: "wos-session",
    apiHttps: true,
    // Defaults to 400 days, the maximum allowed by Chrome
    // It's fine to have a long cookie expiry date as the access/refresh tokens
    // act as the actual time-limited aspects of the session.
    cookieMaxAge: 60 * 60 * 24 * 400,
    apiHostname: "api.workos.com"
  };
  valueSource = defaultSource;
  requiredKeys = [
    "clientId",
    "apiKey",
    "redirectUri",
    "cookiePassword"
  ];
  /**
   * Convert a camelCase string to an uppercase, underscore-separated environment variable name.
   * @param str The string to convert
   * @returns The environment variable name
   */
  getEnvironmentVariableName(str) {
    return `WORKOS_${str.replace(/([a-z])([A-Z])/g, "$1_$2").toUpperCase()}`;
  }
  updateConfig(config) {
    this.config = { ...this.config, ...config };
  }
  setValueSource(source) {
    this.valueSource = source;
  }
  configure(configOrSource, source) {
    if (typeof configOrSource === "function") {
      this.setValueSource(configOrSource);
    } else if (typeof configOrSource === "object" && !source) {
      this.updateConfig(configOrSource);
    } else if (typeof configOrSource === "object" && source) {
      this.updateConfig(configOrSource);
      this.setValueSource(source);
    }
  }
  getValue(key) {
    const envKey = this.getEnvironmentVariableName(key);
    const envValue = this.getEnvironmentValue(envKey);
    const rawValue = envValue ?? this.config[key];
    if (rawValue != null) {
      return this.convertValueType(key, rawValue);
    }
    if (this.requiredKeys.includes(key)) {
      throw new Error(`Missing required configuration value for ${key} (${envKey}).`);
    }
    return void 0;
  }
  getEnvironmentValue(envKey) {
    const { valueSource } = this;
    if (typeof valueSource === "function") {
      return valueSource(envKey);
    }
    if (valueSource && envKey in valueSource) {
      return valueSource[envKey];
    }
    return void 0;
  }
  convertValueType(key, value) {
    if (typeof value !== "string") {
      return value;
    }
    if (key === "apiHttps") {
      return value === "true";
    }
    if (key === "apiPort" || key === "cookieMaxAge") {
      const num = parseInt(value, 10);
      return isNaN(num) ? void 0 : num;
    }
    return value;
  }
  /**
   * Validates that all required configuration values are present and meet requirements.
   * Collects all validation errors before throwing to provide comprehensive feedback.
   *
   * @throws {Error} If any required configuration is missing or invalid
   *
   * @example
   * ```typescript
   * const provider = new ConfigurationProvider();
   * try {
   *   provider.validate();
   * } catch (error) {
   *   console.error(error.message); // Shows all missing/invalid config at once
   * }
   * ```
   */
  validate() {
    const errors = [];
    for (const key of this.requiredKeys) {
      const envKey = this.getEnvironmentVariableName(key);
      const envValue = this.getEnvironmentValue(envKey);
      const configValue = this.config[key];
      const value = envValue ?? configValue;
      if (!value) {
        errors.push(`${envKey} is required`);
      } else if (key === "cookiePassword") {
        const password = String(value);
        if (password.length < 32) {
          errors.push(`${envKey} must be at least 32 characters (currently ${password.length})`);
        }
      }
    }
    if (errors.length > 0) {
      throw new Error("AuthKit configuration error. Missing or invalid environment variables:\n\n" + errors.map((e) => `  • ${e}`).join("\n") + "\n\nSet these environment variables or call configure() with the required values.\nGet your values from the WorkOS Dashboard: https://dashboard.workos.com");
    }
  }
  getConfig() {
    const fullConfig = {};
    const allKeys = /* @__PURE__ */ new Set([
      ...Object.keys(this.config),
      ...this.requiredKeys
    ]);
    for (const key of allKeys) {
      try {
        const value = this.getValue(key);
        if (value !== void 0) {
          fullConfig[key] = value;
        }
      } catch (error) {
        if (this.requiredKeys.includes(key)) {
          throw error;
        }
      }
    }
    return fullConfig;
  }
}
const getConfigurationInstance = once(() => new ConfigurationProvider());
function getConfig(key) {
  return getConfigurationInstance().getValue(key);
}
function getFullConfig() {
  return getConfigurationInstance().getConfig();
}
function validateConfig() {
  return getConfigurationInstance().validate();
}
const version = "0.3.4";
const pkg = {
  version
};
function createWorkOSInstance() {
  const apiKey = getConfig("apiKey");
  const apiHostname = getConfig("apiHostname");
  const apiHttps = getConfig("apiHttps");
  const apiPort = getConfig("apiPort");
  const options = {
    apiHostname,
    https: apiHttps,
    port: apiPort,
    appInfo: {
      name: "authkit-session",
      version: pkg.version
    }
  };
  const workos = new WorkOSNode(apiKey, options);
  return workos;
}
const getWorkOS = once(createWorkOSInstance);
class SessionEncryption {
  versionDelimiter = "~";
  currentMajorVersion = 2;
  // Parse an iron-session seal to extract the version
  parseSeal(seal2) {
    const [sealWithoutVersion = "", tokenVersionAsString] = seal2.split(this.versionDelimiter);
    const tokenVersion = tokenVersionAsString == null ? null : parseInt(tokenVersionAsString, 10);
    return { sealWithoutVersion, tokenVersion };
  }
  // Encrypt data in a way that's compatible with iron-session
  async sealData(data, { password, ttl = 0 }) {
    const passwordObj = {
      id: "1",
      secret: password
    };
    const seal$1 = await seal(data, passwordObj, {
      encryption: {
        saltBits: 256,
        algorithm: "aes-256-cbc",
        iterations: 1,
        minPasswordlength: 32
      },
      integrity: {
        saltBits: 256,
        algorithm: "sha256",
        iterations: 1,
        minPasswordlength: 32
      },
      ttl: ttl * 1e3,
      // Convert seconds to milliseconds
      localtimeOffsetMsec: 0
    });
    return `${seal$1}${this.versionDelimiter}${this.currentMajorVersion}`;
  }
  // Decrypt data from iron-session with HMAC verification
  async unsealData(encryptedData, { password }) {
    const { sealWithoutVersion, tokenVersion } = this.parseSeal(encryptedData);
    const passwordMap = { 1: password };
    const data = await unseal(sealWithoutVersion, passwordMap, {
      encryption: {
        saltBits: 256,
        algorithm: "aes-256-cbc",
        iterations: 1,
        minPasswordlength: 32
      },
      integrity: {
        saltBits: 256,
        algorithm: "sha256",
        iterations: 1,
        minPasswordlength: 32
      },
      timestampSkewSec: 60,
      localtimeOffsetMsec: 0
    });
    if (tokenVersion === 2) {
      return data;
    } else if (tokenVersion !== null) {
      return { ...data.persistent };
    }
    return data;
  }
}
const ironWebcryptoEncryption = new SessionEncryption();
function createAuthService(options) {
  const { sessionStorageFactory, clientFactory = () => getWorkOS(), encryptionFactory = () => ironWebcryptoEncryption } = options;
  const getService = once(() => {
    const config = getFullConfig();
    const storage = sessionStorageFactory(config);
    const client = clientFactory(config);
    const encryption = encryptionFactory(config);
    return new AuthService(config, storage, client, encryption);
  });
  return {
    withAuth: (request) => getService().withAuth(request),
    getSession: (request) => getService().getSession(request),
    saveSession: (response, sessionData) => getService().saveSession(response, sessionData),
    clearSession: (response) => getService().clearSession(response),
    signOut: (sessionId, opts) => getService().signOut(sessionId, opts),
    switchOrganization: (session, organizationId) => getService().switchOrganization(session, organizationId),
    refreshSession: (session, organizationId) => getService().refreshSession(session, organizationId),
    getAuthorizationUrl: (opts) => getService().getAuthorizationUrl(opts),
    getSignInUrl: (opts) => getService().getSignInUrl(opts),
    getSignUpUrl: (opts) => getService().getSignUpUrl(opts),
    getWorkOS: () => getService().getWorkOS(),
    handleCallback: (request, response, opts) => getService().handleCallback(request, response, opts)
  };
}
class CookieSessionStorage {
  cookieName;
  cookieOptions;
  constructor(config) {
    this.cookieName = config.cookieName ?? "wos_session";
    const sameSite = config.cookieSameSite ?? "lax";
    let secure = true;
    if (sameSite.toLowerCase() !== "none") {
      try {
        const url = new URL(config.redirectUri);
        secure = url.protocol === "https:";
      } catch {
      }
    }
    this.cookieOptions = {
      path: "/",
      httpOnly: true,
      sameSite,
      secure,
      maxAge: config.cookieMaxAge ?? 60 * 60 * 24 * 400,
      // 400 days
      domain: config.cookieDomain
    };
  }
  async applyHeaders(_response, _headers) {
  }
  buildSetCookie(value, expired) {
    const a = [`${this.cookieName}=${encodeURIComponent(value)}`];
    const o = this.cookieOptions;
    if (o.path)
      a.push(`Path=${o.path}`);
    if (o.domain)
      a.push(`Domain=${o.domain}`);
    if (o.maxAge || expired)
      a.push(`Max-Age=${expired ? 0 : o.maxAge}`);
    if (o.httpOnly)
      a.push("HttpOnly");
    if (o.secure)
      a.push("Secure");
    if (o.sameSite) {
      const capitalizedSameSite = o.sameSite.charAt(0).toUpperCase() + o.sameSite.slice(1).toLowerCase();
      a.push(`SameSite=${capitalizedSameSite}`);
    }
    if (o.priority)
      a.push(`Priority=${o.priority}`);
    if (o.partitioned)
      a.push("Partitioned");
    return a.join("; ");
  }
  async saveSession(response, sessionData) {
    const header = this.buildSetCookie(sessionData);
    const mutated = await this.applyHeaders(response, { "Set-Cookie": header });
    return mutated ?? { headers: { "Set-Cookie": header } };
  }
  async clearSession(response) {
    const header = this.buildSetCookie("", true);
    const mutated = await this.applyHeaders(response, { "Set-Cookie": header });
    return mutated ?? { headers: { "Set-Cookie": header } };
  }
}
export {
  CookieSessionStorage as C,
  createAuthService as c,
  validateConfig as v
};
