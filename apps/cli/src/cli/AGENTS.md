# Operator CLI implementation

Typer and Rich command implementations for the operator CLI.

A command validates input, calls a typed client or service, formats the result,
and returns a stable exit code. Public command implementations are reused from
the public CLI package; only internal composition lives here.

Map typed transport and operation errors through the shared error components so
that every command fails the same way, mask secrets in every rendering, and do
not add compatibility commands or per-command parsing of error payloads.
