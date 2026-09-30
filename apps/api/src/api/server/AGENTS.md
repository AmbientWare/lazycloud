# API handlers

- Use native FastAPI validation and precise shared HTTP request/response models.
- Call `authorize_token_workspace` for every bearer request's workspace,
  including path/query defaults. Account resources use `read_user`/`write_user`.
- Administrative unit operations resolve by ID and remain platform-scoped.
- WebSocket tickets are single-use and enforce the same workspace authorization.
