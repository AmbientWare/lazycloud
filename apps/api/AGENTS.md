# API app

FastAPI construction and request and process lifetime for the control plane.
This is where settings, repositories, clients, and services are composed.

Reusable workflows stay in domain packages, and SDK abstractions never enter
backend composition. Register domain error mapping centrally, by exception type,
rather than per route. A new domain error is then handled everywhere at once
instead of wherever someone remembered.

Router registration order matters where a fallback route exists: a catch-all
registered too early silently claims paths that belong to something else.
