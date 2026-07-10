# ── Eido Web — Production Dockerfile ─────────────────────────────────────────
# Next.js standalone build — minimal image with no devDependencies.
# node:20-alpine@sha256 — verified 2026-04-30

FROM node:20-alpine@sha256:2d07db07a2df6830718ae2a47db6fedce6745f5bcd174c398f2acdda90a11c03 AS base
RUN corepack enable && corepack prepare pnpm@9.0.0 --activate

# ── Deps stage ────────────────────────────────────────────────────────────────
FROM base AS deps
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/
RUN pnpm install --frozen-lockfile --filter @eido/web

# ── Builder stage ─────────────────────────────────────────────────────────────
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/web/node_modules ./apps/web/node_modules
COPY . .

# Build-time env — placeholders replaced by Enclii secrets at runtime
ENV NEXT_PUBLIC_API_URL=https://api.eido.cam
ENV NEXT_PUBLIC_CDN_URL=https://cdn.eido.cam
ENV NEXT_PUBLIC_JANUA_URL=https://auth.madfam.io
ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app/apps/web
RUN pnpm build

# ── Runner stage ──────────────────────────────────────────────────────────────
FROM node:20-alpine@sha256:2d07db07a2df6830718ae2a47db6fedce6745f5bcd174c398f2acdda90a11c03 AS runner

# busybox (alpine) syntax — Debian-style long options exit 1 here
RUN addgroup -S -g 1001 eido && \
    adduser -S -u 1001 -G eido eido

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000

# Next.js standalone output
COPY --from=builder --chown=eido:eido /app/apps/web/.next/standalone ./
COPY --from=builder --chown=eido:eido /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=eido:eido /app/apps/web/public ./apps/web/public

USER eido
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://localhost:3000/api/health || exit 1

CMD ["node", "apps/web/server.js"]
