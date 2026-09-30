# GitHub identity

- Authenticate by numeric user ID, never login/email. Access tokens remain inside
  `identify` and are never returned, stored or logged.
- Use the configured redirect URI, not request Host headers. Check exchange bodies
  for errors even on HTTP 200.
- Missing verified email is valid; missing permission to read email fails explicitly.
  Login requires neither repository access nor App installation.
