# PatchPilot dashboard: built with Vite, served by nginx, proxying /api to the
# API service so the browser only ever makes same-origin requests.

FROM node:22-alpine AS build
WORKDIR /app

COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY apps/web/ ./
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY infra/nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

ENV PATCHPILOT_API_URL=http://api:8000
EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
