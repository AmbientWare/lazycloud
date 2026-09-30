# Foundation

- Keep helpers small, specifically named and protocol-neutral.
- No workflows, persistence clients, app/SDK behavior or composition. Move behavior
  to its domain owner when one exists; do not make a catch-all utility package.
