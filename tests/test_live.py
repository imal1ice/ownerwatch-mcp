"""Live reads of Ethereum mainnet through public RPCs, over stdio. Run with OWNERWATCH_MCP_LIVE=1.

Target: the Uniswap V3 Factory, whose owner() leads to the Uniswap governance Timelock (2-day delay)."""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from ownerwatch.config import load_all
from ownerwatch.disclosure import draft
from ownerwatch.probe import make_w3

from ownerwatch_mcp import chains

from .conftest import call, text

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("OWNERWATCH_MCP_LIVE") != "1",
                                                   reason="set OWNERWATCH_MCP_LIVE=1 to read public mainnet RPCs")]
PUBLICNODE = "https://ethereum-rpc.publicnode.com"
FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
TIMELOCK = "0x1a9C8182C09F50C8318d769245beA52c32BE35BC"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def test_probe_and_config_on_mainnet():
    by_chain, by_url, cfg, wrong = call([
        ("probe_admin_surface", {"addresses": [FACTORY], "chain": "ethereum", "label": "Uniswap V3"}),
        ("probe_admin_surface", {"addresses": [FACTORY], "rpc_url": PUBLICNODE}),
        ("suggest_monitoring_config", {"addresses": [FACTORY], "chain": "eth", "label": "Uniswap V3"}),
        ("probe_admin_surface", {"addresses": [FACTORY], "chain": "base", "rpc_url": PUBLICNODE}),
    ])
    for res in (by_chain, by_url):
        assert not res.is_error, text(res)
        d = res.structured_content
        assert d["chain"]["chain_id"] == 1 and d["rpc"] == PUBLICNODE
        path = d["targets"][0]["control_path"]
        assert path[0]["address"] == FACTORY
        assert {"address": TIMELOCK, "kind": "legacy-timelock", "delay_seconds": 172800} in path
        assert "does not show how a key is held" in text(res)
    assert not cfg.is_error, text(cfg)
    body = cfg.structured_content["config_yaml"]
    parsed = yaml.safe_load(body)
    assert parsed["rpc_url"] == PUBLICNODE and parsed["chain_name"] == "ethereum"
    assert TIMELOCK in [c["address"] for c in parsed["contracts"]]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.yaml").write_text(body)
        assert load_all(Path(tmp) / "config.yaml")
    assert wrong.is_error and "not base" in text(wrong)


def test_check_disclosure_on_mainnet():
    w3 = make_w3(PUBLICNODE)
    block = w3.eth.block_number - 5
    d = draft(w3, [FACTORY], "Uniswap V3", block)
    tampered = {**d, "powers": [{**p, "controllerNodeId": d["nodes"][0]["nodeId"]} if p["kind"] == "OWNERSHIP" else p
                                for p in d["powers"]]}
    tampered["protocol"] = {k: v for k, v in d["protocol"].items() if k != "manifestHash"}
    clean, broken = call([
        ("check_disclosure", {"disclosure_json": d, "rpc_url": PUBLICNODE, "block": block}),
        ("check_disclosure", {"disclosure_json": tampered, "rpc_url": PUBLICNODE, "block": block}),
    ])
    assert not clean.is_error, text(clean)
    c = clean.structured_content
    assert c["block"] == block and c["chainId"] == 1
    assert c["status"] in ("no_discrepancies", "no_discrepancies_some_unverifiable"), text(clean)
    assert c["summary"]["ok"] > 0 and c["summary"]["mismatch"] == 0 and c["summary"]["undisclosed"] == 0
    assert not broken.is_error, text(broken)
    b = broken.structured_content
    assert b["status"] == "discrepancies_found" and b["exit_code_equivalent"] == 1
    assert b["summary"]["mismatch"] >= 1 and b["summary"]["undisclosed"] >= 1


@pytest.mark.parametrize("chain", chains.CHAINS, ids=lambda c: c.name)
def test_default_rpc_per_chain(chain):
    host = urlsplit(chain.default_rpc).hostname
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        pytest.skip(f"{host} does not resolve from this machine")
    (res,) = call([("probe_admin_surface", {"addresses": [MULTICALL3], "chain": chain.name})])
    assert not res.is_error, text(res)
    d = res.structured_content
    assert d["chain"] == {"name": chain.name, "chain_id": chain.chain_id}
    # Multicall3 has no controller: that must show up as not established, not as a clean bill
    assert any("no owner, admin" in g for g in d["not_established"])
