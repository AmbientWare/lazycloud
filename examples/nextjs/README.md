# Next.js Landing Page Example

A simple Next.js landing page ready to deploy with LazyCloud.

## Files

- `app/` - Next.js App Router pages
- `package.json` - Dependencies
- `Dockerfile` - Production build with standalone output
- `compose.yaml` - Docker Compose with LazyCloud labels

## Deploy

```bash
cd examples/nextjs
lazycloud init
lazycloud deploy
```

## Local Development

```bash
npm install
npm run dev
```

Then visit http://localhost:3000
