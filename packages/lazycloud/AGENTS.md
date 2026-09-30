# Public SDK and CLI

- Depend only on `shared` within this repository; no backend packages or clients.
- Keep transport clients in `clients`, bound workflows in `session` and public
  commands in `cli`. Inject clients explicitly; translate `HttpApiError` once
  into typed operation errors.
- Image context/digests cover manifests and declared local dependencies, excluding
  root source code. Refuse dependency paths outside the project root.
- Source sync always applies baseline exclusions, including secrets. Only public
  sync entrypoints create a missing `.lazycloudignore`; collection helpers and
  image builds do not write into source or dependency directories.
