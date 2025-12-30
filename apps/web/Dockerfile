##### DEPENDENCIES

FROM --platform=linux/amd64 oven/bun:1-alpine AS deps
RUN apk add --no-cache libc6-compat openssl
WORKDIR /app

# Install dependencies
COPY apps/web/package.json apps/web/bun.lock ./

RUN bun install --frozen-lockfile

##### BUILDER

FROM --platform=linux/amd64 oven/bun:1-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ .

RUN SKIP_ENV_VALIDATION=1 bun run build

##### RUNNER

FROM --platform=linux/amd64 oven/bun:1-alpine AS runner
RUN apk add --no-cache curl
WORKDIR /app

ENV NODE_ENV=production

COPY --from=builder /app/next.config.js ./
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

CMD ["bun", "server.js"]
