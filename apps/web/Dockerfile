##### DEPENDENCIES

FROM --platform=linux/amd64 oven/bun:1-alpine AS deps
RUN apk add --no-cache libc6-compat openssl
WORKDIR /app

# Install dependencies
COPY apps/web/package.json apps/web/bun.lock ./

RUN bun install --frozen-lockfile

##### BUILDER

FROM --platform=linux/amd64 oven/bun:1-alpine AS builder
RUN apk add --no-cache libc6-compat
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ .

# Build the TanStack Start app (outputs to .output/)
ENV NODE_ENV=production
RUN bun run build

##### RUNNER

FROM --platform=linux/amd64 oven/bun:1-alpine AS runner
RUN apk add --no-cache curl
WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3000
ENV HOST=0.0.0.0

# Copy the built output
COPY --from=builder /app/.output ./.output
COPY --from=builder /app/package.json ./package.json

EXPOSE 3000

# TanStack Start with Nitro outputs a server entry point
CMD ["bun", ".output/server/index.mjs"]
