# Public SDK and CLI

- Import no other package in this repository; no backend packages or clients.
  `contracts` and `_shared` import nothing else from `lazycloud`, because the
  runner loads them.
- Keep transport clients in `clients`, bound workflows in `session` and public
  commands in `cli`. Inject clients explicitly; translate `HttpApiError` once
  into typed operation errors.
- Image context/digests cover manifests and declared local dependencies, excluding
  root source code. Refuse dependency paths outside the project root.
- Source sync always applies baseline exclusions, including secrets. Only public
  sync entrypoints create a missing `.lazycloudignore`; collection helpers and
  image builds do not write into source or dependency directories.
- Examples under `_examples` use only public SDK/CLI interfaces and stay aligned
  with the docs. EXAMPLES.md covers the catalog.
