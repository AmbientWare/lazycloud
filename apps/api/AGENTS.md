# API App

FastAPI construction and request and process lifetime for the control plane.
Settings, repositories, clients, and services are composed here.

Reusable workflows stay in domain packages, and SDK abstractions never enter
backend composition. Register domain error mapping centrally, by exception type,
rather than per route—a new domain error should then be handled everywhere at
once instead of wherever someone remembered.

Router registration order is load-bearing where a fallback route exists: a
catch-all registered too early silently claims paths that belong to something
else.
