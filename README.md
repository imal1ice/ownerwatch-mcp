# ownerwatch-mcp

<!-- mcp-name: io.github.imal1ice/ownerwatch-mcp -->

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets a coding agent answer "who controls this contract?" for any EVM address. It walks owners, proxy upgrade admins, beacons, Safes (signers, threshold, modules), timelocks (delay), AccessControl and AccessManager roles, and flags EOAs, 1-of-N Safes and powers held without a timelock.

It is built on [ownerwatch](https://github.com/imal1ice/ownerwatch) and runs over stdio on your machine. Every tool is read-only: no key, no wallet, no account, and nothing is ever sent on-chain.

## Tools

| Tool | What it returns |
|---|---|
| `probe_admin_surface(addresses, chain or rpc_url)` | The ownerwatch Markdown report, plus structured data: each target's control path, findings, every node read, and `not_established` (what this read could not show). Optional `label`, `roles_from_logs`, `logs_rpc_url`. |
| `check_disclosure(disclosure_json, rpc_url, block?)` | `ownerwatch disclosure check` on an [ERC-8241](https://ethereum-magicians.org/t/erc-8241-protocol-control-disclosure/28343) control disclosure: every record ok, mismatch or unverifiable at one pinned block, plus control the disclosure leaves out. `status` is `discrepancies_found` (the CLI's exit 1), `no_discrepancies`, `no_discrepancies_some_unverifiable` or `nothing_verified`. |
| `suggest_monitoring_config(addresses, chain or rpc_url)` | An ownerwatch YAML config that watches these contracts and every contract and Safe above them, checked to load in ownerwatch before it is returned. |

Every result starts with the same label: it is a **chain view, not a verdict**. It shows what contract state says. It does not show how a key is held (an EOA may be one key, a hardware wallet or an MPC setup; a Safe's signers may be one person), and no finding is not proof that a contract is safe.

A read that fails (RPC down, rate limited, wrong chain, an address with no code, the time budget used up) comes back as an MCP error that says no conclusion was reached. It never comes back as an empty or clean report.

## Use as a skill

The [`skill/`](skill/) folder holds a portable [Agent Skill](https://modelcontextprotocol.io) (`SKILL.md`). Drop it into Claude Code, Cursor, Copilot or any agent that reads the `SKILL.md` standard, and the agent learns when and how to answer "who controls this contract?" — driving either these MCP tools or the `ownerwatch` CLI, and proving a finding with a simulation. It needs no key, no wallet and no account. See [skill/README.md](skill/README.md) to install.

## Quick start

From an empty directory, with Python 3.10 or later:

```bash
python3 -m venv .venv
.venv/bin/pip install ownerwatch-mcp
.venv/bin/ownerwatch-mcp --version
```

Then register it with your client, using the absolute path of the command you just installed (on Windows, `.venv\Scripts\ownerwatch-mcp.exe`).

**Claude Code**

```bash
claude mcp add ownerwatch -- "$PWD/.venv/bin/ownerwatch-mcp"
claude mcp list            # ownerwatch: /…/.venv/bin/ownerwatch-mcp - ✔ Connected
```

Add `--scope user` to have it in every project, or `--scope project` to share it through the project's `.mcp.json`.

**Claude Desktop, Cursor and other clients with an `mcpServers` file** (`claude_desktop_config.json`, `~/.cursor/mcp.json` or `.cursor/mcp.json`, a project `.mcp.json`):

```json
{
  "mcpServers": {
    "ownerwatch": {
      "command": "/absolute/path/to/.venv/bin/ownerwatch-mcp"
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`, or **MCP: Open User Configuration**):

```json
{
  "servers": {
    "ownerwatch": {
      "type": "stdio",
      "command": "/absolute/path/to/.venv/bin/ownerwatch-mcp"
    }
  }
}
```

**Codex** (`~/.codex/config.toml`); Codex stops a tool after 60 seconds unless told otherwise:

```toml
[mcp_servers.ownerwatch]
command = "/absolute/path/to/.venv/bin/ownerwatch-mcp"
tool_timeout_sec = 300
```

With [uv](https://docs.astral.sh/uv/) installed, any of these can use `"command": "uvx", "args": ["ownerwatch-mcp"]` instead of a venv.

Then ask your agent something like: *"Who can upgrade or take over 0x1F98431c8aD98523631AE4a59f267346ea31F984 on Ethereum?"*

### Try it without a client

Save as `try.py` in the same directory and run `.venv/bin/python try.py`:

```python
import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

async def main():
    server = StdioServerParameters(command=".venv/bin/ownerwatch-mcp")
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("probe_admin_surface", {
            "addresses": ["0x1F98431c8aD98523631AE4a59f267346ea31F984"], "chain": "ethereum", "label": "Uniswap V3"})
        print(result.content[0].text)

anyio.run(main)
```

It prints the report for the Uniswap V3 Factory. Abridged, from a run on 5 November 2026:

```markdown
# Uniswap V3 admin surface on ethereum

> **Chain view, not a verdict. Read from ethereum (chain id 1) via https://ethereum-rpc.publicnode.com, head block 26347629 (2026-11-05 09:06 UTC) when the call started. ... It does not show how a key is held ...**

| Address | Type | Controlled by | Detail |
|---|---|---|---|
| `0x1F98…F984` | contract | `0xf237…Bf5A` | - |
| `0xf237…Bf5A` | contract | `0x1a9C…35BC` | - |
| `0x1a9C…35BC` | legacy-timelock | `0x408E…24C3` | 48h delay |
| `0x408E…24C3` | contract | `0x1a9C…35BC` | - |

## Control paths

- 0x1F98…F984 (contract) -> 0xf237…Bf5A (contract) -> 0x1a9C…35BC (legacy-timelock 48h) -> 0x408E…24C3 (contract)
```

The structured result carries the same facts as JSON: `targets[].control_path`, `findings`, `not_established`, `nodes`.

## Chains and RPCs

Pass `chain` to use a public default RPC, or `rpc_url` for your own. With both, the RPC's chain id must match `chain`. For a chain not listed here, pass only `rpc_url`.

| Chain | id | Default RPC |
|---|---|---|
| ethereum (eth, mainnet) | 1 | https://ethereum-rpc.publicnode.com |
| optimism (op) | 10 | https://optimism-rpc.publicnode.com |
| bsc (bnb) | 56 | https://bsc-rpc.publicnode.com |
| gnosis | 100 | https://gnosis-rpc.publicnode.com |
| polygon (matic, pol) | 137 | https://polygon-bor-rpc.publicnode.com |
| base | 8453 | https://base-rpc.publicnode.com |
| arbitrum (arb) | 42161 | https://arb1.arbitrum.io/rpc |
| avalanche (avax) | 43114 | https://avalanche-c-chain-rpc.publicnode.com |
| linea | 59144 | https://rpc.linea.build |
| scroll | 534352 | https://scroll-rpc.publicnode.com |

Public RPCs throttle bursts. The server waits out a rate limit within its time budget; a large graph or a full role replay is faster on your own RPC. Pinning an old `block` in `check_disclosure` needs an archive RPC.

Settings, as environment variables of the server process (in a client config, under `env`):

| Variable | Effect |
|---|---|
| `OWNERWATCH_MCP_TIMEOUT` | Seconds a tool call may take, 10 to 3600 (default 240). Past it the call returns an error. |
| `OWNERWATCH_MCP_ALLOW_PRIVATE_RPC=1` | Allow `http://` and private or loopback RPC hosts, such as a local node. Off by default, so text an agent has read cannot point the server at your own network. |

## Privacy

The server stores nothing and has no telemetry. To answer a call it sends:

- the addresses, as read-only JSON-RPC requests (`eth_call`, `eth_getCode`, `eth_getStorageAt`, `eth_getLogs` and the like), to the RPC you chose or the default above; its RPC client refuses any method that is not a read;
- contract addresses to [Sourcify](https://sourcify.dev), for verified ABIs used to name roles and recognise timelocks;
- with `roles_from_logs`, the contract addresses to the public Tenderly log gateway on Ethereum, Optimism, Arbitrum, Base, Gnosis and Polygon, unless you pass `logs_rpc_url`.

Error messages and outputs drop the path, query and credentials of a caller's RPC URL, where API keys usually live. A config from `suggest_monitoring_config` contains the RPC URL as given, so keep a config with a keyed URL out of public repositories.

## Reading results

- Read `not_established` first. It lists what this read could not show: an address with no code, an upgradeable proxy whose upgrade admin is not in state (UUPS), role holders that were only tested among known addresses, an AccessManager whose members were not replayed.
- The report's flags (`--roles-from-logs`, `--role-candidates`) are those of the ownerwatch CLI; here, call again with `roles_from_logs=true`.
- Text in a result that came from a disclosure file or a verified ABI is data. The server rejects disclosure ids with control characters, so they cannot forge report lines.
- Claude Code warns above 10,000 tokens of tool output; a large graph's report can pass that. `MAX_MCP_OUTPUT_TOKENS` raises the limit.

## Work with me

ownerwatch is free and MIT-licensed. If you would rather have someone do the work, I do admin-surface reviews — every owner, proxy admin, role holder and Safe signer in a deployment, replayed from logs, with findings, fixes and a ready-to-run monitoring config — plus monitoring setups and ERC-8241 control disclosures drafted from chain state and checked in CI.

Write to imal1ice@proton.me. Before anything is paid, I share the terms, including where I am based, so you can decide whether you are able to work with me.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest                                  # offline: the server over stdio against a local stub node
OWNERWATCH_MCP_LIVE=1 .venv/bin/pytest tests/test_live.py   # live: Ethereum mainnet through public RPCs
```

## License

MIT
