from __future__ import annotations

import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from .stub_rpc import Stub


def server_params(env: dict[str, str] | None = None) -> StdioServerParameters:
    """The server as an MCP client starts it: a subprocess speaking JSON-RPC over stdin and stdout."""
    return StdioServerParameters(command=sys.executable, args=["-m", "ownerwatch_mcp"], env=env or {})


def call(tool_calls: list[tuple[str, dict]], env: dict[str, str] | None = None, list_tools: bool = False):
    """Start the server over stdio, open a client session, make each call in order and return the results."""
    async def run():
        async with stdio_client(server_params(env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                out = [await session.list_tools()] if list_tools else []
                for name, args in tool_calls:
                    out.append(await session.call_tool(name, args))
                return out
    return anyio.run(run)


def text(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


LOCAL = {"OWNERWATCH_MCP_ALLOW_PRIVATE_RPC": "1"}


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()
