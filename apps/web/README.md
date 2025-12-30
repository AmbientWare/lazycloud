# LazyCloud Web

A modern web application built with Next.js, featuring authentication, database integration, and a beautiful UI.

## Features

- **Modern Tech Stack**
  - Next.js 16 with App Router
  - TypeScript for type safety
  - Tailwind CSS for styling
  - Drizzle ORM for database operations
  - WorkOS for authentication
  - React Query for data fetching
  - MDX support for content

- **Development Experience**
  - Hot reloading with Turbopack
  - ESLint and Prettier for code quality
  - TypeScript for type checking
  - Docker support for containerization

## Prerequisites

- Bun (v1.3.5 or higher)
- Docker and Docker Compose (for containerized deployment)
- PostgreSQL database

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/AmbientWare/machines-web
   cd machines-web
   ```

2. Install dependencies:
   ```bash
   bun install
   ```

3. Set up environment variables:
   ```bash
   cp .env.example .env
   ```
   Fill in the required environment variables in the `.env` file.

## Development

Start the development server:
```bash
bun dev
```

The application will be available at `http://localhost:3000`.

## Building for Production

Build the application:
```bash
bun run build
```

Start the production server:
```bash
bun start
```

## Docker Deployment

The application can be deployed using Docker Compose:

```bash
docker compose up -d
```

This will start both the web application and an ngrok tunnel for external access.

## Available Scripts

- `bun dev` - Start development server with Turbopack
- `bun run build` - Build the application
- `bun start` - Start production server
- `bun lint` - Run ESLint
- `bun lint:fix` - Fix ESLint errors
- `bun format:check` - Check code formatting
- `bun format:write` - Format code
- `bun typecheck` - Run TypeScript type checking
- `bun db:generate` - Generate database migrations
- `bun db:push` - Push database changes
- `bun db:studio` - Open Drizzle Studio

## Environment Variables

Required environment variables (see `.env.example` for full list):

### Authentication (WorkOS)
- `WORKOS_API_KEY` - WorkOS API key for server-side operations
- `WORKOS_CLIENT_ID` - WorkOS client ID
- `WORKOS_COOKIE_PASSWORD` - Secure password for session cookies (min 32 characters)
- `WORKOS_REDIRECT_URI` - OAuth callback URL (e.g., `http://localhost:3000/callback`)

### Database
- `DATABASE_URL` - PostgreSQL connection string

### Other Services
- `POLAR_ACCESS_TOKEN` - Polar.sh API token for billing
- `RESEND_API_KEY` - Resend API key for emails
- `UPSTASH_REDIS_REST_URL` - Upstash Redis URL (optional, for rate limiting)
- `UPSTASH_REDIS_REST_TOKEN` - Upstash Redis token (optional)
