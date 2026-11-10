# ghcr.io/xeroc/x2llm — CLI + stdio MCP server as a deployable unit.
# No store baked in: configure with env creds (X_AUTH_TOKEN/X_CT0) or
# DATABASE_URL at runtime.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN uv build --wheel

FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title=x2llm \
      org.opencontainers.image.version=${VERSION} \
      org.opencontainers.image.description="Read-only x.com client for LLM agents: CLI and stdio MCP server"
RUN useradd --create-home --system x2llm
COPY --from=build /src/dist/*.whl /tmp/
RUN WHEEL="$(ls /tmp/*.whl)" \
    && pip install --no-cache-dir "$WHEEL[postgres]" \
    && rm -f /tmp/*.whl
USER x2llm
ENV HOME=/home/x2llm
ENTRYPOINT ["x2llm"]
CMD ["mcp"]
