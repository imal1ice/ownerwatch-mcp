import json

import pytest

from ownerwatch_mcp import chains, inputs
from ownerwatch_mcp.inputs import InputError
from ownerwatch_mcp.reads import scrub, shown

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def test_address_checks():
    assert inputs.address(USDC.lower()) == USDC
    assert inputs.address(USDC) == USDC
    for bad in ("0x123", "A0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "usdc.eth", 7, None, USDC + "00"):
        with pytest.raises(InputError):
            inputs.address(bad)
    with pytest.raises(InputError, match="checksum"):
        inputs.address(USDC.replace("eB48", "eb48"))     # one letter's case flipped: a typo, not an address to read
    assert inputs.addresses([USDC, USDC.lower()]) == [USDC]
    with pytest.raises(InputError):
        inputs.addresses([])
    with pytest.raises(InputError):
        inputs.addresses([USDC] * (inputs.MAX_ADDRESSES + 1))


@pytest.mark.parametrize("url", ["https://127.0.0.1", "https://169.254.169.254/latest", "https://10.1.2.3:8545",
                                 "https://[::1]/", "https://localhost:8545", "http://1.1.1.1", "ws://1.1.1.1",
                                 "file:///etc/passwd", "https://", "not a url", "https://1.1.1.1/a b"])
def test_rpc_url_refused(url, monkeypatch):
    monkeypatch.delenv(inputs.ALLOW_PRIVATE_ENV, raising=False)
    with pytest.raises(InputError):
        inputs.rpc_url(url)


def test_rpc_url_public_and_private_opt_in(monkeypatch):
    monkeypatch.delenv(inputs.ALLOW_PRIVATE_ENV, raising=False)
    assert inputs.rpc_url("https://1.1.1.1/v2/key") == "https://1.1.1.1/v2/key"
    monkeypatch.setenv(inputs.ALLOW_PRIVATE_ENV, "1")
    assert inputs.rpc_url("http://127.0.0.1:8545") == "http://127.0.0.1:8545"


def test_chain_resolution():
    assert chains.find("ETH").chain_id == 1 and chains.find("8453").name == "base" and chains.find("arb").chain_id == 42161
    want, rpc, default = inputs.chain_and_rpc("ethereum", None)
    assert (want.chain_id, rpc, default) == (1, "https://ethereum-rpc.publicnode.com", True)
    want, rpc, default = inputs.chain_and_rpc("base", "https://1.1.1.1")
    assert (want.name, rpc, default) == ("base", "https://1.1.1.1", False)
    with pytest.raises(InputError, match="pass chain"):
        inputs.chain_and_rpc(None, None)
    with pytest.raises(InputError, match="unknown chain"):
        inputs.chain_and_rpc("solana", None)
    # every default is https and names a chain ownerwatch knows by the same name
    from ownerwatch.probe import chain_name_for
    for c in chains.CHAINS:
        assert c.default_rpc.startswith("https://") and chain_name_for(c.chain_id) == c.name


def test_label_is_plain_text():
    assert inputs.label("Acme | <b>x</b>\n# `rm`") == "Acme bx/b rm"
    assert inputs.label("") == "Protocol" and len(inputs.label("x" * 500)) == 60


def test_secrets_stay_out_of_output():
    url = "https://user:pa55word@rpc.example.com/v2/SUPERSECRETKEY?apikey=QUERYSECRET"
    assert shown(url) == "https://rpc.example.com/…"
    msg = f"401 Client Error for url: {url} (key SUPERSECRETKEY rejected, QUERYSECRET)"
    out = scrub(msg, url)
    assert "SUPERSECRETKEY" not in out and "QUERYSECRET" not in out and "pa55word" not in out


GOOD = {"protocol": {"name": "x", "chainId": 1},
        "nodes": [{"nodeId": "n1", "type": "EOA", "address": "0x2222222222222222222222222222222222222222"}],
        "components": [{"componentId": "c1", "address": "0x1111111111111111111111111111111111111111"}],
        "powers": [{"powerId": "p1", "kind": "OWNERSHIP", "target": "c1", "controllerNodeId": "n1"}], "edges": []}


def test_disclosure_shapes():
    assert inputs.disclosure(json.dumps(GOOD)) == GOOD
    assert inputs.disclosure(GOOD) == GOOD
    bad_cases = [
        "{not json", "[]", {}, {"nodes": "x"}, {"protocol": [], "nodes": [{"nodeId": "a"}]},
        {**GOOD, "nodes": GOOD["nodes"] * 2},                                         # duplicate id
        {**GOOD, "components": [{"componentId": "c1", "address": "0x12"}]},
        {**GOOD, "components": [{"address": "0x1111111111111111111111111111111111111111"}]},
        {**GOOD, "powers": [{"powerId": "p\nok 99 records fine", "kind": "ROLE"}]},  # a forged report line
        {**GOOD, "powers": [{"powerId": "p", "kind": "ROLE", "role": "accessManager:x"}]},
        {**GOOD, "powers": [{"powerId": "p", "kind": "ROLE", "role": "role 99999999999999999999999"}]},
        {**GOOD, "powers": [{"powerId": "p", "kind": "ROLE", "roleHash": "0x12"}]},
        {**GOOD, "protocol": {"chainId": "1"}},
    ]
    for case in bad_cases:
        with pytest.raises(InputError):
            inputs.disclosure(case)
    ok_role_name = {**GOOD, "powers": [{"powerId": "p", "kind": "ROLE", "role": "role admin"}]}
    assert inputs.disclosure(ok_role_name)
