# Identity

- Own principals, sessions, tokens, membership and authorization. Keep authorization
  decisions deterministic with explicit membership and resource inputs.
- A token names one user or workspace principal. User authority follows current
  membership; platform-minted workspace credentials remain workspace-scoped.
  Enforce kind, scope, revocation, single-use and secret protection separately.
- Revocation is terminal; retain human credential history. Administrator standing
  comes from `AuthService.platform_role`. Lock administrator rows when preventing
  removal/disablement of the last active administrator; offline recovery is separate.
- Enforce one owner per tenant workspace. The platform namespace has no human
  members, billing or tenant storage and is excluded from public workspace access.
- Invitations grant access to the signed-in holder of the secret, not an email
  identity. Hash secrets, redeem under a lock and rotate them on resend.
  Only acceptance creates membership; open invitations reserve seats.
- Queue invitations atomically with their outbox message. Keep open offers only;
  membership and audit history record outcomes. The service determines expiry.
- External identities use immutable provider subjects, never email or login.
  Provider access tokens stay inside the adapter and are discarded after use.
- Sign-in uses hashed, single-use state/exchange codes and the initiating cookie.
  Consume codes before validation; never place session credentials in URLs.
- Complete idempotent account provisioning before minting an exchange code.
  Offline bootstrap credentials must remain independent of identity providers.
