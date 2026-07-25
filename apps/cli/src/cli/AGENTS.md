# Operator CLI Implementation

Use Typer/Rich commands that validate input, call typed clients/services, format
output, and preserve stable exits. Public command implementations are reused
from `lazycloud.cli`; internal composition remains here. Map typed transport and
operation errors through `cli.components.errors`, mask secrets, and do not add
compatibility commands or per-command soft-error parsing.
