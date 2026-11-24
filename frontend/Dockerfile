# syntax=docker/dockerfile:1

# Stage 1: build the bundle.
#
# Node is pinned to 24.15, the floor in package.json's `engines` (ADR-0018).
FROM node:24.15.0-alpine AS build

WORKDIR /app

# `npm ci` from the lockfile, in its own layer, so editing a component does not
# reinstall the dependency tree.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines VITE_* at build time -- there is no runtime config to change
# afterwards, so the API's location is a property of the image. `/api` is the
# path Caddy strips in stage 2; the default in src/api/client.ts is the
# host-dev http://localhost:8000, which would be wrong in a container.
ARG VITE_API_URL=/api
ENV VITE_API_URL=$VITE_API_URL

# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the
# image rather than shipping a bundle nobody type-checked.
RUN npm run build

# Stage 2: serve it.
#
# Caddy, not nginx, because the same short config does three things the SPA
# needs: static files, the index.html fallback, and the /api reverse proxy.
FROM caddy:2.10-alpine

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv

# Non-root (spec 22 §11). :8080 is above 1024, so no capability is needed to
# bind it. Caddy still wants its XDG directories writable even with
# `auto_https off`, and the base image points those at /data and /config.
RUN adduser -D -H -u 10001 web \
 && mkdir -p /data /config \
 && chown -R web:web /data /config
USER web

EXPOSE 8080
