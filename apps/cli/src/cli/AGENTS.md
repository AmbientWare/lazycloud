# CLI commands

- Keep Typer/Rich commands thin: validate, call a typed client, format output.
- Preserve exit codes, machine-readable output and genuine progress.
- Use shared typed errors and redact secrets; avoid command-specific transport
  parsing and compatibility commands.
