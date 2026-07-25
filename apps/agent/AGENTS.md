# Agent App

Own only the private-pool agent entrypoint: arguments, settings, daemon startup,
and route-proxy wiring. Reusable behavior belongs in `packages/agent`. Gateway
transport failures use `HttpApiError`; keep environment names aligned with
install and join flows. Accept entrypoint changes through real daemon startup or
the affected route-proxy failure.
