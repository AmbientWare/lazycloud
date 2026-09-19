# API app

FastAPI construction and request and process lifetime for the control plane.
This is where settings, repositories, clients, and services are composed.

Reusable workflows stay in domain packages, and SDK abstractions never enter
backend composition. Register domain error mapping centrally, by exception type,
rather than per route. A new domain error is then handled everywhere at once
instead of wherever someone remembered.

Router registration order matters where a fallback route exists: a catch-all
registered too early silently claims paths that belong to something else.

Machines are account resources. `POST /api/v1/machines/join-command` and
`PATCH /api/v1/machines/{id}` act for the signed-in account, which is the only
thing that owns a machine name; `GET /api/v1/machines` reads the workspace the
request names. There is no route that creates a machine directly or hands out a
bare join token: the command is the credential's only rendering.
`GET /api/v1/machines/self-hosted` returns the account's machines whose
placement is a machine, and `GET /api/v1/compute/instances` the machines placed
on the account's connection, each joined to its provider row; both carry the
machine lifecycle, and `GET /api/v1/compute/summary` counts the same list.
Platform machines are never returned to a customer.
