# Resend provider

Transactional email: the one call that delivers a message somebody else wrote.

The adapter takes a finished `shared.email.EmailMessage` and posts it. Which
messages exist, what they say, and where their links point are decided by the
domain package that sends them, so a second email provider is a second file here
and nothing anywhere else.

The sender address must be on a domain the Resend account has verified. Resend
refuses anything else with a 403 that names the domain, and that refusal is
surfaced verbatim as an `UpstreamUnavailableError` rather than logged and
swallowed: an invitation the dashboard reports as sent and nobody receives is
the failure this package exists to make loud.
