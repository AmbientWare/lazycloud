# Resend

- Send finished shared email messages; domains own content and link targets.
- Invalid recipients raise `InvalidInputError`; credentials, rate limits and
  unverified senders raise `UpstreamUnavailableError`. Never swallow refusals.
- Verify webhook signatures with constant-time comparison and timestamp bounds.
  Ignore unsupported event kinds without provoking provider retries.
