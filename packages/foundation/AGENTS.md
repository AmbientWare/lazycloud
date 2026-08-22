# Foundation package

Small, narrowly named, protocol-neutral helpers: process, network, shell,
payload, validation, header and path, URL, and ID.

Nothing here decides a workflow. No apps, SDK, persistence clients, services,
providers, schedulers, workers, or composition. A helper that needs any of those
belongs to the domain that owns them.

The failure mode this package guards against is becoming a junk drawer. Name each
helper for exactly what it does rather than for where it happened to be needed,
and when a domain owner appears for something living here, move it there and
delete the old path.
