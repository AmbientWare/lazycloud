# LazyCloud Web

TanStack Start frontend for the LazyCloud deployment dashboard.

## Tech Stack

- [TanStack Start](https://tanstack.com/start) - Full-stack React framework
- [TanStack Router](https://tanstack.com/router) - File-based routing
- [TanStack Query](https://tanstack.com/query) - Data fetching
- [Tailwind CSS](https://tailwindcss.com/) - Styling
- [Shadcn/ui](https://ui.shadcn.com/) - UI components
- [WorkOS](https://workos.com/) - Authentication
- [T3 Env](https://env.t3.gg/) - Type-safe environment variables
- [Fumadocs](https://fumadocs.vercel.app/) - Documentation

## Development

```bash
# Install dependencies
bun install

# Start dev server
bun dev

# Build for production
bun run build

# Run tests
bun test

# Lint & format
bun lint
bun format
```

## Environment Variables

Copy `.env.example` to `.env.local` and fill in the values. See `src/env.ts` for the full schema.

Required server-side variables:
- `API_URL` - Backend API URL
- `API_PREFIX` - API path prefix
- `ADMIN_API_KEY` - Admin API key
- `WORKOS_CLIENT_ID` - WorkOS client ID
- `WORKOS_API_KEY` - WorkOS API key
- `WORKOS_COOKIE_PASSWORD` - WorkOS session cookie password
- `WORKOS_REDIRECT_URI` - WorkOS OAuth callback URL
- `POLAR_ACCESS_TOKEN` - Polar billing token
- `SUPPORT_EMAIL` - Support email address

## Project Structure

```
src/
├── routes/           # File-based routes
│   ├── __root.tsx    # Root layout
│   ├── _authenticated/  # Auth-protected routes
│   ├── _landing/     # Public landing pages
│   └── docs/         # Documentation pages
├── components/       # Shared components
├── server/          # Server functions
├── integrations/    # Third-party integrations
├── lib/             # Utilities
└── env.ts           # Environment schema
```

## Adding UI Components

```bash
bunx shadcn@latest add button
```
