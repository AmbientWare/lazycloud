# GitHub Provider

The GitHub App a person signs in through. Authentication only: this adapter learns
who someone is and nothing else.

- An account is keyed on GitHub's numeric user id and never on the login or the
  email. A login can be renamed and, once released, registered by somebody else;
  an email address can be moved between accounts. Keying on either is how one
  person ends up signed in as another.
- The user access token exists inside `identify` and nowhere else. It is not
  returned, stored, logged, or put in an error, because a token that leaves this
  boundary becomes something every caller above has to be trusted with.
- The redirect URI is configuration. Deriving it from the request would let the
  caller's Host header choose a redirect target GitHub will honour.
- A rejected authorization code comes back as HTTP 200 with an `error` field, so
  the status code alone does not say whether an exchange succeeded.
- No verified primary email is a real state a person can be in and sign-in
  continues without one. A refusal to list emails at all is a missing App
  permission, and that fails loudly rather than reading as "no email".
- The App needs no repository access and no installation. A person authorizes it
  without installing it anywhere, which is what makes login-only work.
