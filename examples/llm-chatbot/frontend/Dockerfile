FROM oven/bun:1 AS builder

WORKDIR /app

COPY package.json bun.lock* ./
RUN bun install

COPY . .
RUN bun run build

FROM oven/bun:1 AS runner

WORKDIR /app

ENV NODE_ENV=production

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["bun", "server.js"]
