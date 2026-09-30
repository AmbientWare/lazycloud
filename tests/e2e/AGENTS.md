# Live acceptance

- Run an exact named module or browser scenario against healthy current-code
  infrastructure. Setup/build/deployment/reset belongs outside the scenario.
- Use canonical local Compose; external targets must be explicitly authorized.
  Keep scenarios opt-in and outside ordinary pytest discovery.
- Exercise public production workflows, not private datastore shortcuts, fake
  agents or mocked provider plans. Keep helpers local and small.
- Use unique, precisely owned resources; verify terminal outcomes and public-path
  cleanup. Exit 0 requires proof, 77 means missing prerequisites, and assertion or
  cleanup failures are nonzero.
- An explicitly selected paid scenario authorizes its scoped resources. Use the
  production capacity owner and prove cost cleanup; do not provision around it.
- Connected-account scenarios validate the exact customer account/template and use
  the public authorization action. Existing approved credentials are valid.
