# Resend provider

Transactional email: the one call that delivers a message somebody else wrote.

The adapter takes a finished `shared.email.EmailMessage` and posts it. Which
messages exist, what they say, and where their links point are decided by the
domain package that sends them, so a second email provider is a second file here
and nothing anywhere else.

The sender address must be on a domain the Resend account has verified, and
Resend refuses anything else. A refusal is surfaced rather than logged and
swallowed, because a message the dashboard reports as sent and nobody receives
is the failure this package exists to make loud.

Which refusal it was decides what the caller does next, so the two are separate
error types. A malformed recipient is ours to fix and will be refused the same
way forever, so it is an `InvalidInputError`. A rate limit, a rejected key and
an unverified sending domain are not the caller's doing and are fixed by waiting
or by an operator, so they stay `UpstreamUnavailableError` and the thing to do
about them is send again later.
