# HarkenIQ Site Manager — gRPC ingest + HTTP API/dashboard in one process.
# Build context is the repository root:
#   docker build -f deploy/site_manager/Dockerfile .

FROM node:22-slim AS ui-build
WORKDIR /ui
COPY services/site_manager/ui/package.json services/site_manager/ui/package-lock.json* ./
RUN npm install
COPY services/site_manager/ui/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# Agent package (harkeniq) provides proto stubs; SM package depends on it.
COPY pyproject.toml README.md* ./
COPY src/ src/
COPY skills/ skills/
RUN pip install --no-cache-dir .

COPY services/site_manager/pyproject.toml services/site_manager/alembic.ini ./sm/
COPY services/site_manager/src/ ./sm/src/
RUN pip install --no-cache-dir ./sm

COPY --from=ui-build /ui/dist/ ./sm/ui/dist/
COPY deploy/site_manager/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HARKEN_SM_UI_DIST=/app/sm/ui/dist
EXPOSE 8080 50051
ENTRYPOINT ["/entrypoint.sh"]
