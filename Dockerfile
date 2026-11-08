# ownerwatch-mcp: read-only EVM admin-surface MCP server (stdio).
# Builds from source so it works before the PyPI release. Needs network only at runtime (RPC calls).
FROM python:3.12-slim AS build
WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir --upgrade pip build && pip wheel --no-cache-dir --no-deps -w /wheels .

FROM python:3.12-slim
# Non-root, read-only server: it never holds a key or wallet and never sends on-chain.
RUN useradd --create-home --uid 10001 ownerwatch
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER ownerwatch
# stdio transport (MCP default). Override timeouts via OWNERWATCH_MCP_TIMEOUT.
ENTRYPOINT ["ownerwatch-mcp"]
