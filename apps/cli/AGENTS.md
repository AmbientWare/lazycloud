# Operator CLI App

Own internal `lazycloud-admin`: admin, diagnostics, local-service, and
backend-backed commands that are not public SDK features. It remains a superset
of `lazycloud`; shared public workflows use `lazycloud.cli`. Keep commands thin,
machine-readable output stable and secret-safe, and public/operator surfaces in
sync when a capability belongs in both.
