"""Chains with a public default RPC. Names match ownerwatch's chain names, so a suggested config lines up with
ownerwatch's Safe Transaction Service keys. Every default answered eth_chainId, eth_call, eth_getCode and an
address-scoped eth_getLogs on 2026-11-05, except Polygon's, which is publicnode's documented endpoint and did not
resolve from the machine that built this list. A caller can always pass its own rpc_url."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    name: str
    chain_id: int
    default_rpc: str
    aliases: tuple[str, ...] = ()


CHAINS = (
    Chain("ethereum", 1, "https://ethereum-rpc.publicnode.com", ("eth", "mainnet", "ethereum-mainnet")),
    Chain("optimism", 10, "https://optimism-rpc.publicnode.com", ("op", "oeth", "op-mainnet")),
    Chain("bsc", 56, "https://bsc-rpc.publicnode.com", ("bnb", "binance", "bnb-smart-chain")),
    Chain("gnosis", 100, "https://gnosis-rpc.publicnode.com", ("xdai", "gno")),
    Chain("polygon", 137, "https://polygon-bor-rpc.publicnode.com", ("matic", "pol", "polygon-pos")),
    Chain("base", 8453, "https://base-rpc.publicnode.com", ("base-mainnet",)),
    Chain("arbitrum", 42161, "https://arb1.arbitrum.io/rpc", ("arb", "arb1", "arbitrum-one")),
    Chain("avalanche", 43114, "https://avalanche-c-chain-rpc.publicnode.com", ("avax", "avalanche-c")),
    Chain("linea", 59144, "https://rpc.linea.build", ()),
    Chain("scroll", 534352, "https://scroll-rpc.publicnode.com", ("scr",)),
)
BY_ID = {c.chain_id: c for c in CHAINS}
_BY_KEY = {k: c for c in CHAINS for k in (c.name, *c.aliases, str(c.chain_id))}


def find(key: str) -> Chain | None:
    """A chain by name, alias or decimal chain id ("ethereum", "eth", "1")."""
    return _BY_KEY.get(key.strip().lower())


def names() -> str:
    return ", ".join(f"{c.name} ({c.chain_id})" for c in CHAINS)
