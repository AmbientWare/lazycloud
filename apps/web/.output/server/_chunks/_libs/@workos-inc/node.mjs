import { u as unseal, d as defaults, s as seal } from "../../../_libs/iron-webcrypto.mjs";
var ApiKeyRequiredException = class extends Error {
  status = 403;
  name = "ApiKeyRequiredException";
  path;
  constructor(path) {
    super(`API key required for "${path}". For server-side apps, initialize with: new WorkOS("sk_..."). For browser/mobile/CLI apps, use authenticateWithCodeAndVerifier() and authenticateWithRefreshToken() which work without an API key.`);
    this.path = path;
  }
};
var GenericServerException = class extends Error {
  name = "GenericServerException";
  message = "The request could not be completed.";
  constructor(status, message, rawData, requestID) {
    super();
    this.status = status;
    this.rawData = rawData;
    this.requestID = requestID;
    if (message) this.message = message;
  }
};
var BadRequestException = class extends Error {
  status = 400;
  name = "BadRequestException";
  message = "Bad request";
  code;
  errors;
  requestID;
  constructor({ code, errors, message, requestID }) {
    super();
    this.requestID = requestID;
    if (message) this.message = message;
    if (code) this.code = code;
    if (errors) this.errors = errors;
  }
};
var NotFoundException = class extends Error {
  status = 404;
  name = "NotFoundException";
  message;
  code;
  requestID;
  constructor({ code, message, path, requestID }) {
    super();
    this.code = code;
    this.message = message ?? `The requested path '${path}' could not be found.`;
    this.requestID = requestID;
  }
};
var OauthException = class extends Error {
  name = "OauthException";
  constructor(status, requestID, error, errorDescription, rawData) {
    super();
    this.status = status;
    this.requestID = requestID;
    this.error = error;
    this.errorDescription = errorDescription;
    this.rawData = rawData;
    if (error && errorDescription) this.message = `Error: ${error}
Error Description: ${errorDescription}`;
    else if (error) this.message = `Error: ${error}`;
    else this.message = `An error has occurred.`;
  }
};
var RateLimitExceededException = class extends GenericServerException {
  name = "RateLimitExceededException";
  constructor(message, requestID, retryAfter) {
    super(429, message, {}, requestID);
    this.retryAfter = retryAfter;
  }
};
var SignatureVerificationException = class extends Error {
  name = "SignatureVerificationException";
  constructor(message) {
    super(message || "Signature verification failed.");
  }
};
var UnauthorizedException = class extends Error {
  status = 401;
  name = "UnauthorizedException";
  message;
  constructor(requestID) {
    super();
    this.requestID = requestID;
    this.message = `Could not authorize the request. Maybe your API key is invalid?`;
  }
};
var UnprocessableEntityException = class extends Error {
  status = 422;
  name = "UnprocessableEntityException";
  message = "Unprocessable entity";
  code;
  requestID;
  constructor({ code, errors, message, requestID }) {
    super();
    this.requestID = requestID;
    if (message) this.message = message;
    if (code) this.code = code;
    if (errors) {
      this.message = `The following ${errors.length === 1 ? "requirement" : "requirements"} must be met:
`;
      for (const { code: code$1 } of errors) this.message = this.message.concat(`	${code$1}
`);
    }
  }
};
var PKCE = class {
  /**
  * Generate a cryptographically random code verifier.
  *
  * @param length - Length of verifier (43-128, default 43)
  * @returns RFC 7636 compliant code verifier
  */
  generateCodeVerifier(length = 43) {
    if (length < 43 || length > 128) throw new RangeError(`Code verifier length must be between 43 and 128, got ${length}`);
    const byteLength = Math.ceil(length * 3 / 4);
    const randomBytes = new Uint8Array(byteLength);
    crypto.getRandomValues(randomBytes);
    return this.base64UrlEncode(randomBytes).slice(0, length);
  }
  /**
  * Generate S256 code challenge from a verifier.
  *
  * @param verifier - The code verifier
  * @returns Base64URL-encoded SHA256 hash
  */
  async generateCodeChallenge(verifier) {
    const data = new TextEncoder().encode(verifier);
    const hash = await crypto.subtle.digest("SHA-256", data);
    return this.base64UrlEncode(new Uint8Array(hash));
  }
  /**
  * Generate a complete PKCE pair (verifier + challenge).
  *
  * @returns Code verifier, challenge, and method ('S256')
  */
  async generate() {
    const codeVerifier = this.generateCodeVerifier();
    return {
      codeVerifier,
      codeChallenge: await this.generateCodeChallenge(codeVerifier),
      codeChallengeMethod: "S256"
    };
  }
  base64UrlEncode(buffer) {
    return btoa(String.fromCharCode(...buffer)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
};
var AutoPaginatable = class {
  object = "list";
  options;
  constructor(list, apiCall, options) {
    this.list = list;
    this.apiCall = apiCall;
    this.options = options ?? {};
  }
  get data() {
    return this.list.data;
  }
  get listMetadata() {
    return this.list.listMetadata;
  }
  async *generatePages(params) {
    const result = await this.apiCall({
      ...this.options,
      limit: 100,
      after: params.after
    });
    yield result.data;
    if (result.listMetadata.after) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      yield* this.generatePages({ after: result.listMetadata.after });
    }
  }
  /**
  * Automatically paginates over the list of results, returning the complete data set.
  * Returns the first result if `options.limit` is passed to the first request.
  */
  async autoPagination() {
    if (this.options.limit) return this.data;
    const results = [];
    for await (const page of this.generatePages({ after: this.options.after })) results.push(...page);
    return results;
  }
};
function deserializeApiKey(apiKey) {
  return {
    object: apiKey.object,
    id: apiKey.id,
    owner: apiKey.owner,
    name: apiKey.name,
    obfuscatedValue: apiKey.obfuscated_value,
    lastUsedAt: apiKey.last_used_at,
    permissions: apiKey.permissions,
    createdAt: apiKey.created_at,
    updatedAt: apiKey.updated_at
  };
}
const deserializeDirectoryGroup = (directoryGroup) => ({
  id: directoryGroup.id,
  idpId: directoryGroup.idp_id,
  directoryId: directoryGroup.directory_id,
  organizationId: directoryGroup.organization_id,
  name: directoryGroup.name,
  createdAt: directoryGroup.created_at,
  updatedAt: directoryGroup.updated_at,
  rawAttributes: directoryGroup.raw_attributes
});
const deserializeUpdatedEventDirectoryGroup = (directoryGroup) => ({
  id: directoryGroup.id,
  idpId: directoryGroup.idp_id,
  directoryId: directoryGroup.directory_id,
  organizationId: directoryGroup.organization_id,
  name: directoryGroup.name,
  createdAt: directoryGroup.created_at,
  updatedAt: directoryGroup.updated_at,
  rawAttributes: directoryGroup.raw_attributes,
  previousAttributes: directoryGroup.previous_attributes
});
const deserializeDirectoryUser = (directoryUser) => ({
  object: directoryUser.object,
  id: directoryUser.id,
  directoryId: directoryUser.directory_id,
  organizationId: directoryUser.organization_id,
  rawAttributes: directoryUser.raw_attributes,
  customAttributes: directoryUser.custom_attributes,
  idpId: directoryUser.idp_id,
  firstName: directoryUser.first_name,
  email: directoryUser.email,
  lastName: directoryUser.last_name,
  state: directoryUser.state,
  role: directoryUser.role,
  roles: directoryUser.roles,
  createdAt: directoryUser.created_at,
  updatedAt: directoryUser.updated_at
});
const deserializeDirectoryUserWithGroups = (directoryUserWithGroups) => ({
  ...deserializeDirectoryUser(directoryUserWithGroups),
  groups: directoryUserWithGroups.groups.map(deserializeDirectoryGroup)
});
const deserializeUpdatedEventDirectoryUser = (directoryUser) => ({
  object: "directory_user",
  id: directoryUser.id,
  directoryId: directoryUser.directory_id,
  organizationId: directoryUser.organization_id,
  rawAttributes: directoryUser.raw_attributes,
  customAttributes: directoryUser.custom_attributes,
  idpId: directoryUser.idp_id,
  firstName: directoryUser.first_name,
  email: directoryUser.email,
  lastName: directoryUser.last_name,
  state: directoryUser.state,
  role: directoryUser.role,
  roles: directoryUser.roles,
  createdAt: directoryUser.created_at,
  updatedAt: directoryUser.updated_at,
  previousAttributes: directoryUser.previous_attributes
});
const deserializeDirectory = (directory) => ({
  object: directory.object,
  id: directory.id,
  domain: directory.domain,
  externalKey: directory.external_key,
  name: directory.name,
  organizationId: directory.organization_id,
  state: deserializeDirectoryState(directory.state),
  type: directory.type,
  createdAt: directory.created_at,
  updatedAt: directory.updated_at
});
const deserializeDirectoryState = (state) => {
  if (state === "linked") return "active";
  if (state === "unlinked") return "inactive";
  return state;
};
const deserializeEventDirectory = (directory) => ({
  object: directory.object,
  id: directory.id,
  externalKey: directory.external_key,
  type: directory.type,
  state: directory.state,
  name: directory.name,
  organizationId: directory.organization_id,
  domains: directory.domains,
  createdAt: directory.created_at,
  updatedAt: directory.updated_at
});
const deserializeDeletedEventDirectory = (directory) => ({
  object: directory.object,
  id: directory.id,
  type: directory.type,
  state: directory.state,
  name: directory.name,
  organizationId: directory.organization_id,
  createdAt: directory.created_at,
  updatedAt: directory.updated_at
});
const deserializeOrganizationDomain = (organizationDomain) => ({
  object: organizationDomain.object,
  id: organizationDomain.id,
  domain: organizationDomain.domain,
  organizationId: organizationDomain.organization_id,
  state: organizationDomain.state,
  verificationToken: organizationDomain.verification_token,
  verificationStrategy: organizationDomain.verification_strategy,
  createdAt: organizationDomain.created_at,
  updatedAt: organizationDomain.updated_at
});
const deserializeOrganization = (organization) => ({
  object: organization.object,
  id: organization.id,
  name: organization.name,
  allowProfilesOutsideOrganization: organization.allow_profiles_outside_organization,
  domains: organization.domains.map(deserializeOrganizationDomain),
  ...typeof organization.stripe_customer_id === "undefined" ? void 0 : { stripeCustomerId: organization.stripe_customer_id },
  createdAt: organization.created_at,
  updatedAt: organization.updated_at,
  externalId: organization.external_id ?? null,
  metadata: organization.metadata ?? {}
});
const deserializeConnection = (connection) => ({
  object: connection.object,
  id: connection.id,
  organizationId: connection.organization_id,
  name: connection.name,
  type: connection.connection_type,
  state: connection.state,
  domains: connection.domains,
  createdAt: connection.created_at,
  updatedAt: connection.updated_at
});
const deserializeAuthenticationEvent = (authenticationEvent) => ({
  email: authenticationEvent.email,
  error: authenticationEvent.error,
  ipAddress: authenticationEvent.ip_address,
  status: authenticationEvent.status,
  type: authenticationEvent.type,
  userAgent: authenticationEvent.user_agent,
  userId: authenticationEvent.user_id
});
const deserializeUser = (user) => ({
  object: user.object,
  id: user.id,
  email: user.email,
  emailVerified: user.email_verified,
  firstName: user.first_name,
  profilePictureUrl: user.profile_picture_url,
  lastName: user.last_name,
  lastSignInAt: user.last_sign_in_at,
  locale: user.locale,
  createdAt: user.created_at,
  updatedAt: user.updated_at,
  externalId: user.external_id ?? null,
  metadata: user.metadata ?? {}
});
const deserializeEmailVerification = (emailVerification) => ({
  object: emailVerification.object,
  id: emailVerification.id,
  userId: emailVerification.user_id,
  email: emailVerification.email,
  expiresAt: emailVerification.expires_at,
  code: emailVerification.code,
  createdAt: emailVerification.created_at,
  updatedAt: emailVerification.updated_at
});
const deserializeEmailVerificationEvent = (emailVerification) => ({
  object: emailVerification.object,
  id: emailVerification.id,
  userId: emailVerification.user_id,
  email: emailVerification.email,
  expiresAt: emailVerification.expires_at,
  createdAt: emailVerification.created_at,
  updatedAt: emailVerification.updated_at
});
const deserializeInvitation = (invitation) => ({
  object: invitation.object,
  id: invitation.id,
  email: invitation.email,
  state: invitation.state,
  acceptedAt: invitation.accepted_at,
  revokedAt: invitation.revoked_at,
  expiresAt: invitation.expires_at,
  organizationId: invitation.organization_id,
  inviterUserId: invitation.inviter_user_id,
  acceptedUserId: invitation.accepted_user_id,
  token: invitation.token,
  acceptInvitationUrl: invitation.accept_invitation_url,
  createdAt: invitation.created_at,
  updatedAt: invitation.updated_at
});
const deserializeInvitationEvent = (invitation) => ({
  object: invitation.object,
  id: invitation.id,
  email: invitation.email,
  state: invitation.state,
  acceptedAt: invitation.accepted_at,
  revokedAt: invitation.revoked_at,
  expiresAt: invitation.expires_at,
  organizationId: invitation.organization_id,
  inviterUserId: invitation.inviter_user_id,
  acceptedUserId: invitation.accepted_user_id,
  createdAt: invitation.created_at,
  updatedAt: invitation.updated_at
});
const deserializeMagicAuth = (magicAuth) => ({
  object: magicAuth.object,
  id: magicAuth.id,
  userId: magicAuth.user_id,
  email: magicAuth.email,
  expiresAt: magicAuth.expires_at,
  code: magicAuth.code,
  createdAt: magicAuth.created_at,
  updatedAt: magicAuth.updated_at
});
const deserializeMagicAuthEvent = (magicAuth) => ({
  object: magicAuth.object,
  id: magicAuth.id,
  userId: magicAuth.user_id,
  email: magicAuth.email,
  expiresAt: magicAuth.expires_at,
  createdAt: magicAuth.created_at,
  updatedAt: magicAuth.updated_at
});
const deserializePasswordReset = (passwordReset) => ({
  object: passwordReset.object,
  id: passwordReset.id,
  userId: passwordReset.user_id,
  email: passwordReset.email,
  passwordResetToken: passwordReset.password_reset_token,
  passwordResetUrl: passwordReset.password_reset_url,
  expiresAt: passwordReset.expires_at,
  createdAt: passwordReset.created_at
});
const deserializePasswordResetEvent = (passwordReset) => ({
  object: passwordReset.object,
  id: passwordReset.id,
  userId: passwordReset.user_id,
  email: passwordReset.email,
  expiresAt: passwordReset.expires_at,
  createdAt: passwordReset.created_at
});
const deserializeSession = (session) => ({
  object: "session",
  id: session.id,
  userId: session.user_id,
  ipAddress: session.ip_address,
  userAgent: session.user_agent,
  organizationId: session.organization_id,
  impersonator: session.impersonator,
  authMethod: session.auth_method,
  status: session.status,
  expiresAt: session.expires_at,
  endedAt: session.ended_at,
  createdAt: session.created_at,
  updatedAt: session.updated_at
});
const deserializeOrganizationMembership = (organizationMembership) => ({
  object: organizationMembership.object,
  id: organizationMembership.id,
  userId: organizationMembership.user_id,
  organizationId: organizationMembership.organization_id,
  organizationName: organizationMembership.organization_name,
  status: organizationMembership.status,
  createdAt: organizationMembership.created_at,
  updatedAt: organizationMembership.updated_at,
  role: organizationMembership.role,
  ...organizationMembership.roles && { roles: organizationMembership.roles }
});
const deserializeRoleEvent = (role) => ({
  object: "role",
  slug: role.slug,
  permissions: role.permissions,
  createdAt: role.created_at,
  updatedAt: role.updated_at
});
const deserializeAuthenticationRadarRiskDetectedEvent = (authenticationRadarRiskDetectedEvent) => ({
  authMethod: authenticationRadarRiskDetectedEvent.auth_method,
  action: authenticationRadarRiskDetectedEvent.action,
  control: authenticationRadarRiskDetectedEvent.control,
  blocklistType: authenticationRadarRiskDetectedEvent.blocklist_type,
  ipAddress: authenticationRadarRiskDetectedEvent.ip_address,
  userAgent: authenticationRadarRiskDetectedEvent.user_agent,
  userId: authenticationRadarRiskDetectedEvent.user_id,
  email: authenticationRadarRiskDetectedEvent.email
});
const deserializeEvent = (event) => {
  const eventBase = {
    id: event.id,
    createdAt: event.created_at,
    context: event.context
  };
  switch (event.event) {
    case "authentication.email_verification_succeeded":
    case "authentication.magic_auth_failed":
    case "authentication.magic_auth_succeeded":
    case "authentication.mfa_succeeded":
    case "authentication.oauth_failed":
    case "authentication.oauth_succeeded":
    case "authentication.passkey_failed":
    case "authentication.passkey_succeeded":
    case "authentication.password_failed":
    case "authentication.password_succeeded":
    case "authentication.sso_failed":
    case "authentication.sso_succeeded":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeAuthenticationEvent(event.data)
      };
    case "authentication.radar_risk_detected":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeAuthenticationRadarRiskDetectedEvent(event.data)
      };
    case "connection.activated":
    case "connection.deactivated":
    case "connection.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeConnection(event.data)
      };
    case "dsync.activated":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeEventDirectory(event.data)
      };
    case "dsync.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeDeletedEventDirectory(event.data)
      };
    case "dsync.group.created":
    case "dsync.group.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeDirectoryGroup(event.data)
      };
    case "dsync.group.updated":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeUpdatedEventDirectoryGroup(event.data)
      };
    case "dsync.group.user_added":
    case "dsync.group.user_removed":
      return {
        ...eventBase,
        event: event.event,
        data: {
          directoryId: event.data.directory_id,
          user: deserializeDirectoryUser(event.data.user),
          group: deserializeDirectoryGroup(event.data.group)
        }
      };
    case "dsync.user.created":
    case "dsync.user.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeDirectoryUser(event.data)
      };
    case "dsync.user.updated":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeUpdatedEventDirectoryUser(event.data)
      };
    case "email_verification.created":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeEmailVerificationEvent(event.data)
      };
    case "invitation.accepted":
    case "invitation.created":
    case "invitation.revoked":
    case "invitation.resent":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeInvitationEvent(event.data)
      };
    case "magic_auth.created":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeMagicAuthEvent(event.data)
      };
    case "password_reset.created":
    case "password_reset.succeeded":
      return {
        ...eventBase,
        event: event.event,
        data: deserializePasswordResetEvent(event.data)
      };
    case "user.created":
    case "user.updated":
    case "user.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeUser(event.data)
      };
    case "organization_membership.created":
    case "organization_membership.deleted":
    case "organization_membership.updated":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeOrganizationMembership(event.data)
      };
    case "role.created":
    case "role.deleted":
    case "role.updated":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeRoleEvent(event.data)
      };
    case "session.created":
    case "session.revoked":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeSession(event.data)
      };
    case "organization.created":
    case "organization.updated":
    case "organization.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeOrganization(event.data)
      };
    case "organization_domain.verified":
    case "organization_domain.verification_failed":
    case "organization_domain.created":
    case "organization_domain.updated":
    case "organization_domain.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeOrganizationDomain(event.data)
      };
    case "api_key.created":
    case "api_key.deleted":
      return {
        ...eventBase,
        event: event.event,
        data: deserializeApiKey(event.data)
      };
  }
};
var SignatureProvider = class {
  cryptoProvider;
  constructor(cryptoProvider) {
    this.cryptoProvider = cryptoProvider;
  }
  async verifyHeader({ payload, sigHeader, secret, tolerance = 18e4 }) {
    const [timestamp, signatureHash] = this.getTimestampAndSignatureHash(sigHeader);
    if (!signatureHash || Object.keys(signatureHash).length === 0) throw new SignatureVerificationException("No signature hash found with expected scheme v1");
    if (parseInt(timestamp, 10) < Date.now() - tolerance) throw new SignatureVerificationException("Timestamp outside the tolerance zone");
    const expectedSig = await this.computeSignature(timestamp, payload, secret);
    if (await this.cryptoProvider.secureCompare(expectedSig, signatureHash) === false) throw new SignatureVerificationException("Signature hash does not match the expected signature hash for payload");
    return true;
  }
  getTimestampAndSignatureHash(sigHeader) {
    const [t, v1] = sigHeader.split(",");
    if (typeof t === "undefined" || typeof v1 === "undefined") throw new SignatureVerificationException("Signature or timestamp missing");
    const { 1: timestamp } = t.split("=");
    const { 1: signatureHash } = v1.split("=");
    return [timestamp, signatureHash];
  }
  async computeSignature(timestamp, payload, secret) {
    payload = JSON.stringify(payload);
    const signedPayload = `${timestamp}.${payload}`;
    return await this.cryptoProvider.computeHMACSignatureAsync(signedPayload, secret);
  }
};
var Webhooks = class {
  signatureProvider;
  constructor(cryptoProvider) {
    this.signatureProvider = new SignatureProvider(cryptoProvider);
  }
  get verifyHeader() {
    return this.signatureProvider.verifyHeader.bind(this.signatureProvider);
  }
  get computeSignature() {
    return this.signatureProvider.computeSignature.bind(this.signatureProvider);
  }
  get getTimestampAndSignatureHash() {
    return this.signatureProvider.getTimestampAndSignatureHash.bind(this.signatureProvider);
  }
  async constructEvent({ payload, sigHeader, secret, tolerance = 18e4 }) {
    const options = {
      payload,
      sigHeader,
      secret,
      tolerance
    };
    await this.verifyHeader(options);
    return deserializeEvent(payload);
  }
};
let AuthenticateWithSessionCookieFailureReason = /* @__PURE__ */ (function(AuthenticateWithSessionCookieFailureReason$1) {
  AuthenticateWithSessionCookieFailureReason$1["INVALID_JWT"] = "invalid_jwt";
  AuthenticateWithSessionCookieFailureReason$1["INVALID_SESSION_COOKIE"] = "invalid_session_cookie";
  AuthenticateWithSessionCookieFailureReason$1["NO_SESSION_COOKIE_PROVIDED"] = "no_session_cookie_provided";
  return AuthenticateWithSessionCookieFailureReason$1;
})({});
const serializeRevokeSessionOptions = (options) => ({ session_id: options.sessionId });
let RefreshSessionFailureReason = /* @__PURE__ */ (function(RefreshSessionFailureReason$1) {
  RefreshSessionFailureReason$1["INVALID_SESSION_COOKIE"] = "invalid_session_cookie";
  RefreshSessionFailureReason$1["NO_SESSION_COOKIE_PROVIDED"] = "no_session_cookie_provided";
  RefreshSessionFailureReason$1["INVALID_GRANT"] = "invalid_grant";
  RefreshSessionFailureReason$1["MFA_ENROLLMENT"] = "mfa_enrollment";
  RefreshSessionFailureReason$1["SSO_REQUIRED"] = "sso_required";
  return RefreshSessionFailureReason$1;
})({});
function isSubject(resource) {
  return Object.prototype.hasOwnProperty.call(resource, "resourceType") && Object.prototype.hasOwnProperty.call(resource, "resourceId");
}
function isResourceInterface(resource) {
  return !!resource && typeof resource === "object" && "getResouceType" in resource && "getResourceId" in resource;
}
const serializeCheckOptions = (options) => ({
  op: options.op,
  checks: options.checks.map(serializeCheckWarrantOptions),
  debug: options.debug
});
const serializeCheckBatchOptions = (options) => ({
  op: "batch",
  checks: options.checks.map(serializeCheckWarrantOptions),
  debug: options.debug
});
const serializeCheckWarrantOptions = (warrant) => {
  return {
    resource_type: isResourceInterface(warrant.resource) ? warrant.resource.getResourceType() : warrant.resource.resourceType,
    resource_id: isResourceInterface(warrant.resource) ? warrant.resource.getResourceId() : warrant.resource.resourceId ? warrant.resource.resourceId : "",
    relation: warrant.relation,
    subject: isSubject(warrant.subject) ? {
      resource_type: warrant.subject.resourceType,
      resource_id: warrant.subject.resourceId
    } : {
      resource_type: warrant.subject.getResourceType(),
      resource_id: warrant.subject.getResourceId()
    },
    context: warrant.context ?? {}
  };
};
const deserializeDecisionTreeNode = (response) => {
  return {
    check: {
      resource: {
        resourceType: response.check.resource_type,
        resourceId: response.check.resource_id
      },
      relation: response.check.relation,
      subject: {
        resourceType: response.check.subject.resource_type,
        resourceId: response.check.subject.resource_id
      },
      context: response.check.context
    },
    policy: response.policy,
    decision: response.decision,
    processingTime: response.processing_time,
    children: response.children.map(deserializeDecisionTreeNode)
  };
};
const CHECK_RESULT_AUTHORIZED = "authorized";
var CheckResult = class {
  result;
  isImplicit;
  warrantToken;
  debugInfo;
  warnings;
  constructor(json) {
    this.result = json.result;
    this.isImplicit = json.is_implicit;
    this.warrantToken = json.warrant_token;
    this.debugInfo = json.debug_info ? {
      processingTime: json.debug_info.processing_time,
      decisionTree: deserializeDecisionTreeNode(json.debug_info.decision_tree)
    } : void 0;
    this.warnings = json.warnings;
  }
  isAuthorized() {
    return this.result === CHECK_RESULT_AUTHORIZED;
  }
};
let ResourceOp = /* @__PURE__ */ (function(ResourceOp$1) {
  ResourceOp$1["Create"] = "create";
  ResourceOp$1["Delete"] = "delete";
  return ResourceOp$1;
})({});
var CryptoProvider = class {
  encoder = new TextEncoder();
};
var SubtleCryptoProvider = class extends CryptoProvider {
  subtleCrypto;
  constructor(subtleCrypto) {
    super();
    this.subtleCrypto = subtleCrypto || crypto.subtle;
  }
  computeHMACSignature(_payload, _secret) {
    throw new Error("SubleCryptoProvider cannot be used in a synchronous context.");
  }
  /** @override */
  async computeHMACSignatureAsync(payload, secret) {
    const encoder = new TextEncoder();
    const key = await this.subtleCrypto.importKey("raw", encoder.encode(secret), {
      name: "HMAC",
      hash: { name: "SHA-256" }
    }, false, ["sign"]);
    const signatureBuffer = await this.subtleCrypto.sign("hmac", key, encoder.encode(payload));
    const signatureBytes = new Uint8Array(signatureBuffer);
    const signatureHexCodes = new Array(signatureBytes.length);
    for (let i = 0; i < signatureBytes.length; i++) signatureHexCodes[i] = byteHexMapping[signatureBytes[i]];
    return signatureHexCodes.join("");
  }
  /** @override */
  async secureCompare(stringA, stringB) {
    const bufferA = this.encoder.encode(stringA);
    const bufferB = this.encoder.encode(stringB);
    if (bufferA.length !== bufferB.length) return false;
    const algorithm = {
      name: "HMAC",
      hash: "SHA-256"
    };
    const key = await crypto.subtle.generateKey(algorithm, false, ["sign", "verify"]);
    const hmac = await crypto.subtle.sign(algorithm, key, bufferA);
    return await crypto.subtle.verify(algorithm, key, hmac, bufferB);
  }
  async encrypt(plaintext, key, iv, aad) {
    const actualIv = iv || crypto.getRandomValues(new Uint8Array(32));
    const cryptoKey = await this.subtleCrypto.importKey("raw", key, { name: "AES-GCM" }, false, ["encrypt"]);
    const encryptParams = {
      name: "AES-GCM",
      iv: actualIv
    };
    if (aad) encryptParams.additionalData = aad;
    const encryptedData = await this.subtleCrypto.encrypt(encryptParams, cryptoKey, plaintext);
    const encryptedBytes = new Uint8Array(encryptedData);
    const tagStart = encryptedBytes.length - 16;
    const tag = encryptedBytes.slice(tagStart);
    return {
      ciphertext: encryptedBytes.slice(0, tagStart),
      iv: actualIv,
      tag
    };
  }
  async decrypt(ciphertext, key, iv, tag, aad) {
    const combinedData = new Uint8Array(ciphertext.length + tag.length);
    combinedData.set(ciphertext, 0);
    combinedData.set(tag, ciphertext.length);
    const cryptoKey = await this.subtleCrypto.importKey("raw", key, { name: "AES-GCM" }, false, ["decrypt"]);
    const decryptParams = {
      name: "AES-GCM",
      iv
    };
    if (aad) decryptParams.additionalData = aad;
    const decryptedData = await this.subtleCrypto.decrypt(decryptParams, cryptoKey, combinedData);
    return new Uint8Array(decryptedData);
  }
  randomBytes(length) {
    const bytes = new Uint8Array(length);
    crypto.getRandomValues(bytes);
    return bytes;
  }
  randomUUID() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = this.randomBytes(16);
    bytes[6] = bytes[6] & 15 | 64;
    bytes[8] = bytes[8] & 63 | 128;
    const hex = Array.from(bytes, (b) => byteHexMapping[b]).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
};
const byteHexMapping = new Array(256);
for (let i = 0; i < byteHexMapping.length; i++) byteHexMapping[i] = i.toString(16).padStart(2, "0");
var HttpClient = class HttpClient2 {
  MAX_RETRY_ATTEMPTS = 3;
  BACKOFF_MULTIPLIER = 1.5;
  MINIMUM_SLEEP_TIME_IN_MILLISECONDS = 500;
  RETRY_STATUS_CODES = [
    408,
    500,
    502,
    504
  ];
  constructor(baseURL, options) {
    this.baseURL = baseURL;
    this.options = options;
  }
  /** The HTTP client name used for diagnostics */
  getClientName() {
    throw new Error("getClientName not implemented");
  }
  addClientToUserAgent(userAgent) {
    if (userAgent.indexOf(" ") > -1) return userAgent.replace(/\b\s/, `/${this.getClientName()} `);
    else return userAgent += `/${this.getClientName()}`;
  }
  static getResourceURL(baseURL, path, params) {
    const queryString = HttpClient2.getQueryString(params);
    return new URL([path, queryString].filter(Boolean).join("?"), baseURL).toString();
  }
  static getQueryString(queryObj) {
    if (!queryObj) return void 0;
    const sanitizedQueryObj = {};
    Object.entries(queryObj).forEach(([param, value]) => {
      if (value !== "" && value !== void 0) sanitizedQueryObj[param] = value;
    });
    return new URLSearchParams(sanitizedQueryObj).toString();
  }
  static getContentTypeHeader(entity) {
    if (entity instanceof URLSearchParams) return { "Content-Type": "application/x-www-form-urlencoded;charset=utf-8" };
  }
  static getBody(entity) {
    if (entity === null || entity instanceof URLSearchParams) return entity;
    return JSON.stringify(entity);
  }
  static isPathRetryable(path) {
    return path.startsWith("/fga/") || path.startsWith("/vault/") || path.startsWith("/audit_logs/events");
  }
  getSleepTimeInMilliseconds(retryAttempt) {
    return this.MINIMUM_SLEEP_TIME_IN_MILLISECONDS * Math.pow(this.BACKOFF_MULTIPLIER, retryAttempt) * (Math.random() + 0.5);
  }
  sleep = (retryAttempt) => new Promise((resolve) => setTimeout(resolve, this.getSleepTimeInMilliseconds(retryAttempt)));
};
var HttpClientResponse = class {
  _statusCode;
  _headers;
  constructor(statusCode, headers) {
    this._statusCode = statusCode;
    this._headers = headers;
  }
  getStatusCode() {
    return this._statusCode;
  }
  getHeaders() {
    return this._headers;
  }
};
var HttpClientError = class extends Error {
  name = "HttpClientError";
  message = "The request could not be completed.";
  response;
  constructor({ message, response }) {
    super(message);
    this.message = message;
    this.response = response;
  }
};
var ParseError = class extends Error {
  name = "ParseError";
  status = 500;
  rawBody;
  rawStatus;
  requestID;
  constructor({ message, rawBody, rawStatus, requestID }) {
    super(message);
    this.rawBody = rawBody;
    this.rawStatus = rawStatus;
    this.requestID = requestID;
  }
};
const DEFAULT_FETCH_TIMEOUT = 6e4;
var FetchHttpClient = class extends HttpClient {
  _fetchFn;
  constructor(baseURL, options, fetchFn) {
    super(baseURL, options);
    this.baseURL = baseURL;
    this.options = options;
    if (!fetchFn) {
      if (!globalThis.fetch) throw new Error("Fetch function not defined in the global scope and no replacement was provided.");
      fetchFn = globalThis.fetch;
    }
    this._fetchFn = fetchFn.bind(globalThis);
  }
  /** @override */
  getClientName() {
    return "fetch";
  }
  async get(path, options) {
    const resourceURL = HttpClient.getResourceURL(this.baseURL, path, options.params);
    if (HttpClient.isPathRetryable(path)) return await this.fetchRequestWithRetry(resourceURL, "GET", null, options.headers);
    else return await this.fetchRequest(resourceURL, "GET", null, options.headers);
  }
  async post(path, entity, options) {
    const resourceURL = HttpClient.getResourceURL(this.baseURL, path, options.params);
    if (HttpClient.isPathRetryable(path)) return await this.fetchRequestWithRetry(resourceURL, "POST", HttpClient.getBody(entity), {
      ...HttpClient.getContentTypeHeader(entity),
      ...options.headers
    });
    else return await this.fetchRequest(resourceURL, "POST", HttpClient.getBody(entity), {
      ...HttpClient.getContentTypeHeader(entity),
      ...options.headers
    });
  }
  async put(path, entity, options) {
    const resourceURL = HttpClient.getResourceURL(this.baseURL, path, options.params);
    if (HttpClient.isPathRetryable(path)) return await this.fetchRequestWithRetry(resourceURL, "PUT", HttpClient.getBody(entity), {
      ...HttpClient.getContentTypeHeader(entity),
      ...options.headers
    });
    else return await this.fetchRequest(resourceURL, "PUT", HttpClient.getBody(entity), {
      ...HttpClient.getContentTypeHeader(entity),
      ...options.headers
    });
  }
  async delete(path, options) {
    const resourceURL = HttpClient.getResourceURL(this.baseURL, path, options.params);
    if (HttpClient.isPathRetryable(path)) return await this.fetchRequestWithRetry(resourceURL, "DELETE", null, options.headers);
    else return await this.fetchRequest(resourceURL, "DELETE", null, options.headers);
  }
  async fetchRequest(url, method, body, headers) {
    const requestBody = body || (method === "POST" || method === "PUT" || method === "PATCH" ? "" : void 0);
    const { "User-Agent": userAgent } = this.options?.headers || {};
    const timeout = this.options?.timeout ?? DEFAULT_FETCH_TIMEOUT;
    const abortController = new AbortController();
    const timeoutId = setTimeout(() => {
      abortController?.abort();
    }, timeout);
    try {
      const res = await this._fetchFn(url, {
        method,
        headers: {
          Accept: "application/json, text/plain, */*",
          "Content-Type": "application/json",
          ...this.options?.headers,
          ...headers,
          "User-Agent": this.addClientToUserAgent((userAgent || "workos-node").toString())
        },
        body: requestBody,
        signal: abortController?.signal
      });
      if (timeoutId) clearTimeout(timeoutId);
      if (!res.ok) {
        const requestID = res.headers.get("X-Request-ID") ?? "";
        const rawBody = await res.text();
        let responseJson;
        try {
          responseJson = JSON.parse(rawBody);
        } catch (error) {
          if (error instanceof SyntaxError) throw new ParseError({
            message: error.message,
            rawBody,
            requestID,
            rawStatus: res.status
          });
          throw error;
        }
        throw new HttpClientError({
          message: res.statusText,
          response: {
            status: res.status,
            headers: res.headers,
            data: responseJson
          }
        });
      }
      return new FetchHttpClientResponse(res);
    } catch (error) {
      if (timeoutId) clearTimeout(timeoutId);
      if (error instanceof Error && error.name === "AbortError") throw new HttpClientError({
        message: `Request timeout after ${timeout}ms`,
        response: {
          status: 408,
          headers: {},
          data: { error: "Request timeout" }
        }
      });
      throw error;
    }
  }
  async fetchRequestWithRetry(url, method, body, headers) {
    let response;
    let retryAttempts = 1;
    const makeRequest = async () => {
      let requestError = null;
      try {
        response = await this.fetchRequest(url, method, body, headers);
      } catch (e) {
        requestError = e;
      }
      if (this.shouldRetryRequest(requestError, retryAttempts)) {
        retryAttempts++;
        await this.sleep(retryAttempts);
        return makeRequest();
      }
      if (requestError != null) throw requestError;
      return response;
    };
    return makeRequest();
  }
  shouldRetryRequest(requestError, retryAttempt) {
    if (retryAttempt > this.MAX_RETRY_ATTEMPTS) return false;
    if (requestError != null) {
      if (requestError instanceof TypeError) return true;
      if (requestError instanceof HttpClientError && this.RETRY_STATUS_CODES.includes(requestError.response.status)) return true;
    }
    return false;
  }
};
var FetchHttpClientResponse = class FetchHttpClientResponse2 extends HttpClientResponse {
  _res;
  constructor(res) {
    super(res.status, FetchHttpClientResponse2._transformHeadersToObject(res.headers));
    this._res = res;
  }
  getRawResponse() {
    return this._res;
  }
  toJSON() {
    return this._res.headers.get("content-type")?.includes("application/json") ? this._res.json() : null;
  }
  static _transformHeadersToObject(headers) {
    const headersObj = {};
    for (const entry of Object.entries(headers)) {
      if (!Array.isArray(entry) || entry.length !== 2) throw new Error("Response objects produced by the fetch function given to FetchHttpClient do not have an iterable headers map. Response#headers should be an iterable object.");
      headersObj[entry[0]] = entry[1];
    }
    return headersObj;
  }
};
const unreachable = (condition, message = `Entered unreachable code. Received '${condition}'.`) => {
  throw new TypeError(message);
};
const deserializeUserData = (userData) => {
  return {
    object: userData.object,
    email: userData.email,
    firstName: userData.first_name,
    lastName: userData.last_name
  };
};
const deserializeAction = (actionPayload) => {
  switch (actionPayload.object) {
    case "user_registration_action_context":
      return {
        id: actionPayload.id,
        object: actionPayload.object,
        userData: deserializeUserData(actionPayload.user_data),
        invitation: actionPayload.invitation ? deserializeInvitation(actionPayload.invitation) : void 0,
        ipAddress: actionPayload.ip_address,
        userAgent: actionPayload.user_agent,
        deviceFingerprint: actionPayload.device_fingerprint
      };
    case "authentication_action_context":
      return {
        id: actionPayload.id,
        object: actionPayload.object,
        user: deserializeUser(actionPayload.user),
        organization: actionPayload.organization ? deserializeOrganization(actionPayload.organization) : void 0,
        organizationMembership: actionPayload.organization_membership ? deserializeOrganizationMembership(actionPayload.organization_membership) : void 0,
        ipAddress: actionPayload.ip_address,
        userAgent: actionPayload.user_agent,
        deviceFingerprint: actionPayload.device_fingerprint,
        issuer: actionPayload.issuer
      };
  }
};
var Actions = class {
  signatureProvider;
  constructor(cryptoProvider) {
    this.signatureProvider = new SignatureProvider(cryptoProvider);
  }
  get computeSignature() {
    return this.signatureProvider.computeSignature.bind(this.signatureProvider);
  }
  get verifyHeader() {
    return this.signatureProvider.verifyHeader.bind(this.signatureProvider);
  }
  serializeType(type) {
    switch (type) {
      case "authentication":
        return "authentication_action_response";
      case "user_registration":
        return "user_registration_action_response";
      default:
        return unreachable(type);
    }
  }
  async signResponse(data, secret) {
    let errorMessage;
    const { verdict, type } = data;
    if (verdict === "Deny" && data.errorMessage) errorMessage = data.errorMessage;
    const responsePayload = {
      timestamp: Date.now(),
      verdict,
      ...verdict === "Deny" && data.errorMessage && { error_message: errorMessage }
    };
    return {
      object: this.serializeType(type),
      payload: responsePayload,
      signature: await this.computeSignature(responsePayload.timestamp, responsePayload, secret)
    };
  }
  async constructAction({ payload, sigHeader, secret, tolerance = 3e4 }) {
    const options = {
      payload,
      sigHeader,
      secret,
      tolerance
    };
    await this.verifyHeader(options);
    return deserializeAction(payload);
  }
};
function deserializeValidateApiKeyResponse(response) {
  return { apiKey: response.api_key ? deserializeApiKey(response.api_key) : null };
}
var ApiKeys = class {
  constructor(workos) {
    this.workos = workos;
  }
  async validateApiKey(payload) {
    const { data } = await this.workos.post("/api_keys/validations", payload);
    return deserializeValidateApiKeyResponse(data);
  }
  async deleteApiKey(id) {
    await this.workos.delete(`/api_keys/${id}`);
  }
};
const serializeListDirectoriesOptions = (options) => ({
  organization_id: options.organizationId,
  search: options.search,
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const deserializeList = (list, deserializer) => ({
  object: "list",
  data: list.data.map(deserializer),
  listMetadata: list.list_metadata
});
const setDefaultOptions = (options) => {
  return {
    ...options,
    order: options?.order || "desc"
  };
};
const fetchAndDeserialize = async (workos, endpoint, deserializeFn, options, requestOptions) => {
  const { data } = await workos.get(endpoint, {
    query: setDefaultOptions(options),
    ...requestOptions
  });
  return deserializeList(data, deserializeFn);
};
var DirectorySync = class {
  constructor(workos) {
    this.workos = workos;
  }
  async listDirectories(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/directories", deserializeDirectory, options ? serializeListDirectoriesOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, "/directories", deserializeDirectory, params), options ? serializeListDirectoriesOptions(options) : void 0);
  }
  async getDirectory(id) {
    const { data } = await this.workos.get(`/directories/${id}`);
    return deserializeDirectory(data);
  }
  async deleteDirectory(id) {
    await this.workos.delete(`/directories/${id}`);
  }
  async listGroups(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/directory_groups", deserializeDirectoryGroup, options), (params) => fetchAndDeserialize(this.workos, "/directory_groups", deserializeDirectoryGroup, params), options);
  }
  async listUsers(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/directory_users", deserializeDirectoryUserWithGroups, options), (params) => fetchAndDeserialize(this.workos, "/directory_users", deserializeDirectoryUserWithGroups, params), options);
  }
  async getUser(user) {
    const { data } = await this.workos.get(`/directory_users/${user}`);
    return deserializeDirectoryUserWithGroups(data);
  }
  async getGroup(group) {
    const { data } = await this.workos.get(`/directory_groups/${group}`);
    return deserializeDirectoryGroup(data);
  }
};
const serializeListEventOptions = (options) => ({
  events: options.events,
  organization_id: options.organizationId,
  range_start: options.rangeStart,
  range_end: options.rangeEnd,
  limit: options.limit,
  after: options.after
});
var Events = class {
  constructor(workos) {
    this.workos = workos;
  }
  async listEvents(options) {
    const { data } = await this.workos.get(`/events`, { query: options ? serializeListEventOptions(options) : void 0 });
    return deserializeList(data, deserializeEvent);
  }
};
const serializeCreateOrganizationOptions = (options) => ({
  name: options.name,
  domain_data: options.domainData,
  external_id: options.externalId,
  metadata: options.metadata
});
const serializeUpdateOrganizationOptions = (options) => ({
  name: options.name,
  domain_data: options.domainData,
  stripe_customer_id: options.stripeCustomerId,
  external_id: options.externalId,
  metadata: options.metadata
});
const deserializeRole = (role) => ({
  object: role.object,
  id: role.id,
  name: role.name,
  slug: role.slug,
  description: role.description,
  permissions: role.permissions,
  type: role.type,
  createdAt: role.created_at,
  updatedAt: role.updated_at
});
const deserializeFeatureFlag = (featureFlag) => ({
  object: featureFlag.object,
  id: featureFlag.id,
  name: featureFlag.name,
  slug: featureFlag.slug,
  description: featureFlag.description,
  tags: featureFlag.tags,
  enabled: featureFlag.enabled,
  defaultValue: featureFlag.default_value,
  createdAt: featureFlag.created_at,
  updatedAt: featureFlag.updated_at
});
function serializeCreateOrganizationApiKeyOptions(options) {
  return {
    name: options.name,
    permissions: options.permissions
  };
}
function deserializeCreatedApiKey(apiKey) {
  return {
    object: apiKey.object,
    id: apiKey.id,
    owner: apiKey.owner,
    name: apiKey.name,
    obfuscatedValue: apiKey.obfuscated_value,
    value: apiKey.value,
    lastUsedAt: apiKey.last_used_at,
    permissions: apiKey.permissions,
    createdAt: apiKey.created_at,
    updatedAt: apiKey.updated_at
  };
}
var Organizations = class {
  constructor(workos) {
    this.workos = workos;
  }
  async listOrganizations(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/organizations", deserializeOrganization, options), (params) => fetchAndDeserialize(this.workos, "/organizations", deserializeOrganization, params), options);
  }
  async createOrganization(payload, requestOptions = {}) {
    const { data } = await this.workos.post("/organizations", serializeCreateOrganizationOptions(payload), requestOptions);
    return deserializeOrganization(data);
  }
  async deleteOrganization(id) {
    await this.workos.delete(`/organizations/${id}`);
  }
  async getOrganization(id) {
    const { data } = await this.workos.get(`/organizations/${id}`);
    return deserializeOrganization(data);
  }
  async getOrganizationByExternalId(externalId) {
    const { data } = await this.workos.get(`/organizations/external_id/${externalId}`);
    return deserializeOrganization(data);
  }
  async updateOrganization(options) {
    const { organization: organizationId, ...payload } = options;
    const { data } = await this.workos.put(`/organizations/${organizationId}`, serializeUpdateOrganizationOptions(payload));
    return deserializeOrganization(data);
  }
  async listOrganizationRoles(options) {
    const { organizationId } = options;
    const { data: response } = await this.workos.get(`/organizations/${organizationId}/roles`);
    return {
      object: "list",
      data: response.data.map((role) => deserializeRole(role))
    };
  }
  async listOrganizationFeatureFlags(options) {
    const { organizationId, ...paginationOptions } = options;
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, `/organizations/${organizationId}/feature-flags`, deserializeFeatureFlag, paginationOptions), (params) => fetchAndDeserialize(this.workos, `/organizations/${organizationId}/feature-flags`, deserializeFeatureFlag, params), options);
  }
  async listOrganizationApiKeys(options) {
    const { organizationId, ...paginationOptions } = options;
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, `/organizations/${organizationId}/api_keys`, deserializeApiKey, paginationOptions), (params) => fetchAndDeserialize(this.workos, `/organizations/${organizationId}/api_keys`, deserializeApiKey, params), paginationOptions);
  }
  async createOrganizationApiKey(options, requestOptions = {}) {
    const { organizationId } = options;
    const { data } = await this.workos.post(`/organizations/${organizationId}/api_keys`, serializeCreateOrganizationApiKeyOptions(options), requestOptions);
    return deserializeCreatedApiKey(data);
  }
};
const serializeCreateOrganizationDomainOptions = (options) => ({
  domain: options.domain,
  organization_id: options.organizationId
});
var OrganizationDomains = class {
  constructor(workos) {
    this.workos = workos;
  }
  async get(id) {
    const { data } = await this.workos.get(`/organization_domains/${id}`);
    return deserializeOrganizationDomain(data);
  }
  async verify(id) {
    const { data } = await this.workos.post(`/organization_domains/${id}/verify`, {});
    return deserializeOrganizationDomain(data);
  }
  async create(payload) {
    const { data } = await this.workos.post(`/organization_domains`, serializeCreateOrganizationDomainOptions(payload));
    return deserializeOrganizationDomain(data);
  }
  async delete(id) {
    await this.workos.delete(`/organization_domains/${id}`);
  }
};
const deserializePasswordlessSession = (passwordlessSession) => ({
  id: passwordlessSession.id,
  email: passwordlessSession.email,
  expiresAt: passwordlessSession.expires_at,
  link: passwordlessSession.link,
  object: passwordlessSession.object
});
var Passwordless = class {
  constructor(workos) {
    this.workos = workos;
  }
  async createSession({ redirectURI, expiresIn, ...options }) {
    const { data } = await this.workos.post("/passwordless/sessions", {
      ...options,
      redirect_uri: redirectURI,
      expires_in: expiresIn
    });
    return deserializePasswordlessSession(data);
  }
  async sendSession(sessionId) {
    const { data } = await this.workos.post(`/passwordless/sessions/${sessionId}/send`, {});
    return data;
  }
};
function deserializeAccessToken(serialized) {
  return {
    object: "access_token",
    accessToken: serialized.access_token,
    expiresAt: serialized.expires_at ? new Date(Date.parse(serialized.expires_at)) : null,
    scopes: serialized.scopes,
    missingScopes: serialized.missing_scopes
  };
}
function serializeGetAccessTokenOptions(options) {
  return {
    user_id: options.userId,
    organization_id: options.organizationId
  };
}
function deserializeGetAccessTokenResponse(response) {
  if (response.active) return {
    active: true,
    accessToken: deserializeAccessToken(response.access_token)
  };
  return {
    active: false,
    error: response.error
  };
}
var Pipes = class {
  constructor(workos) {
    this.workos = workos;
  }
  async getAccessToken({ provider, ...options }) {
    const { data } = await this.workos.post(`data-integrations/${provider}/token`, serializeGetAccessTokenOptions(options));
    return deserializeGetAccessTokenResponse(data);
  }
};
var Portal = class {
  constructor(workos) {
    this.workos = workos;
  }
  async generateLink({ intent, organization, returnUrl, successUrl }) {
    const { data } = await this.workos.post("/portal/generate_link", {
      intent,
      organization,
      return_url: returnUrl,
      success_url: successUrl
    });
    return data;
  }
};
const serializeListConnectionsOptions = (options) => ({
  connection_type: options.connectionType,
  domain: options.domain,
  organization_id: options.organizationId,
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const deserializeProfile = (profile) => ({
  id: profile.id,
  idpId: profile.idp_id,
  organizationId: profile.organization_id,
  connectionId: profile.connection_id,
  connectionType: profile.connection_type,
  email: profile.email,
  firstName: profile.first_name,
  lastName: profile.last_name,
  role: profile.role,
  roles: profile.roles,
  groups: profile.groups,
  customAttributes: profile.custom_attributes,
  rawAttributes: profile.raw_attributes
});
const deserializeOauthTokens = (oauthTokens) => oauthTokens ? {
  accessToken: oauthTokens.access_token,
  refreshToken: oauthTokens.refresh_token,
  expiresAt: oauthTokens.expires_at,
  scopes: oauthTokens.scopes
} : void 0;
const deserializeProfileAndToken = (profileAndToken) => ({
  accessToken: profileAndToken.access_token,
  profile: deserializeProfile(profileAndToken.profile),
  oauthTokens: deserializeOauthTokens(profileAndToken.oauth_tokens)
});
function toQueryString(options) {
  const params = [];
  const sortedKeys = Object.keys(options).sort((a, b) => a.localeCompare(b));
  for (const key of sortedKeys) {
    const value = options[key];
    if (value === void 0) continue;
    if (Array.isArray(value)) for (const item of value) params.push([key, String(item)]);
    else if (typeof value === "object" && value !== null) {
      const sortedSubKeys = Object.keys(value).sort((a, b) => a.localeCompare(b));
      for (const subKey of sortedSubKeys) {
        const subValue = value[subKey];
        if (subValue !== void 0) params.push([`${key}[${subKey}]`, String(subValue)]);
      }
    } else params.push([key, String(value)]);
  }
  return params.map(([key, value]) => {
    return `${encodeRFC1738(key)}=${encodeRFC1738(value)}`;
  }).join("&");
}
function encodeRFC1738(str) {
  return encodeURIComponent(str).replace(/%20/g, "+").replace(/[!'*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}
var SSO = class {
  constructor(workos) {
    this.workos = workos;
  }
  async listConnections(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/connections", deserializeConnection, options ? serializeListConnectionsOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, "/connections", deserializeConnection, params), options ? serializeListConnectionsOptions(options) : void 0);
  }
  async deleteConnection(id) {
    await this.workos.delete(`/connections/${id}`);
  }
  getAuthorizationUrl(options) {
    const { codeChallenge, codeChallengeMethod, connection, clientId, domainHint, loginHint, organization, provider, providerQueryParams, providerScopes, redirectUri, state } = options;
    if (!provider && !connection && !organization) throw new TypeError(`Incomplete arguments. Need to specify either a 'connection', 'organization', or 'provider'.`);
    const query = toQueryString({
      code_challenge: codeChallenge,
      code_challenge_method: codeChallengeMethod,
      connection,
      organization,
      domain_hint: domainHint,
      login_hint: loginHint,
      provider,
      provider_query_params: providerQueryParams,
      provider_scopes: providerScopes,
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      state
    });
    return `${this.workos.baseURL}/sso/authorize?${query}`;
  }
  /**
  * Generates an authorization URL with PKCE parameters automatically generated.
  * Use this for public clients (CLI apps, Electron, mobile) that cannot
  * securely store a client secret.
  *
  * @returns Object containing url, state, and codeVerifier
  *
  * @example
  * ```typescript
  * const { url, state, codeVerifier } = await workos.sso.getAuthorizationUrlWithPKCE({
  *   connection: 'conn_123',
  *   clientId: 'client_123',
  *   redirectUri: 'myapp://callback',
  * });
  *
  * // Store state and codeVerifier securely, then redirect user to url
  * // After callback, exchange the code:
  * const { profile, accessToken } = await workos.sso.getProfileAndToken({
  *   code: authorizationCode,
  *   codeVerifier,
  *   clientId: 'client_123',
  * });
  * ```
  */
  async getAuthorizationUrlWithPKCE(options) {
    const { connection, clientId, domainHint, loginHint, organization, provider, providerQueryParams, providerScopes, redirectUri } = options;
    if (!provider && !connection && !organization) throw new TypeError(`Incomplete arguments. Need to specify either a 'connection', 'organization', or 'provider'.`);
    const pkce = await this.workos.pkce.generate();
    const state = this.workos.pkce.generateCodeVerifier(43);
    const query = toQueryString({
      code_challenge: pkce.codeChallenge,
      code_challenge_method: "S256",
      connection,
      organization,
      domain_hint: domainHint,
      login_hint: loginHint,
      provider,
      provider_query_params: providerQueryParams,
      provider_scopes: providerScopes,
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      state
    });
    return {
      url: `${this.workos.baseURL}/sso/authorize?${query}`,
      state,
      codeVerifier: pkce.codeVerifier
    };
  }
  async getConnection(id) {
    const { data } = await this.workos.get(`/connections/${id}`);
    return deserializeConnection(data);
  }
  /**
  * Exchange an authorization code for a profile and access token.
  *
  * Auto-detects public vs confidential client mode:
  * - If codeVerifier is provided: Uses PKCE flow (public client)
  * - If no codeVerifier: Uses client_secret from API key (confidential client)
  * - If both: Uses both client_secret AND codeVerifier (confidential client with PKCE)
  *
  * Using PKCE with confidential clients is recommended by OAuth 2.1 for defense
  * in depth and provides additional CSRF protection on the authorization flow.
  *
  * @throws Error if neither codeVerifier nor API key is available
  */
  async getProfileAndToken({ code, clientId, codeVerifier }) {
    if (codeVerifier !== void 0 && codeVerifier.trim() === "") throw new TypeError("codeVerifier cannot be an empty string. Generate a valid PKCE pair using workos.pkce.generate().");
    const hasApiKey = !!this.workos.key;
    const hasPKCE = !!codeVerifier;
    if (!hasPKCE && !hasApiKey) throw new TypeError("getProfileAndToken requires either a codeVerifier (for public clients) or an API key configured on the WorkOS instance (for confidential clients).");
    const form = new URLSearchParams({
      client_id: clientId,
      grant_type: "authorization_code",
      code
    });
    if (hasPKCE) form.set("code_verifier", codeVerifier);
    if (hasApiKey) form.set("client_secret", this.workos.key);
    const { data } = await this.workos.post("/sso/token", form, { skipApiKeyCheck: !hasApiKey });
    return deserializeProfileAndToken(data);
  }
  async getProfile({ accessToken }) {
    const { data } = await this.workos.get("/sso/profile", { accessToken });
    return deserializeProfile(data);
  }
};
const deserializeChallenge = (challenge) => ({
  object: challenge.object,
  id: challenge.id,
  createdAt: challenge.created_at,
  updatedAt: challenge.updated_at,
  expiresAt: challenge.expires_at,
  code: challenge.code,
  authenticationFactorId: challenge.authentication_factor_id
});
const deserializeTotp = (totp) => {
  return {
    issuer: totp.issuer,
    user: totp.user
  };
};
const deserializeTotpWithSecrets = (totp) => {
  return {
    issuer: totp.issuer,
    user: totp.user,
    qrCode: totp.qr_code,
    secret: totp.secret,
    uri: totp.uri
  };
};
const deserializeSms = (sms) => ({ phoneNumber: sms.phone_number });
const deserializeFactor$1 = (factor) => ({
  object: factor.object,
  id: factor.id,
  createdAt: factor.created_at,
  updatedAt: factor.updated_at,
  type: factor.type,
  ...factor.sms ? { sms: deserializeSms(factor.sms) } : {},
  ...factor.totp ? { totp: deserializeTotp(factor.totp) } : {}
});
const deserializeFactorWithSecrets$1 = (factor) => ({
  object: factor.object,
  id: factor.id,
  createdAt: factor.created_at,
  updatedAt: factor.updated_at,
  type: factor.type,
  ...factor.sms ? { sms: deserializeSms(factor.sms) } : {},
  ...factor.totp ? { totp: deserializeTotpWithSecrets(factor.totp) } : {}
});
const deserializeVerifyResponse = (verifyResponse) => ({
  challenge: deserializeChallenge(verifyResponse.challenge),
  valid: verifyResponse.valid
});
var Mfa = class {
  constructor(workos) {
    this.workos = workos;
  }
  async deleteFactor(id) {
    await this.workos.delete(`/auth/factors/${id}`);
  }
  async getFactor(id) {
    const { data } = await this.workos.get(`/auth/factors/${id}`);
    return deserializeFactor$1(data);
  }
  async enrollFactor(options) {
    const { data } = await this.workos.post("/auth/factors/enroll", {
      type: options.type,
      ...(() => {
        switch (options.type) {
          case "sms":
            return { phone_number: options.phoneNumber };
          case "totp":
            return {
              totp_issuer: options.issuer,
              totp_user: options.user
            };
          default:
            return {};
        }
      })()
    });
    return deserializeFactorWithSecrets$1(data);
  }
  async challengeFactor(options) {
    const { data } = await this.workos.post(`/auth/factors/${options.authenticationFactorId}/challenge`, { sms_template: "smsTemplate" in options ? options.smsTemplate : void 0 });
    return deserializeChallenge(data);
  }
  async verifyChallenge(options) {
    const { data } = await this.workos.post(`/auth/challenges/${options.authenticationChallengeId}/verify`, { code: options.code });
    return deserializeVerifyResponse(data);
  }
};
const deserializeAuditLogExport = (auditLogExport) => ({
  object: auditLogExport.object,
  id: auditLogExport.id,
  state: auditLogExport.state,
  url: auditLogExport.url,
  createdAt: auditLogExport.created_at,
  updatedAt: auditLogExport.updated_at
});
const serializeAuditLogExportOptions = (options) => ({
  actions: options.actions,
  actor_names: options.actorNames,
  actor_ids: options.actorIds,
  organization_id: options.organizationId,
  range_end: options.rangeEnd.toISOString(),
  range_start: options.rangeStart.toISOString(),
  targets: options.targets
});
const serializeCreateAuditLogEventOptions = (event) => ({
  action: event.action,
  version: event.version,
  occurred_at: event.occurredAt.toISOString(),
  actor: event.actor,
  targets: event.targets,
  context: {
    location: event.context.location,
    user_agent: event.context.userAgent
  },
  metadata: event.metadata
});
function serializeMetadata(metadata) {
  if (!metadata) return {};
  const serializedMetadata = {};
  Object.keys(metadata).forEach((key) => {
    serializedMetadata[key] = { type: metadata[key] };
  });
  return serializedMetadata;
}
const serializeCreateAuditLogSchemaOptions = (schema) => ({
  actor: { metadata: {
    type: "object",
    properties: serializeMetadata(schema.actor?.metadata)
  } },
  targets: schema.targets.map((target) => {
    return {
      type: target.type,
      metadata: target.metadata ? {
        type: "object",
        properties: serializeMetadata(target.metadata)
      } : void 0
    };
  }),
  metadata: schema.metadata ? {
    type: "object",
    properties: serializeMetadata(schema.metadata)
  } : void 0
});
function deserializeMetadata(metadata) {
  if (!metadata || !metadata.properties) return {};
  const deserializedMetadata = {};
  Object.keys(metadata.properties).forEach((key) => {
    if (metadata.properties) deserializedMetadata[key] = metadata.properties[key].type;
  });
  return deserializedMetadata;
}
const deserializeAuditLogSchema = (auditLogSchema) => ({
  object: auditLogSchema.object,
  version: auditLogSchema.version,
  targets: auditLogSchema.targets.map((target) => {
    return {
      type: target.type,
      metadata: target.metadata ? deserializeMetadata(target.metadata) : void 0
    };
  }),
  actor: { metadata: deserializeMetadata(auditLogSchema.actor?.metadata) },
  metadata: auditLogSchema.metadata ? deserializeMetadata(auditLogSchema.metadata) : void 0,
  createdAt: auditLogSchema.created_at
});
var AuditLogs = class {
  constructor(workos) {
    this.workos = workos;
  }
  async createEvent(organization, event, options = {}) {
    const optionsWithIdempotency = {
      ...options,
      idempotencyKey: options.idempotencyKey || `workos-node-${globalThis.crypto.randomUUID()}`
    };
    await this.workos.post("/audit_logs/events", {
      event: serializeCreateAuditLogEventOptions(event),
      organization_id: organization
    }, optionsWithIdempotency);
  }
  async createExport(options) {
    const { data } = await this.workos.post("/audit_logs/exports", serializeAuditLogExportOptions(options));
    return deserializeAuditLogExport(data);
  }
  async getExport(auditLogExportId) {
    const { data } = await this.workos.get(`/audit_logs/exports/${auditLogExportId}`);
    return deserializeAuditLogExport(data);
  }
  async createSchema(schema, options = {}) {
    const { data } = await this.workos.post(`/audit_logs/actions/${schema.action}/schemas`, serializeCreateAuditLogSchemaOptions(schema), options);
    return deserializeAuditLogSchema(data);
  }
};
let detectedRuntime = null;
function detectRuntime() {
  if (detectedRuntime) return detectedRuntime;
  const global = globalThis;
  if (typeof process !== "undefined" && process.release?.name === "node") detectedRuntime = "node";
  else if (typeof global.Deno !== "undefined") detectedRuntime = "deno";
  else if (typeof navigator !== "undefined" && navigator.userAgent?.includes("Bun")) detectedRuntime = "bun";
  else if (typeof navigator !== "undefined" && navigator.userAgent?.includes("Cloudflare")) detectedRuntime = "cloudflare";
  else if (typeof global !== "undefined" && "fastly" in global) detectedRuntime = "fastly";
  else if (typeof global !== "undefined" && "EdgeRuntime" in global) detectedRuntime = "edge-light";
  else detectedRuntime = "other";
  return detectedRuntime;
}
function getEnvironmentVariable(key) {
  const runtime = detectRuntime();
  const global = globalThis;
  try {
    switch (runtime) {
      case "node":
      case "bun":
      case "edge-light":
        return process.env[key];
      case "deno":
        return global.Deno.env.get(key);
      case "cloudflare":
        return global.env?.[key] ?? global[key];
      case "fastly":
        return global[key];
      default:
        return process?.env?.[key] ?? global.env?.[key] ?? global[key];
    }
  } catch {
    return;
  }
}
function getEnv(key, defaultValue) {
  return getEnvironmentVariable(key) ?? defaultValue;
}
const serializeAuthenticateWithCodeOptions = (options) => ({
  grant_type: "authorization_code",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  code: options.code,
  code_verifier: options.codeVerifier,
  invitation_token: options.invitationToken,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithCodeAndVerifierOptions = (options) => ({
  grant_type: "authorization_code",
  client_id: options.clientId,
  code: options.code,
  code_verifier: options.codeVerifier,
  invitation_token: options.invitationToken,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithMagicAuthOptions = (options) => ({
  grant_type: "urn:workos:oauth:grant-type:magic-auth:code",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  code: options.code,
  email: options.email,
  invitation_token: options.invitationToken,
  link_authorization_code: options.linkAuthorizationCode,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithPasswordOptions = (options) => ({
  grant_type: "password",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  email: options.email,
  password: options.password,
  invitation_token: options.invitationToken,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithRefreshTokenOptions = (options) => ({
  grant_type: "refresh_token",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  refresh_token: options.refreshToken,
  organization_id: options.organizationId,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithRefreshTokenPublicClientOptions = (options) => ({
  grant_type: "refresh_token",
  client_id: options.clientId,
  refresh_token: options.refreshToken,
  organization_id: options.organizationId,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithTotpOptions = (options) => ({
  grant_type: "urn:workos:oauth:grant-type:mfa-totp",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  code: options.code,
  authentication_challenge_id: options.authenticationChallengeId,
  pending_authentication_token: options.pendingAuthenticationToken,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const deserializeAuthenticationResponse = (authenticationResponse) => {
  const { user, organization_id, access_token, refresh_token, authentication_method, impersonator, oauth_tokens, ...rest } = authenticationResponse;
  return {
    user: deserializeUser(user),
    organizationId: organization_id,
    accessToken: access_token,
    refreshToken: refresh_token,
    impersonator,
    authenticationMethod: authentication_method,
    oauthTokens: deserializeOauthTokens(oauth_tokens),
    ...rest
  };
};
const serializeCreateMagicAuthOptions = (options) => ({
  email: options.email,
  invitation_token: options.invitationToken
});
const serializeCreatePasswordResetOptions = (options) => ({ email: options.email });
const serializeEnrollAuthFactorOptions = (options) => ({
  type: options.type,
  totp_issuer: options.totpIssuer,
  totp_user: options.totpUser,
  totp_secret: options.totpSecret
});
const deserializeFactor = (factor) => ({
  object: factor.object,
  id: factor.id,
  createdAt: factor.created_at,
  updatedAt: factor.updated_at,
  type: factor.type,
  totp: deserializeTotp(factor.totp),
  userId: factor.user_id
});
const deserializeFactorWithSecrets = (factor) => ({
  object: factor.object,
  id: factor.id,
  createdAt: factor.created_at,
  updatedAt: factor.updated_at,
  type: factor.type,
  totp: deserializeTotpWithSecrets(factor.totp),
  userId: factor.user_id
});
const serializeListSessionsOptions = (options) => ({ ...options });
const serializeResetPasswordOptions = (options) => ({
  token: options.token,
  new_password: options.newPassword
});
const serializeCreateUserOptions = (options) => ({
  email: options.email,
  password: options.password,
  password_hash: options.passwordHash,
  password_hash_type: options.passwordHashType,
  first_name: options.firstName,
  last_name: options.lastName,
  email_verified: options.emailVerified,
  external_id: options.externalId,
  metadata: options.metadata
});
const serializeUpdateUserOptions = (options) => ({
  email: options.email,
  email_verified: options.emailVerified,
  first_name: options.firstName,
  last_name: options.lastName,
  password: options.password,
  password_hash: options.passwordHash,
  password_hash_type: options.passwordHashType,
  external_id: options.externalId,
  locale: options.locale,
  metadata: options.metadata
});
const VERSION_DELIMITER = "~";
const CURRENT_MAJOR_VERSION = 2;
function parseSeal(seal$1) {
  const [sealWithoutVersion = "", tokenVersionAsString] = seal$1.split(VERSION_DELIMITER);
  return {
    sealWithoutVersion,
    tokenVersion: tokenVersionAsString == null ? null : parseInt(tokenVersionAsString, 10)
  };
}
async function sealData(data, { password }) {
  return `${await seal(data, {
    id: "1",
    secret: password
  }, {
    ...defaults,
    ttl: 0,
    encode: JSON.stringify
  })}${VERSION_DELIMITER}${CURRENT_MAJOR_VERSION}`;
}
async function unsealData(encryptedData, { password }) {
  const { sealWithoutVersion, tokenVersion } = parseSeal(encryptedData);
  const passwordMap = { 1: password };
  let data;
  try {
    data = await unseal(sealWithoutVersion, passwordMap, {
      ...defaults,
      ttl: 0
    }) ?? {};
  } catch (error) {
    if (error instanceof Error && /^(Expired seal|Bad hmac value|Cannot find password|Incorrect number of sealed components|Wrong mac prefix)/.test(error.message)) return {};
    throw error;
  }
  if (tokenVersion === 2) return data;
  else if (tokenVersion !== null) return data.persistent ?? data;
  return data;
}
const serializeAuthenticateWithEmailVerificationOptions = (options) => ({
  grant_type: "urn:workos:oauth:grant-type:email-verification:code",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  pending_authentication_token: options.pendingAuthenticationToken,
  code: options.code,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeAuthenticateWithOrganizationSelectionOptions = (options) => ({
  grant_type: "urn:workos:oauth:grant-type:organization-selection",
  client_id: options.clientId,
  client_secret: options.clientSecret,
  pending_authentication_token: options.pendingAuthenticationToken,
  organization_id: options.organizationId,
  ip_address: options.ipAddress,
  user_agent: options.userAgent
});
const serializeCreateOrganizationMembershipOptions = (options) => ({
  organization_id: options.organizationId,
  user_id: options.userId,
  role_slug: options.roleSlug,
  role_slugs: options.roleSlugs
});
const deserializeIdentities = (identities) => {
  return identities.map((identity) => {
    return {
      idpId: identity.idp_id,
      type: identity.type,
      provider: identity.provider
    };
  });
};
const serializeListInvitationsOptions = (options) => ({
  email: options.email,
  organization_id: options.organizationId,
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const serializeListOrganizationMembershipsOptions = (options) => ({
  user_id: options.userId,
  organization_id: options.organizationId,
  statuses: options.statuses?.join(","),
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const serializeListUsersOptions = (options) => ({
  email: options.email,
  organization_id: options.organizationId,
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const serializeSendInvitationOptions = (options) => ({
  email: options.email,
  organization_id: options.organizationId,
  expires_in_days: options.expiresInDays,
  inviter_user_id: options.inviterUserId,
  role_slug: options.roleSlug
});
const serializeUpdateOrganizationMembershipOptions = (options) => ({
  role_slug: options.roleSlug,
  role_slugs: options.roleSlugs
});
let _josePromise;
function getJose() {
  return _josePromise ??= import("../../../_libs/jose.mjs");
}
var CookieSession = class {
  userManagement;
  cookiePassword;
  sessionData;
  constructor(userManagement, sessionData, cookiePassword) {
    if (!cookiePassword) throw new Error("cookiePassword is required");
    this.userManagement = userManagement;
    this.cookiePassword = cookiePassword;
    this.sessionData = sessionData;
  }
  /**
  * Authenticates a user with a session cookie.
  *
  * @returns An object indicating whether the authentication was successful or not. If successful, it will include the user's session data.
  */
  async authenticate() {
    if (!this.sessionData) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.NO_SESSION_COOKIE_PROVIDED
    };
    const session = await unsealData(this.sessionData, { password: this.cookiePassword });
    if (!session.accessToken) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.INVALID_SESSION_COOKIE
    };
    if (!await this.isValidJwt(session.accessToken)) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.INVALID_JWT
    };
    const { decodeJwt } = await getJose();
    const { sid: sessionId, org_id: organizationId, role, roles, permissions, entitlements, feature_flags: featureFlags } = decodeJwt(session.accessToken);
    return {
      authenticated: true,
      sessionId,
      organizationId,
      role,
      roles,
      permissions,
      entitlements,
      featureFlags,
      user: session.user,
      authenticationMethod: session.authenticationMethod,
      impersonator: session.impersonator,
      accessToken: session.accessToken
    };
  }
  /**
  * Refreshes the user's session.
  *
  * @param options - Optional options for refreshing the session.
  * @param options.cookiePassword - The password to use for the new session cookie.
  * @param options.organizationId - The organization ID to use for the new session cookie.
  * @returns An object indicating whether the refresh was successful or not. If successful, it will include the new sealed session data.
  */
  async refresh(options = {}) {
    const { decodeJwt } = await getJose();
    const session = await unsealData(this.sessionData, { password: this.cookiePassword });
    if (!session.refreshToken || !session.user) return {
      authenticated: false,
      reason: RefreshSessionFailureReason.INVALID_SESSION_COOKIE
    };
    const { org_id: organizationIdFromAccessToken } = decodeJwt(session.accessToken);
    try {
      const cookiePassword = options.cookiePassword ?? this.cookiePassword;
      const authenticationResponse = await this.userManagement.authenticateWithRefreshToken({
        clientId: this.userManagement.clientId,
        refreshToken: session.refreshToken,
        organizationId: options.organizationId ?? organizationIdFromAccessToken,
        session: {
          sealSession: true,
          cookiePassword
        }
      });
      if (options.cookiePassword) this.cookiePassword = options.cookiePassword;
      this.sessionData = authenticationResponse.sealedSession;
      const { sid: sessionId, org_id: organizationId, role, roles, permissions, entitlements, feature_flags: featureFlags } = decodeJwt(authenticationResponse.accessToken);
      return {
        authenticated: true,
        sealedSession: authenticationResponse.sealedSession,
        session: authenticationResponse,
        authenticationMethod: authenticationResponse.authenticationMethod,
        sessionId,
        organizationId,
        role,
        roles,
        permissions,
        entitlements,
        featureFlags,
        user: session.user,
        impersonator: session.impersonator
      };
    } catch (error) {
      if (error instanceof OauthException && (error.error === RefreshSessionFailureReason.INVALID_GRANT || error.error === RefreshSessionFailureReason.MFA_ENROLLMENT || error.error === RefreshSessionFailureReason.SSO_REQUIRED)) return {
        authenticated: false,
        reason: error.error
      };
      throw error;
    }
  }
  /**
  * Gets the URL to redirect the user to for logging out.
  *
  * @returns The URL to redirect the user to for logging out.
  */
  async getLogoutUrl({ returnTo } = {}) {
    const authenticationResponse = await this.authenticate();
    if (!authenticationResponse.authenticated) {
      const { reason } = authenticationResponse;
      throw new Error(`Failed to extract session ID for logout URL: ${reason}`);
    }
    return this.userManagement.getLogoutUrl({
      sessionId: authenticationResponse.sessionId,
      returnTo
    });
  }
  async isValidJwt(accessToken) {
    const { jwtVerify } = await getJose();
    const jwks = await this.userManagement.getJWKS();
    if (!jwks) throw new Error("Missing client ID. Did you provide it when initializing WorkOS?");
    try {
      await jwtVerify(accessToken, jwks);
      return true;
    } catch (e) {
      if (e instanceof Error && "code" in e && typeof e.code === "string" && (e.code.startsWith("ERR_JWT_") || e.code.startsWith("ERR_JWS_"))) return false;
      throw e;
    }
  }
};
var UserManagement = class {
  _jwks;
  clientId;
  constructor(workos) {
    this.workos = workos;
    const { clientId } = workos.options;
    this.clientId = clientId;
  }
  /**
  * Resolve clientId from method options or fall back to constructor-provided value.
  * @throws TypeError if clientId is not available from either source
  */
  resolveClientId(clientId) {
    const resolved = clientId ?? this.clientId;
    if (!resolved) throw new TypeError("clientId is required. Provide it in method options or when initializing WorkOS.");
    return resolved;
  }
  async getJWKS() {
    const { createRemoteJWKSet } = await getJose();
    if (!this.clientId) return;
    this._jwks ??= createRemoteJWKSet(new URL(this.getJwksUrl(this.clientId)), { cooldownDuration: 1e3 * 60 * 5 });
    return this._jwks;
  }
  /**
  * Loads a sealed session using the provided session data and cookie password.
  *
  * @param options - The options for loading the sealed session.
  * @param options.sessionData - The sealed session data.
  * @param options.cookiePassword - The password used to encrypt the session data.
  * @returns The session class.
  */
  loadSealedSession(options) {
    return new CookieSession(this, options.sessionData, options.cookiePassword);
  }
  async getUser(userId) {
    const { data } = await this.workos.get(`/user_management/users/${userId}`);
    return deserializeUser(data);
  }
  async getUserByExternalId(externalId) {
    const { data } = await this.workos.get(`/user_management/users/external_id/${externalId}`);
    return deserializeUser(data);
  }
  async listUsers(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/user_management/users", deserializeUser, options ? serializeListUsersOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, "/user_management/users", deserializeUser, params), options ? serializeListUsersOptions(options) : void 0);
  }
  async createUser(payload) {
    const { data } = await this.workos.post("/user_management/users", serializeCreateUserOptions(payload));
    return deserializeUser(data);
  }
  async authenticateWithMagicAuth(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithMagicAuthOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    }));
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  async authenticateWithPassword(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithPasswordOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    }));
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  /**
  * Exchange an authorization code for tokens.
  *
  * Auto-detects public vs confidential client mode:
  * - If codeVerifier is provided: Uses PKCE flow (public client)
  * - If no codeVerifier: Uses client_secret from API key (confidential client)
  * - If both: Uses both client_secret AND codeVerifier (confidential client with PKCE)
  *
  * Using PKCE with confidential clients is recommended by OAuth 2.1 for defense
  * in depth and provides additional CSRF protection on the authorization flow.
  *
  * @throws Error if neither codeVerifier nor API key is available
  */
  async authenticateWithCode(payload) {
    const { session, clientId, codeVerifier, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    if (codeVerifier !== void 0 && codeVerifier.trim() === "") throw new TypeError("codeVerifier cannot be an empty string. Generate a valid PKCE pair using workos.pkce.generate().");
    const hasApiKey = !!this.workos.key;
    if (!!!codeVerifier && !hasApiKey) throw new TypeError("authenticateWithCode requires either a codeVerifier (for public clients) or an API key configured on the WorkOS instance (for confidential clients).");
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithCodeOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      codeVerifier,
      clientSecret: hasApiKey ? this.workos.key : void 0
    }), { skipApiKeyCheck: !hasApiKey });
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  /**
  * Exchange an authorization code for tokens using PKCE (public client flow).
  * Use this instead of authenticateWithCode() when the client cannot securely
  * store a client_secret (browser, mobile, CLI, desktop apps).
  *
  * @param payload.clientId - Your WorkOS client ID
  * @param payload.code - The authorization code from the OAuth callback
  * @param payload.codeVerifier - The PKCE code verifier used to generate the code challenge
  */
  async authenticateWithCodeAndVerifier(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithCodeAndVerifierOptions({
      ...remainingPayload,
      clientId: resolvedClientId
    }), { skipApiKeyCheck: true });
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  /**
  * Refresh an access token using a refresh token.
  * Automatically detects public client mode - if no API key is configured,
  * omits client_secret from the request.
  */
  async authenticateWithRefreshToken(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const isPublicClient = !this.workos.key;
    const body = isPublicClient ? serializeAuthenticateWithRefreshTokenPublicClientOptions({
      ...remainingPayload,
      clientId: resolvedClientId
    }) : serializeAuthenticateWithRefreshTokenOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    });
    const { data } = await this.workos.post("/user_management/authenticate", body, { skipApiKeyCheck: isPublicClient });
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  async authenticateWithTotp(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithTotpOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    }));
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  async authenticateWithEmailVerification(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithEmailVerificationOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    }));
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  async authenticateWithOrganizationSelection(payload) {
    const { session, clientId, ...remainingPayload } = payload;
    const resolvedClientId = this.resolveClientId(clientId);
    const { data } = await this.workos.post("/user_management/authenticate", serializeAuthenticateWithOrganizationSelectionOptions({
      ...remainingPayload,
      clientId: resolvedClientId,
      clientSecret: this.workos.key
    }));
    return this.prepareAuthenticationResponse({
      authenticationResponse: deserializeAuthenticationResponse(data),
      session
    });
  }
  async authenticateWithSessionCookie({ sessionData, cookiePassword = getEnv("WORKOS_COOKIE_PASSWORD") }) {
    if (!cookiePassword) throw new Error("Cookie password is required");
    if (!await this.getJWKS()) throw new Error("Must provide clientId to initialize JWKS");
    const { decodeJwt } = await getJose();
    if (!sessionData) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.NO_SESSION_COOKIE_PROVIDED
    };
    const session = await unsealData(sessionData, { password: cookiePassword });
    if (!session.accessToken) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.INVALID_SESSION_COOKIE
    };
    if (!await this.isValidJwt(session.accessToken)) return {
      authenticated: false,
      reason: AuthenticateWithSessionCookieFailureReason.INVALID_JWT
    };
    const { sid: sessionId, org_id: organizationId, role, roles, permissions, entitlements, feature_flags: featureFlags } = decodeJwt(session.accessToken);
    return {
      authenticated: true,
      sessionId,
      organizationId,
      role,
      roles,
      user: session.user,
      permissions,
      entitlements,
      featureFlags,
      accessToken: session.accessToken,
      authenticationMethod: session.authenticationMethod
    };
  }
  async isValidJwt(accessToken) {
    const jwks = await this.getJWKS();
    const { jwtVerify } = await getJose();
    if (!jwks) throw new Error("Must provide clientId to initialize JWKS");
    try {
      await jwtVerify(accessToken, jwks);
      return true;
    } catch (e) {
      if (e instanceof Error && "code" in e && typeof e.code === "string" && (e.code.startsWith("ERR_JWT_") || e.code.startsWith("ERR_JWS_"))) return false;
      throw e;
    }
  }
  async prepareAuthenticationResponse({ authenticationResponse, session }) {
    if (session?.sealSession) {
      if (!this.workos.key) throw new Error("Session sealing requires server-side usage with an API key. Public clients should store tokens directly (e.g., secure storage on mobile, keychain on desktop).");
      return {
        ...authenticationResponse,
        sealedSession: await this.sealSessionDataFromAuthenticationResponse({
          authenticationResponse,
          cookiePassword: session.cookiePassword
        })
      };
    }
    return authenticationResponse;
  }
  async sealSessionDataFromAuthenticationResponse({ authenticationResponse, cookiePassword }) {
    if (!cookiePassword) throw new Error("Cookie password is required");
    const { decodeJwt } = await getJose();
    const { org_id: organizationIdFromAccessToken } = decodeJwt(authenticationResponse.accessToken);
    return sealData({
      organizationId: organizationIdFromAccessToken,
      user: authenticationResponse.user,
      accessToken: authenticationResponse.accessToken,
      refreshToken: authenticationResponse.refreshToken,
      authenticationMethod: authenticationResponse.authenticationMethod,
      impersonator: authenticationResponse.impersonator
    }, { password: cookiePassword });
  }
  async getSessionFromCookie({ sessionData, cookiePassword = getEnv("WORKOS_COOKIE_PASSWORD") }) {
    if (!cookiePassword) throw new Error("Cookie password is required");
    if (sessionData) return unsealData(sessionData, { password: cookiePassword });
  }
  async getEmailVerification(emailVerificationId) {
    const { data } = await this.workos.get(`/user_management/email_verification/${emailVerificationId}`);
    return deserializeEmailVerification(data);
  }
  async sendVerificationEmail({ userId }) {
    const { data } = await this.workos.post(`/user_management/users/${userId}/email_verification/send`, {});
    return { user: deserializeUser(data.user) };
  }
  async getMagicAuth(magicAuthId) {
    const { data } = await this.workos.get(`/user_management/magic_auth/${magicAuthId}`);
    return deserializeMagicAuth(data);
  }
  async createMagicAuth(options) {
    const { data } = await this.workos.post("/user_management/magic_auth", serializeCreateMagicAuthOptions({ ...options }));
    return deserializeMagicAuth(data);
  }
  async verifyEmail({ code, userId }) {
    const { data } = await this.workos.post(`/user_management/users/${userId}/email_verification/confirm`, { code });
    return { user: deserializeUser(data.user) };
  }
  async getPasswordReset(passwordResetId) {
    const { data } = await this.workos.get(`/user_management/password_reset/${passwordResetId}`);
    return deserializePasswordReset(data);
  }
  async createPasswordReset(options) {
    const { data } = await this.workos.post("/user_management/password_reset", serializeCreatePasswordResetOptions({ ...options }));
    return deserializePasswordReset(data);
  }
  async resetPassword(payload) {
    const { data } = await this.workos.post("/user_management/password_reset/confirm", serializeResetPasswordOptions(payload));
    return { user: deserializeUser(data.user) };
  }
  async updateUser(payload) {
    const { data } = await this.workos.put(`/user_management/users/${payload.userId}`, serializeUpdateUserOptions(payload));
    return deserializeUser(data);
  }
  async enrollAuthFactor(payload) {
    const { data } = await this.workos.post(`/user_management/users/${payload.userId}/auth_factors`, serializeEnrollAuthFactorOptions(payload));
    return {
      authenticationFactor: deserializeFactorWithSecrets(data.authentication_factor),
      authenticationChallenge: deserializeChallenge(data.authentication_challenge)
    };
  }
  async listAuthFactors(options) {
    const { userId, ...restOfOptions } = options;
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, `/user_management/users/${userId}/auth_factors`, deserializeFactor, restOfOptions), (params) => fetchAndDeserialize(this.workos, `/user_management/users/${userId}/auth_factors`, deserializeFactor, params), restOfOptions);
  }
  async listUserFeatureFlags(options) {
    const { userId, ...paginationOptions } = options;
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, `/user_management/users/${userId}/feature-flags`, deserializeFeatureFlag, paginationOptions), (params) => fetchAndDeserialize(this.workos, `/user_management/users/${userId}/feature-flags`, deserializeFeatureFlag, params), paginationOptions);
  }
  async listSessions(userId, options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, `/user_management/users/${userId}/sessions`, deserializeSession, options ? serializeListSessionsOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, `/user_management/users/${userId}/sessions`, deserializeSession, params), options ? serializeListSessionsOptions(options) : void 0);
  }
  async deleteUser(userId) {
    await this.workos.delete(`/user_management/users/${userId}`);
  }
  async getUserIdentities(userId) {
    if (!userId) throw new TypeError(`Incomplete arguments. Need to specify 'userId'.`);
    const { data } = await this.workos.get(`/user_management/users/${userId}/identities`);
    return deserializeIdentities(data);
  }
  async getOrganizationMembership(organizationMembershipId) {
    const { data } = await this.workos.get(`/user_management/organization_memberships/${organizationMembershipId}`);
    return deserializeOrganizationMembership(data);
  }
  async listOrganizationMemberships(options) {
    const serializedOptions = serializeListOrganizationMembershipsOptions(options);
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/user_management/organization_memberships", deserializeOrganizationMembership, serializedOptions), (params) => fetchAndDeserialize(this.workos, "/user_management/organization_memberships", deserializeOrganizationMembership, params), serializedOptions);
  }
  async createOrganizationMembership(options) {
    const { data } = await this.workos.post("/user_management/organization_memberships", serializeCreateOrganizationMembershipOptions(options));
    return deserializeOrganizationMembership(data);
  }
  async updateOrganizationMembership(organizationMembershipId, options) {
    const { data } = await this.workos.put(`/user_management/organization_memberships/${organizationMembershipId}`, serializeUpdateOrganizationMembershipOptions(options));
    return deserializeOrganizationMembership(data);
  }
  async deleteOrganizationMembership(organizationMembershipId) {
    await this.workos.delete(`/user_management/organization_memberships/${organizationMembershipId}`);
  }
  async deactivateOrganizationMembership(organizationMembershipId) {
    const { data } = await this.workos.put(`/user_management/organization_memberships/${organizationMembershipId}/deactivate`, {});
    return deserializeOrganizationMembership(data);
  }
  async reactivateOrganizationMembership(organizationMembershipId) {
    const { data } = await this.workos.put(`/user_management/organization_memberships/${organizationMembershipId}/reactivate`, {});
    return deserializeOrganizationMembership(data);
  }
  async getInvitation(invitationId) {
    const { data } = await this.workos.get(`/user_management/invitations/${invitationId}`);
    return deserializeInvitation(data);
  }
  async findInvitationByToken(invitationToken) {
    const { data } = await this.workos.get(`/user_management/invitations/by_token/${invitationToken}`);
    return deserializeInvitation(data);
  }
  async listInvitations(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/user_management/invitations", deserializeInvitation, options ? serializeListInvitationsOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, "/user_management/invitations", deserializeInvitation, params), options ? serializeListInvitationsOptions(options) : void 0);
  }
  async sendInvitation(payload) {
    const { data } = await this.workos.post("/user_management/invitations", serializeSendInvitationOptions({ ...payload }));
    return deserializeInvitation(data);
  }
  async acceptInvitation(invitationId) {
    const { data } = await this.workos.post(`/user_management/invitations/${invitationId}/accept`, null);
    return deserializeInvitation(data);
  }
  async revokeInvitation(invitationId) {
    const { data } = await this.workos.post(`/user_management/invitations/${invitationId}/revoke`, null);
    return deserializeInvitation(data);
  }
  async resendInvitation(invitationId) {
    const { data } = await this.workos.post(`/user_management/invitations/${invitationId}/resend`, null);
    return deserializeInvitation(data);
  }
  async revokeSession(payload) {
    await this.workos.post("/user_management/sessions/revoke", serializeRevokeSessionOptions(payload));
  }
  /**
  * Generate an OAuth 2.0 authorization URL.
  *
  * For public clients (browser, mobile, CLI), include PKCE parameters:
  * - Generate PKCE using workos.pkce.generate()
  * - Pass codeChallenge and codeChallengeMethod here
  * - Store codeVerifier and pass to authenticateWithCode() later
  *
  * Or use getAuthorizationUrlWithPKCE() which handles PKCE automatically.
  */
  getAuthorizationUrl(options) {
    const { connectionId, codeChallenge, codeChallengeMethod, clientId, domainHint, loginHint, organizationId, provider, providerQueryParams, providerScopes, prompt, redirectUri, state, screenHint } = options;
    const resolvedClientId = this.resolveClientId(clientId);
    if (!provider && !connectionId && !organizationId) throw new TypeError(`Incomplete arguments. Need to specify either a 'connectionId', 'organizationId', or 'provider'.`);
    if (provider !== "authkit" && screenHint) throw new TypeError(`'screenHint' is only supported for 'authkit' provider`);
    const query = toQueryString({
      connection_id: connectionId,
      code_challenge: codeChallenge,
      code_challenge_method: codeChallengeMethod,
      organization_id: organizationId,
      domain_hint: domainHint,
      login_hint: loginHint,
      provider,
      provider_query_params: providerQueryParams,
      provider_scopes: providerScopes,
      prompt,
      client_id: resolvedClientId,
      redirect_uri: redirectUri,
      response_type: "code",
      state,
      screen_hint: screenHint
    });
    return `${this.workos.baseURL}/user_management/authorize?${query}`;
  }
  /**
  * Generate an OAuth 2.0 authorization URL with automatic PKCE.
  *
  * This method generates PKCE parameters internally and returns them along with
  * the authorization URL. Use this for public clients (CLI apps, Electron, mobile)
  * that cannot securely store a client secret.
  *
  * @returns Object containing url, state, and codeVerifier
  *
  * @example
  * ```typescript
  * const { url, state, codeVerifier } = await workos.userManagement.getAuthorizationUrlWithPKCE({
  *   provider: 'authkit',
  *   clientId: 'client_123',
  *   redirectUri: 'myapp://callback',
  * });
  *
  * // Store state and codeVerifier securely, then redirect user to url
  * // After callback, exchange the code:
  * const response = await workos.userManagement.authenticateWithCode({
  *   code: authorizationCode,
  *   codeVerifier,
  *   clientId: 'client_123',
  * });
  * ```
  */
  async getAuthorizationUrlWithPKCE(options) {
    const { clientId, connectionId, domainHint, loginHint, organizationId, provider, providerQueryParams, providerScopes, prompt, redirectUri, screenHint } = options;
    const resolvedClientId = this.resolveClientId(clientId);
    if (!provider && !connectionId && !organizationId) throw new TypeError(`Incomplete arguments. Need to specify either a 'connectionId', 'organizationId', or 'provider'.`);
    if (provider !== "authkit" && screenHint) throw new TypeError(`'screenHint' is only supported for 'authkit' provider`);
    const pkce = await this.workos.pkce.generate();
    const state = this.workos.pkce.generateCodeVerifier(43);
    const query = toQueryString({
      connection_id: connectionId,
      code_challenge: pkce.codeChallenge,
      code_challenge_method: "S256",
      organization_id: organizationId,
      domain_hint: domainHint,
      login_hint: loginHint,
      provider,
      provider_query_params: providerQueryParams,
      provider_scopes: providerScopes,
      prompt,
      client_id: resolvedClientId,
      redirect_uri: redirectUri,
      response_type: "code",
      state,
      screen_hint: screenHint
    });
    return {
      url: `${this.workos.baseURL}/user_management/authorize?${query}`,
      state,
      codeVerifier: pkce.codeVerifier
    };
  }
  getLogoutUrl(options) {
    const { sessionId, returnTo } = options;
    if (!sessionId) throw new TypeError(`Incomplete arguments. Need to specify 'sessionId'.`);
    const url = new URL("/user_management/sessions/logout", this.workos.baseURL);
    url.searchParams.set("session_id", sessionId);
    if (returnTo) url.searchParams.set("return_to", returnTo);
    return url.toString();
  }
  getJwksUrl(clientId) {
    if (!clientId) throw new TypeError("clientId must be a valid clientId");
    return `${this.workos.baseURL}/sso/jwks/${clientId}`;
  }
};
const serializeCreateResourceOptions = (options) => ({
  resource_type: isResourceInterface(options.resource) ? options.resource.getResourceType() : options.resource.resourceType,
  resource_id: isResourceInterface(options.resource) ? options.resource.getResourceId() : options.resource.resourceId ? options.resource.resourceId : "",
  meta: options.meta
});
const serializeDeleteResourceOptions = (options) => ({
  resource_type: isResourceInterface(options) ? options.getResourceType() : options.resourceType,
  resource_id: isResourceInterface(options) ? options.getResourceId() : options.resourceId ? options.resourceId : ""
});
const serializeBatchWriteResourcesOptions = (options) => {
  let serializedResources = [];
  if (options.op === ResourceOp.Create) serializedResources = options.resources.map((options$1) => serializeCreateResourceOptions(options$1));
  else if (options.op === ResourceOp.Delete) serializedResources = options.resources.map((options$1) => serializeDeleteResourceOptions(options$1));
  return {
    op: options.op,
    resources: serializedResources
  };
};
const serializeListResourceOptions = (options) => ({
  resource_type: options.resourceType,
  search: options.search,
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const serializeListWarrantsOptions = (options) => ({
  resource_type: options.resourceType,
  resource_id: options.resourceId,
  relation: options.relation,
  subject_type: options.subjectType,
  subject_id: options.subjectId,
  subject_relation: options.subjectRelation,
  limit: options.limit,
  after: options.after
});
const serializeQueryOptions = (options) => ({
  q: options.q,
  context: JSON.stringify(options.context),
  limit: options.limit,
  before: options.before,
  after: options.after,
  order: options.order
});
const deserializeQueryResult = (queryResult) => ({
  resourceType: queryResult.resource_type,
  resourceId: queryResult.resource_id,
  relation: queryResult.relation,
  warrant: {
    resourceType: queryResult.warrant.resource_type,
    resourceId: queryResult.warrant.resource_id,
    relation: queryResult.warrant.relation,
    subject: {
      resourceType: queryResult.warrant.subject.resource_type,
      resourceId: queryResult.warrant.subject.resource_id,
      relation: queryResult.warrant.subject.relation
    }
  },
  isImplicit: queryResult.is_implicit,
  meta: queryResult.meta
});
const deserializeResource = (response) => ({
  resourceType: response.resource_type,
  resourceId: response.resource_id,
  meta: response.meta
});
const deserializeBatchWriteResourcesResponse = (response) => {
  return response.data.map((resource) => deserializeResource(resource));
};
const deserializeWarrantToken = (warrantToken) => ({ warrantToken: warrantToken.warrant_token });
const deserializeWarrant = (warrant) => ({
  resourceType: warrant.resource_type,
  resourceId: warrant.resource_id,
  relation: warrant.relation,
  subject: {
    resourceType: warrant.subject.resource_type,
    resourceId: warrant.subject.resource_id,
    relation: warrant.subject.relation
  },
  policy: warrant.policy
});
const serializeWriteWarrantOptions = (warrant) => ({
  op: warrant.op,
  resource_type: isResourceInterface(warrant.resource) ? warrant.resource.getResourceType() : warrant.resource.resourceType,
  resource_id: isResourceInterface(warrant.resource) ? warrant.resource.getResourceId() : warrant.resource.resourceId ? warrant.resource.resourceId : "",
  relation: warrant.relation,
  subject: isSubject(warrant.subject) ? {
    resource_type: warrant.subject.resourceType,
    resource_id: warrant.subject.resourceId
  } : {
    resource_type: warrant.subject.getResourceType(),
    resource_id: warrant.subject.getResourceId()
  },
  policy: warrant.policy
});
var FgaPaginatable = class extends AutoPaginatable {
  list;
  constructor(list, apiCall, options) {
    super(list, apiCall, options);
    this.list = list;
  }
  get warnings() {
    return this.list.warnings;
  }
};
const deserializeFGAList = (response, deserializeFn) => ({
  object: "list",
  data: response.data.map(deserializeFn),
  listMetadata: response.list_metadata,
  warnings: response.warnings
});
const fetchAndDeserializeFGAList = async (workos, endpoint, deserializeFn, options, requestOptions) => {
  const { data: response } = await workos.get(endpoint, {
    query: options,
    ...requestOptions
  });
  return deserializeFGAList(response, deserializeFn);
};
var FGA = class {
  constructor(workos) {
    this.workos = workos;
  }
  async check(checkOptions, options = {}) {
    const { data } = await this.workos.post(`/fga/v1/check`, serializeCheckOptions(checkOptions), options);
    return new CheckResult(data);
  }
  async checkBatch(checkOptions, options = {}) {
    const { data } = await this.workos.post(`/fga/v1/check`, serializeCheckBatchOptions(checkOptions), options);
    return data.map((checkResult) => new CheckResult(checkResult));
  }
  async createResource(resource) {
    const { data } = await this.workos.post("/fga/v1/resources", serializeCreateResourceOptions(resource));
    return deserializeResource(data);
  }
  async getResource(resource) {
    const resourceType = isResourceInterface(resource) ? resource.getResourceType() : resource.resourceType;
    const resourceId = isResourceInterface(resource) ? resource.getResourceId() : resource.resourceId;
    const { data } = await this.workos.get(`/fga/v1/resources/${resourceType}/${resourceId}`);
    return deserializeResource(data);
  }
  async listResources(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/fga/v1/resources", deserializeResource, options ? serializeListResourceOptions(options) : void 0), (params) => fetchAndDeserialize(this.workos, "/fga/v1/resources", deserializeResource, params), options ? serializeListResourceOptions(options) : void 0);
  }
  async updateResource(options) {
    const resourceType = isResourceInterface(options.resource) ? options.resource.getResourceType() : options.resource.resourceType;
    const resourceId = isResourceInterface(options.resource) ? options.resource.getResourceId() : options.resource.resourceId;
    const { data } = await this.workos.put(`/fga/v1/resources/${resourceType}/${resourceId}`, { meta: options.meta });
    return deserializeResource(data);
  }
  async deleteResource(resource) {
    const resourceType = isResourceInterface(resource) ? resource.getResourceType() : resource.resourceType;
    const resourceId = isResourceInterface(resource) ? resource.getResourceId() : resource.resourceId;
    await this.workos.delete(`/fga/v1/resources/${resourceType}/${resourceId}`);
  }
  async batchWriteResources(options) {
    const { data } = await this.workos.post("/fga/v1/resources/batch", serializeBatchWriteResourcesOptions(options));
    return deserializeBatchWriteResourcesResponse(data);
  }
  async writeWarrant(options) {
    const { data } = await this.workos.post("/fga/v1/warrants", serializeWriteWarrantOptions(options));
    return deserializeWarrantToken(data);
  }
  async batchWriteWarrants(options) {
    const { data: warrantToken } = await this.workos.post("/fga/v1/warrants", options.map(serializeWriteWarrantOptions));
    return deserializeWarrantToken(warrantToken);
  }
  async listWarrants(options, requestOptions) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/fga/v1/warrants", deserializeWarrant, options ? serializeListWarrantsOptions(options) : void 0, requestOptions), (params) => fetchAndDeserialize(this.workos, "/fga/v1/warrants", deserializeWarrant, params, requestOptions), options ? serializeListWarrantsOptions(options) : void 0);
  }
  async query(options, requestOptions = {}) {
    return new FgaPaginatable(await fetchAndDeserializeFGAList(this.workos, "/fga/v1/query", deserializeQueryResult, serializeQueryOptions(options), requestOptions), (params) => fetchAndDeserializeFGAList(this.workos, "/fga/v1/query", deserializeQueryResult, params, requestOptions), serializeQueryOptions(options));
  }
};
var FeatureFlags = class {
  constructor(workos) {
    this.workos = workos;
  }
  async listFeatureFlags(options) {
    return new AutoPaginatable(await fetchAndDeserialize(this.workos, "/feature-flags", deserializeFeatureFlag, options), (params) => fetchAndDeserialize(this.workos, "/feature-flags", deserializeFeatureFlag, params), options);
  }
  async getFeatureFlag(slug) {
    const { data } = await this.workos.get(`/feature-flags/${slug}`);
    return deserializeFeatureFlag(data);
  }
  async enableFeatureFlag(slug) {
    const { data } = await this.workos.put(`/feature-flags/${slug}/enable`, {});
    return deserializeFeatureFlag(data);
  }
  async disableFeatureFlag(slug) {
    const { data } = await this.workos.put(`/feature-flags/${slug}/disable`, {});
    return deserializeFeatureFlag(data);
  }
  async addFlagTarget(options) {
    const { slug, targetId } = options;
    await this.workos.post(`/feature-flags/${slug}/targets/${targetId}`, {});
  }
  async removeFlagTarget(options) {
    const { slug, targetId } = options;
    await this.workos.delete(`/feature-flags/${slug}/targets/${targetId}`);
  }
};
const serializeGetTokenOptions = (options) => ({
  organization_id: options.organizationId,
  user_id: options.userId,
  scopes: options.scopes
});
const deserializeGetTokenResponse = (data) => ({ token: data.token });
var Widgets = class {
  constructor(workos) {
    this.workos = workos;
  }
  async getToken(payload) {
    const { data } = await this.workos.post("/widgets/token", serializeGetTokenOptions(payload));
    return deserializeGetTokenResponse(data).token;
  }
};
const MAX_UINT32 = 4294967295;
const CONTINUATION_BIT = 128;
const DATA_BITS_MASK = 127;
const DATA_BITS_PER_BYTE = 7;
const MAX_BYTES_FOR_UINT32 = 5;
function encodeUInt32(value) {
  validateUInt32(value);
  if (value === 0) return new Uint8Array([0]);
  const bytes = [];
  do {
    let byte = value & DATA_BITS_MASK;
    value >>>= DATA_BITS_PER_BYTE;
    if (value !== 0) byte |= CONTINUATION_BIT;
    bytes.push(byte);
  } while (value !== 0);
  return new Uint8Array(bytes);
}
function decodeUInt32(data, offset = 0) {
  validateOffset(data, offset);
  let result = 0;
  let shift = 0;
  let index = offset;
  let bytesRead = 0;
  while (index < data.length) {
    const byte = data[index++];
    bytesRead++;
    if (bytesRead > MAX_BYTES_FOR_UINT32) throw new Error("LEB128 sequence exceeds maximum length for uint32");
    result |= (byte & DATA_BITS_MASK) << shift;
    if (!hasContinuationBit(byte)) return {
      value: result >>> 0,
      nextIndex: index
    };
    shift += DATA_BITS_PER_BYTE;
  }
  throw new Error("Truncated LEB128 encoding");
}
function validateUInt32(value) {
  if (!Number.isFinite(value)) throw new Error("Value must be a finite number");
  if (!Number.isInteger(value)) throw new Error("Value must be an integer");
  if (value < 0) throw new Error("Value must be non-negative");
  if (value > MAX_UINT32) throw new Error(`Value must not exceed ${MAX_UINT32} (MAX_UINT32)`);
}
function validateOffset(data, offset) {
  if (offset < 0 || offset >= data.length) throw new Error(`Offset ${offset} is out of bounds (buffer length: ${data.length})`);
}
function hasContinuationBit(byte) {
  return (byte & CONTINUATION_BIT) !== 0;
}
function base64ToUint8Array(base64) {
  if (typeof atob === "function") {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } else if (typeof Buffer !== "undefined") return new Uint8Array(Buffer.from(base64, "base64"));
  else throw new Error("No base64 decoding implementation available");
}
function uint8ArrayToBase64(bytes) {
  if (typeof btoa === "function") {
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  } else if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
  else throw new Error("No base64 encoding implementation available");
}
const deserializeCreateDataKeyResponse = (key) => ({
  context: key.context,
  dataKey: {
    key: key.data_key,
    id: key.id
  },
  encryptedKeys: key.encrypted_keys
});
const deserializeDecryptDataKeyResponse = (key) => ({
  key: key.data_key,
  id: key.id
});
const deserializeObjectMetadata = (metadata) => ({
  context: metadata.context,
  environmentId: metadata.environment_id,
  id: metadata.id,
  keyId: metadata.key_id,
  updatedAt: new Date(Date.parse(metadata.updated_at)),
  updatedBy: metadata.updated_by,
  versionId: metadata.version_id
});
const deserializeObject = (object) => ({
  id: object.id,
  name: object.name,
  value: object.value,
  metadata: deserializeObjectMetadata(object.metadata)
});
const deserializeObjectDigest = (digest) => ({
  id: digest.id,
  name: digest.name,
  updatedAt: new Date(Date.parse(digest.updated_at))
});
const deserializeListObjects = (list) => ({
  object: "list",
  data: list.data.map(deserializeObjectDigest),
  listMetadata: {
    after: list.list_metadata.after ?? void 0,
    before: list.list_metadata.before ?? void 0
  }
});
const desrializeListObjectVersions = (list) => list.data.map(deserializeObjectVersion);
const deserializeObjectVersion = (version) => ({
  createdAt: new Date(Date.parse(version.created_at)),
  currentVersion: version.current_version,
  id: version.id
});
const serializeCreateObjectEntity = (options) => ({
  name: options.name,
  value: options.value,
  key_context: options.context
});
const serializeUpdateObjectEntity = (options) => ({
  value: options.value,
  version_check: options.versionCheck
});
var Vault = class {
  cryptoProvider;
  constructor(workos) {
    this.workos = workos;
    this.cryptoProvider = workos.getCryptoProvider();
  }
  decode(payload) {
    const inputData = base64ToUint8Array(payload);
    const iv = new Uint8Array(inputData.subarray(0, 12));
    const tag = new Uint8Array(inputData.subarray(12, 28));
    const { value: keyLen, nextIndex } = decodeUInt32(inputData, 28);
    return {
      iv,
      tag,
      keys: uint8ArrayToBase64(inputData.subarray(nextIndex, nextIndex + keyLen)),
      ciphertext: new Uint8Array(inputData.subarray(nextIndex + keyLen))
    };
  }
  async createObject(options) {
    const { data } = await this.workos.post(`/vault/v1/kv`, serializeCreateObjectEntity(options));
    return deserializeObjectMetadata(data);
  }
  async listObjects(options) {
    const url = new URL("/vault/v1/kv", this.workos.baseURL);
    if (options?.after) url.searchParams.set("after", options.after);
    if (options?.limit) url.searchParams.set("limit", options.limit.toString());
    const { data } = await this.workos.get(url.toString());
    return deserializeListObjects(data);
  }
  async listObjectVersions(options) {
    const { data } = await this.workos.get(`/vault/v1/kv/${encodeURIComponent(options.id)}/versions`);
    return desrializeListObjectVersions(data);
  }
  async readObject(options) {
    const { data } = await this.workos.get(`/vault/v1/kv/${encodeURIComponent(options.id)}`);
    return deserializeObject(data);
  }
  async readObjectByName(name) {
    const { data } = await this.workos.get(`/vault/v1/kv/name/${encodeURIComponent(name)}`);
    return deserializeObject(data);
  }
  async describeObject(options) {
    const { data } = await this.workos.get(`/vault/v1/kv/${encodeURIComponent(options.id)}/metadata`);
    return deserializeObject(data);
  }
  async updateObject(options) {
    const { data } = await this.workos.put(`/vault/v1/kv/${encodeURIComponent(options.id)}`, serializeUpdateObjectEntity(options));
    return deserializeObject(data);
  }
  async deleteObject(options) {
    return this.workos.delete(`/vault/v1/kv/${encodeURIComponent(options.id)}`);
  }
  async createDataKey(options) {
    const { data } = await this.workos.post(`/vault/v1/keys/data-key`, options);
    return deserializeCreateDataKeyResponse(data);
  }
  async decryptDataKey(options) {
    const { data } = await this.workos.post(`/vault/v1/keys/decrypt`, options);
    return deserializeDecryptDataKeyResponse(data);
  }
  async encrypt(data, context, associatedData) {
    const keyPair = await this.createDataKey({ context });
    const encoder = new TextEncoder();
    const key = base64ToUint8Array(keyPair.dataKey.key);
    const keyBlob = base64ToUint8Array(keyPair.encryptedKeys);
    const prefixLenBuffer = encodeUInt32(keyBlob.length);
    const aadBuffer = associatedData ? encoder.encode(associatedData) : void 0;
    const iv = this.cryptoProvider.randomBytes(12);
    const { ciphertext, iv: resultIv, tag } = await this.cryptoProvider.encrypt(encoder.encode(data), key, iv, aadBuffer);
    const resultArray = new Uint8Array(resultIv.length + tag.length + prefixLenBuffer.length + keyBlob.length + ciphertext.length);
    let offset = 0;
    resultArray.set(resultIv, offset);
    offset += resultIv.length;
    resultArray.set(tag, offset);
    offset += tag.length;
    resultArray.set(new Uint8Array(prefixLenBuffer), offset);
    offset += prefixLenBuffer.length;
    resultArray.set(keyBlob, offset);
    offset += keyBlob.length;
    resultArray.set(ciphertext, offset);
    return uint8ArrayToBase64(resultArray);
  }
  async decrypt(encryptedData, associatedData) {
    const decoded = this.decode(encryptedData);
    const key = base64ToUint8Array((await this.decryptDataKey({ keys: decoded.keys })).key);
    const encoder = new TextEncoder();
    const aadBuffer = associatedData ? encoder.encode(associatedData) : void 0;
    const decrypted = await this.cryptoProvider.decrypt(decoded.ciphertext, key, decoded.iv, decoded.tag, aadBuffer);
    return new TextDecoder().decode(decrypted);
  }
};
var ConflictException = class extends Error {
  status = 409;
  name = "ConflictException";
  requestID;
  constructor({ error, message, requestID }) {
    super();
    this.requestID = requestID;
    if (message) this.message = message;
    else if (error) this.message = `Error: ${error}`;
    else this.message = `An conflict has occurred on the server.`;
  }
};
function getRuntimeInfo() {
  const name = detectRuntime();
  let version;
  try {
    switch (name) {
      case "node":
        version = typeof process !== "undefined" ? process.version : void 0;
        break;
      case "deno":
        version = globalThis.Deno?.version?.deno;
        break;
      case "bun":
        version = globalThis.Bun?.version || extractBunVersionFromUserAgent();
        break;
      case "cloudflare":
      case "fastly":
      case "edge-light":
      case "other":
      default:
        version = void 0;
        break;
    }
  } catch {
    version = void 0;
  }
  return {
    name,
    version
  };
}
function extractBunVersionFromUserAgent() {
  try {
    if (typeof navigator !== "undefined" && navigator.userAgent) return navigator.userAgent.match(/Bun\/(\d+\.\d+\.\d+)/)?.[1];
  } catch {
  }
}
const VERSION = "8.0.0";
const DEFAULT_HOSTNAME = "api.workos.com";
const HEADER_AUTHORIZATION = "Authorization";
const HEADER_IDEMPOTENCY_KEY = "Idempotency-Key";
const HEADER_WARRANT_TOKEN = "Warrant-Token";
var WorkOS = class {
  baseURL;
  client;
  clientId;
  key;
  options;
  pkce;
  hasApiKey;
  actions;
  apiKeys = new ApiKeys(this);
  auditLogs = new AuditLogs(this);
  directorySync = new DirectorySync(this);
  events = new Events(this);
  featureFlags = new FeatureFlags(this);
  fga = new FGA(this);
  mfa = new Mfa(this);
  organizations = new Organizations(this);
  organizationDomains = new OrganizationDomains(this);
  passwordless = new Passwordless(this);
  pipes = new Pipes(this);
  portal = new Portal(this);
  sso = new SSO(this);
  userManagement;
  vault = new Vault(this);
  webhooks;
  widgets = new Widgets(this);
  /**
  * Create a new WorkOS client.
  *
  * @param keyOrOptions - API key string, or options object
  * @param maybeOptions - Options when first argument is API key
  *
  * @example
  * // Server-side with API key (string)
  * const workos = new WorkOS('sk_...');
  *
  * @example
  * // Server-side with API key (object)
  * const workos = new WorkOS({ apiKey: 'sk_...', clientId: 'client_...' });
  *
  * @example
  * // PKCE/public client (no API key)
  * const workos = new WorkOS({ clientId: 'client_...' });
  */
  constructor(keyOrOptions, maybeOptions) {
    if (typeof keyOrOptions === "object") {
      this.key = keyOrOptions.apiKey;
      this.options = keyOrOptions;
    } else {
      this.key = keyOrOptions;
      this.options = maybeOptions ?? {};
    }
    if (!this.key) this.key = getEnv("WORKOS_API_KEY");
    this.hasApiKey = !!this.key;
    if (this.options.https === void 0) this.options.https = true;
    this.clientId = this.options.clientId;
    if (!this.clientId) this.clientId = getEnv("WORKOS_CLIENT_ID");
    if (!this.hasApiKey && !this.clientId) throw new Error('WorkOS requires either an API key or a clientId. For server-side: new WorkOS("sk_...") or new WorkOS({ apiKey: "sk_..." }). For PKCE/public clients: new WorkOS({ clientId: "client_..." })');
    const protocol = this.options.https ? "https" : "http";
    const apiHostname = this.options.apiHostname || DEFAULT_HOSTNAME;
    const port = this.options.port;
    this.baseURL = `${protocol}://${apiHostname}`;
    if (port) this.baseURL = this.baseURL + `:${port}`;
    this.pkce = new PKCE();
    this.webhooks = this.createWebhookClient();
    this.actions = this.createActionsClient();
    this.userManagement = new UserManagement(this);
    const userAgent = this.createUserAgent(this.options);
    this.client = this.createHttpClient(this.options, userAgent);
  }
  createUserAgent(options) {
    let userAgent = `workos-node/${VERSION}`;
    const { name: runtimeName, version: runtimeVersion } = getRuntimeInfo();
    userAgent += ` (${runtimeName}${runtimeVersion ? `/${runtimeVersion}` : ""})`;
    if (options.appInfo) {
      const { name, version } = options.appInfo;
      userAgent += ` ${name}: ${version}`;
    }
    return userAgent;
  }
  createWebhookClient() {
    return new Webhooks(this.getCryptoProvider());
  }
  createActionsClient() {
    return new Actions(this.getCryptoProvider());
  }
  getCryptoProvider() {
    return new SubtleCryptoProvider();
  }
  createHttpClient(options, userAgent) {
    const headers = { "User-Agent": userAgent };
    const configHeaders = options.config?.headers;
    if (configHeaders && typeof configHeaders === "object" && !Array.isArray(configHeaders) && !(configHeaders instanceof Headers)) Object.assign(headers, configHeaders);
    if (this.key) headers["Authorization"] = `Bearer ${this.key}`;
    return new FetchHttpClient(this.baseURL, {
      ...options.config,
      timeout: options.timeout,
      headers
    });
  }
  get version() {
    return VERSION;
  }
  /**
  * Require API key for methods that need it.
  * @param methodName - Name of the method requiring API key (for error message)
  * @throws ApiKeyRequiredException if no API key was provided
  */
  requireApiKey(methodName) {
    if (!this.hasApiKey) throw new ApiKeyRequiredException(methodName);
  }
  async post(path, entity, options = {}) {
    if (!options.skipApiKeyCheck) this.requireApiKey(path);
    const requestHeaders = {};
    if (options.idempotencyKey) requestHeaders[HEADER_IDEMPOTENCY_KEY] = options.idempotencyKey;
    if (options.warrantToken) requestHeaders[HEADER_WARRANT_TOKEN] = options.warrantToken;
    let res;
    try {
      res = await this.client.post(path, entity, {
        params: options.query,
        headers: requestHeaders
      });
    } catch (error) {
      this.handleHttpError({
        path,
        error
      });
      throw error;
    }
    try {
      return { data: await res.toJSON() };
    } catch (error) {
      await this.handleParseError(error, res);
      throw error;
    }
  }
  async get(path, options = {}) {
    if (!options.skipApiKeyCheck) this.requireApiKey(path);
    const requestHeaders = {};
    if (options.accessToken) requestHeaders[HEADER_AUTHORIZATION] = `Bearer ${options.accessToken}`;
    if (options.warrantToken) requestHeaders[HEADER_WARRANT_TOKEN] = options.warrantToken;
    let res;
    try {
      res = await this.client.get(path, {
        params: options.query,
        headers: requestHeaders
      });
    } catch (error) {
      this.handleHttpError({
        path,
        error
      });
      throw error;
    }
    try {
      return { data: await res.toJSON() };
    } catch (error) {
      await this.handleParseError(error, res);
      throw error;
    }
  }
  async put(path, entity, options = {}) {
    if (!options.skipApiKeyCheck) this.requireApiKey(path);
    const requestHeaders = {};
    if (options.idempotencyKey) requestHeaders[HEADER_IDEMPOTENCY_KEY] = options.idempotencyKey;
    let res;
    try {
      res = await this.client.put(path, entity, {
        params: options.query,
        headers: requestHeaders
      });
    } catch (error) {
      this.handleHttpError({
        path,
        error
      });
      throw error;
    }
    try {
      return { data: await res.toJSON() };
    } catch (error) {
      await this.handleParseError(error, res);
      throw error;
    }
  }
  async delete(path, query) {
    this.requireApiKey(path);
    try {
      await this.client.delete(path, { params: query });
    } catch (error) {
      this.handleHttpError({
        path,
        error
      });
      throw error;
    }
  }
  emitWarning(warning) {
    console.warn(`WorkOS: ${warning}`);
  }
  async handleParseError(error, res) {
    if (error instanceof SyntaxError) {
      const rawResponse = res.getRawResponse();
      const requestID = rawResponse.headers.get("X-Request-ID") ?? "";
      const rawStatus = rawResponse.status;
      const rawBody = await rawResponse.text();
      throw new ParseError({
        message: error.message,
        rawBody,
        rawStatus,
        requestID
      });
    }
  }
  handleHttpError({ path, error }) {
    if (!(error instanceof HttpClientError)) throw new Error(`Unexpected error: ${error}`, { cause: error });
    const { response } = error;
    if (response) {
      const { status, data, headers } = response;
      const requestID = headers["X-Request-ID"] ?? "";
      const { code, error_description: errorDescription, error: error$1, errors, message } = data;
      switch (status) {
        case 401:
          throw new UnauthorizedException(requestID);
        case 409:
          throw new ConflictException({
            requestID,
            message,
            error: error$1
          });
        case 422:
          throw new UnprocessableEntityException({
            code,
            errors,
            message,
            requestID
          });
        case 404:
          throw new NotFoundException({
            code,
            message,
            path,
            requestID
          });
        case 429: {
          const retryAfter = headers.get("Retry-After");
          throw new RateLimitExceededException(data.message, requestID, retryAfter ? Number(retryAfter) : null);
        }
        default:
          if (error$1 || errorDescription) throw new OauthException(status, requestID, error$1, errorDescription, data);
          else if (code && errors) throw new BadRequestException({
            code,
            errors,
            message,
            requestID
          });
          else throw new GenericServerException(status, data.message, data, requestID);
      }
    }
  }
};
var WorkOSNode = class extends WorkOS {
  /** @override */
  createHttpClient(options, userAgent) {
    const headers = {};
    const configHeaders = options.config?.headers;
    if (configHeaders) if (configHeaders instanceof Headers) configHeaders.forEach((v, k) => headers[k] = v);
    else if (Array.isArray(configHeaders)) configHeaders.forEach(([k, v]) => headers[k] = v);
    else Object.assign(headers, configHeaders);
    headers["User-Agent"] = userAgent;
    if (this.key) headers["Authorization"] = `Bearer ${this.key}`;
    const opts = {
      ...options.config,
      timeout: options.timeout,
      headers
    };
    return new FetchHttpClient(this.baseURL, opts, options.fetchFn);
  }
  /** @override */
  createWebhookClient() {
    return new Webhooks(this.getCryptoProvider());
  }
  getCryptoProvider() {
    return new SubtleCryptoProvider();
  }
  /** @override */
  createActionsClient() {
    return new Actions(this.getCryptoProvider());
  }
  /** @override */
  emitWarning(warning) {
    return process.emitWarning(warning, "WorkOS");
  }
};
export {
  WorkOSNode as W
};
