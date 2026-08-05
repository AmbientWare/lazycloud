# Identity Package

Authentication, authorization, tokens, bootstrap, and workspace identity.

Authorization decisions stay deterministic and inspectable: the same principal
and the same resource yield the same answer, with no ambient state quietly
deciding it. Durable access uses explicit database repositories, and runtime
context arrives through narrow protocols.

Tenant scope and the distinctions between admin, worker, and machine tokens are
load-bearing—preserve them explicitly rather than deriving them. Treat token
scope, token kind, revocation, forgery resistance, single use, and secret
non-disclosure as separate properties that each have to hold on their own.
