FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
RUN pip install --no-cache-dir uv==0.7.15
COPY pyproject.toml uv.lock ./
COPY backend/ontology/ backend/ontology/
RUN uv sync --frozen --no-dev --no-editable
COPY backend/migrations/ backend/migrations/
COPY alembic.ini ./
COPY deploy/ deploy/
COPY LICENSE NOTICE ./
COPY third_party/README.md third_party/README.md
COPY --from=frontend /build/dist/ frontend/dist/
RUN useradd --uid 10001 --create-home ontweft && mkdir /data && chown ontweft /data
USER ontweft
ENV ONTOLOGY_DATA_DIR=/data ONTOLOGY_FRONTEND_DIR=/app/frontend/dist

FROM runtime AS demo
ARG VERSION=development
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/xiaohuihuiaijia/ontweft" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --chown=ontweft scripts/start_local_demo.py scripts/experience_sample.py ./scripts/
COPY --chown=ontweft examples/product-inventory/ ./examples/product-inventory/
EXPOSE 8020
CMD ["python", "scripts/start_local_demo.py", "experience", "--container", "--data-dir", "/data"]

# Keep the production target last: existing Compose builds retain their entrypoint.
FROM runtime AS production
EXPOSE 8000
CMD ["ontology", "serve", "--host", "0.0.0.0", "--port", "8000"]
