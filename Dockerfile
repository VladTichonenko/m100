FROM node:22-slim AS frontend
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY --from=frontend /web/dist /app/web/dist
ENV WEB_HOST=0.0.0.0 WEB_AUTO_OPEN=false
RUN mkdir -p /app/data /app/secrets
CMD ["/app/.venv/bin/m100-bot"]
