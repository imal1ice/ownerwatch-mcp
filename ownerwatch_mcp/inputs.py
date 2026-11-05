"""Input checks. Everything a tool receives comes from a model that may have read untrusted text, so addresses,
URLs, labels and disclosure files are checked before any request leaves the machine."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlsplit

from web3 import Web3

from . import chains

MAX_ADDRESSES = 25
MAX_DISCLOSURE_BYTES = 2_000_000
ALLOW_PRIVATE_ENV = "OWNERWATCH_MCP_ALLOW_PRIVATE_RPC"
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_DIGITS = re.compile(r"[0-9]{1,20}")
_LABEL_DROP = re.compile(r"[^\w .,&()'/+-]")


class InputError(ValueError):
    """The request itself is wrong; nothing was read from the chain."""


def address(value: object, what: str = "address") -> str:
    """A checksummed address. Mixed case must carry a valid EIP-55 checksum: a wrong one is usually a typo."""
    if not isinstance(value, str) or not _HEX_ADDRESS.fullmatch(value.strip()):
        raise InputError(f"{what} {str(value)[:80]!r} is not a 0x-prefixed 20-byte hex address (ENS names are not resolved)")
    v = value.strip()
    if v != v.lower() and v[2:] != v[2:].upper() and not Web3.is_checksum_address(v):
        raise InputError(f"{what} {v} has an invalid EIP-55 checksum: check it for a typo, or pass it in lower case")
    return Web3.to_checksum_address(v)


def addresses(values: object) -> list[str]:
    if not isinstance(values, list) or not values:
        raise InputError("addresses must be a non-empty list of 0x addresses")
    if len(values) > MAX_ADDRESSES:
        raise InputError(f"at most {MAX_ADDRESSES} addresses per call; split the list")
    return list(dict.fromkeys(address(v) for v in values))


def private_allowed() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


def rpc_url(value: object, what: str = "rpc_url") -> str:
    """An RPC URL this server may send requests to: https to a public host. http, loopback and private hosts
    (a local node) need OWNERWATCH_MCP_ALLOW_PRIVATE_RPC=1 in the server's environment, which a tool call cannot set,
    so a prompt cannot point the server at the user's own network or a cloud metadata endpoint."""
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{what} must be a URL string")
    url = value.strip()
    if len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise InputError(f"{what} is not a valid URL")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as e:
        raise InputError(f"{what} is not a valid URL: {e}") from None
    if parts.scheme not in ("https", "http") or not host:
        raise InputError(f"{what} must be an https:// URL with a host (ws://, file:// and others are not accepted)")
    allow = private_allowed()
    if parts.scheme == "http" and not allow:
        raise InputError(f"{what} must use https://. Plain http:// is accepted only when the server runs with {ALLOW_PRIVATE_ENV}=1")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        raise InputError(f"{what}: the host {host} does not resolve") from None
    if not allow:
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not ip.is_global:
                raise InputError(f"{what}: {host} resolves to a non-public address ({ip}). A local or private node needs "
                                 f"{ALLOW_PRIVATE_ENV}=1 in the server's environment")
    return url


def chain_and_rpc(chain: str | int | None, rpc: str | None) -> tuple[chains.Chain | None, str, bool]:
    """(expected chain or None, RPC URL, whether the URL is this server's default)."""
    want = None
    if chain is not None and str(chain).strip():
        want = chains.find(str(chain))
        if want is None:
            raise InputError(f"unknown chain {str(chain)[:40]!r}. Known: {chains.names()}. For any other EVM chain pass rpc_url")
    if rpc is not None and str(rpc).strip():
        return want, rpc_url(rpc), False
    if want is None:
        raise InputError(f"pass chain (one of: {chains.names()}) or rpc_url")
    return want, want.default_rpc, True


def label(value: object) -> str:
    """A protocol name for headings and config labels: printable, no Markdown control characters, 60 chars."""
    text = " ".join(_LABEL_DROP.sub("", " ".join(str(value or "").split())).split())[:60].strip()
    return text or "Protocol"


def block(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError("block must be a non-negative block number")
    return value


def _str(obj: dict, key: str, where: str, required: bool = False) -> None:
    v = obj.get(key)
    if v is None and not required:
        return
    if not isinstance(v, str) or not v:
        raise InputError(f"{where}: {key} must be a non-empty string")
    if len(v) > 300 or not v.isprintable():
        # these strings are quoted in the report: a newline in an id could forge a line of it
        raise InputError(f"{where}: {key} must be printable text of at most 300 characters")


def disclosure(value: object) -> dict:
    """An ERC-8241 disclosure (JSON text or an object) with the structure ownerwatch's checker reads. A malformed file
    is rejected here, so a later failure can only mean the chain could not be read."""
    if isinstance(value, str):
        if len(value.encode()) > MAX_DISCLOSURE_BYTES:
            raise InputError(f"disclosure_json is larger than {MAX_DISCLOSURE_BYTES // 1_000_000} MB")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise InputError(f"disclosure_json is not valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise InputError("disclosure_json must be a JSON object (an ERC-8241 disclosure)")
    d = value
    proto = d.get("protocol", {})
    if not isinstance(proto, dict):
        raise InputError("protocol must be an object")
    _str(proto, "manifestHash", "protocol")
    cid = proto.get("chainId")
    if cid is not None and (isinstance(cid, bool) or not isinstance(cid, int)):
        raise InputError("protocol.chainId must be an integer")
    lists = {}
    for key in ("nodes", "components", "powers", "edges"):
        items = d.get(key, [])
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise InputError(f"{key} must be a list of objects")
        lists[key] = items
    if not any(lists.values()):
        raise InputError("the disclosure has no nodes, components, powers or edges: nothing to check")
    for key, idk in (("nodes", "nodeId"), ("components", "componentId"), ("powers", "powerId"), ("edges", "edgeId")):
        seen: set[str] = set()
        for i, rec in enumerate(lists[key]):
            where = f"{key}[{i}]"
            _str(rec, idk, where, required=True)
            if rec[idk] in seen:
                raise InputError(f"{where}: duplicate {idk} {rec[idk][:80]!r}")
            seen.add(rec[idk])
    for i, n in enumerate(lists["nodes"]):
        _str(n, "type", f"nodes[{i}]")
        if n.get("address") is not None:
            address(n["address"], f"nodes[{i}].address")
    for i, c in enumerate(lists["components"]):
        address(c.get("address"), f"components[{i}].address")
        if c.get("implementation"):
            address(c["implementation"], f"components[{i}].implementation")
        _str(c, "adminNodeId", f"components[{i}]")
    for i, p in enumerate(lists["powers"]):
        where = f"powers[{i}]"
        for k in ("kind", "target", "controllerNodeId", "role"):
            _str(p, k, where)
        if p.get("roleHash") is not None and not (isinstance(p["roleHash"], str) and _HASH.fullmatch(p["roleHash"])):
            raise InputError(f"{where}: roleHash must be 0x followed by 64 hex digits")
        role = p.get("role") or ""
        for prefix in ("accessManager:", "role "):
            rest = role[len(prefix):]
            # read as an AccessManager role id: anything that is not a plain decimal below 2^64 would crash the check
            if role.startswith(prefix) and (prefix == "accessManager:" or rest.isdigit()):
                if not _DIGITS.fullmatch(rest) or int(rest) >= 2**64:
                    raise InputError(f"{where}: role {role[:80]!r} needs a decimal role id below 2^64 after {prefix!r}")
    for i, e in enumerate(lists["edges"]):
        for k in ("kind", "from", "to"):
            _str(e, k, f"edges[{i}]")
    return d
