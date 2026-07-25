# Identity Package

Own authentication, authorization, tokens, bootstrap, and workspace identity.
Authorization decisions stay deterministic; durable access uses explicit
database repositories and runtime context uses narrow protocols. Preserve
tenant scope and admin/worker/machine token semantics. Keep focused evidence for
distinct scope, token-kind, revocation, forgery, single-use, and secret-leak
outcomes through a real issuer and protected request.
