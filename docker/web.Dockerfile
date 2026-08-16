# Frontend image — Next.js standalone output.

FROM node:22-alpine AS deps
WORKDIR /app
RUN corepack enable
# No `pnpm-lock.yaml*` glob and no `|| pnpm install` fallback. Both were ways
# of saying "carry on even if the lockfile is missing or stale", which turns a
# reproducible build into whatever the registry happened to resolve that day —
# and does it silently, so the image looks fine. If the lockfile is out of sync
# the build should stop and someone should regenerate it.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

USER nextjs
EXPOSE 3000

CMD ["node_modules/.bin/next", "start", "-p", "3000"]
