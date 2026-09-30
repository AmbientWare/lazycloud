# Cloudflare

- Own customer hostnames through a scoped zone token; storage stays with its owner.
- Mutate provider-assigned hostname IDs, not reusable customer names. Register the
  exact hostname; wildcard customer registrations are outside the public contract.
- Already-absent deletion succeeds. Distinguish invalid input from retryable
  provider failures; never expose tokens in logs, errors or reprs.
