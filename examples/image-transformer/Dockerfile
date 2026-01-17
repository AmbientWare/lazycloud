FROM oven/bun:1-debian AS builder

WORKDIR /app

# Install Python and build tools for native modules (better-sqlite3)
RUN apt-get update && apt-get install -y python3 build-essential && rm -rf /var/lib/apt/lists/*

COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile

COPY . .
RUN bun run build

FROM oven/bun:1-debian

WORKDIR /app

COPY --from=builder /app/.output ./.output
COPY --from=builder /app/package.json ./

EXPOSE 3000

CMD ["bun", ".output/server/index.mjs"]
