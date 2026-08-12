# Billing Package

What an account is on, what it owes, and what happens when it does not pay.

Pricing vocabulary lives in `shared.billing`; the payment provider protocol lives
in `shared.payments`; the rates and the report that applies them live in
`observability`. This package owns the account: its plan, its standing, and the
decisions that read them.

- One billing account per user, never per workspace. Someone running dev, staging
  and prod holds three workspaces and one payment relationship, and resolution
  goes `workspace -> owner -> account` for the same reason a connected cloud
  account does.
- Absence is the free plan. Nothing writes a row to record that an account has
  not agreed to pay, so no caller has to decide what a missing row means, and the
  free default carries no identifier that could be mistaken for a stored one.
- A plan and a standing change for unrelated reasons. A plan changes when someone
  chooses; standing changes when a payment succeeds or fails. Reading one to
  infer the other is how an account that owes money keeps its entitlements.
