"""The server started over stdio by an MCP client session, against a local stub node: the tool surface, a clean read,
and every way a read can fail. A failure must come back as an error and never as a report that reads clean."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from ownerwatch.config import load_all

from .conftest import LOCAL, call, text
from .stub_rpc import CONTRACT, OTHER, OWNER_EOA, Stub

CLEAN_WORDS = ("No single-key", "no_discrepancies")


def probe_args(url: str, **kw) -> dict:
    return {"addresses": [CONTRACT], "rpc_url": url, **kw}


def assert_failed_read(res, *must_contain: str) -> None:
    body = text(res)
    assert res.is_error, body
    assert res.structured_content is None
    assert "No result" in body or "Nothing was read" in body, body
    for word in CLEAN_WORDS:
        assert word not in body
    for m in must_contain:
        assert m in body, body


def test_tool_surface():
    (tools,) = call([], list_tools=True)
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == {"probe_admin_surface", "check_disclosure", "suggest_monitoring_config"}
    for t in by_name.values():
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.output_schema and "notice" in t.output_schema["properties"]
        assert "chain view" in t.description.lower() or "read-only" in t.description.lower()
    assert by_name["check_disclosure"].input_schema["required"] == ["disclosure_json", "rpc_url"]
    assert by_name["probe_admin_surface"].input_schema["required"] == ["addresses"]


def test_probe_reads_the_owner_and_labels_the_view(stub):
    (res,) = call([("probe_admin_surface", probe_args(stub.url, label="Stub", chain=1))], env=LOCAL)   # a numeric chain id too
    assert not res.is_error, text(res)
    data = res.structured_content
    assert data["chain"] == {"name": "ethereum", "chain_id": 1}
    assert data["targets"][0]["owner"] == OWNER_EOA
    assert [h["address"] for h in data["targets"][0]["control_path"]] == [CONTRACT, OWNER_EOA]
    assert any("is an EOA" in f["finding"] for f in data["findings"])
    for body in (text(res), data["report_markdown"], data["notice"]):
        assert "does not show how a key is held" in body and "not a verdict" in body
    assert text(res).startswith("# Stub admin surface on ethereum")
    # read requests only, and nothing but JSON-RPC reads
    assert stub.methods and all(m == "eth_call" or m.startswith("eth_get") or m in ("eth_chainId", "eth_blockNumber")
                                for m in stub.methods)


def test_contract_without_controller_is_not_reported_clean(stub):
    stub.mode = "ok"
    other = "0x" + "44" * 20
    (res,) = call([("probe_admin_surface", {"addresses": [CONTRACT, other], "rpc_url": stub.url})], env=LOCAL)
    gaps = res.structured_content["not_established"]
    assert any(other.lower() in g.lower() and "no contract code" in g for g in gaps)
    assert "Not established by this read" in text(res)


def test_failed_reads_are_errors():
    for mode, expect in (("rate_limited", "rate limit"), ("http500", "500"), ("html", "Could not read"),
                         ("redirect", "Could not read")):
        s = Stub(mode)
        try:
            env = {**LOCAL, "OWNERWATCH_MCP_TIMEOUT": "12"}
            res_probe, res_cfg = call([("probe_admin_surface", probe_args(s.url)),
                                       ("suggest_monitoring_config", probe_args(s.url))], env=env)
            assert_failed_read(res_probe, expect)
            assert_failed_read(res_cfg)
            assert not any(m.startswith("eth_send") for m in s.methods)
        finally:
            s.close()


def test_dead_rpc_is_an_error():
    s = Stub()
    url = s.url
    s.close()
    (res,) = call([("probe_admin_surface", probe_args(url))], env=LOCAL)
    assert_failed_read(res, "Could not read chain state")


def test_wrong_chain_and_no_code_are_errors():
    s = Stub("wrong_chain")
    try:
        (res,) = call([("probe_admin_surface", probe_args(s.url, chain="ethereum"))], env=LOCAL)
        assert_failed_read(res, "chain id 5", "not ethereum")
        s.mode = "no_code"
        (res,) = call([("probe_admin_surface", probe_args(s.url))], env=LOCAL)
        assert_failed_read(res, "none of these addresses has contract code")
    finally:
        s.close()


def test_private_rpc_needs_the_operator_opt_in(stub):
    (res,) = call([("probe_admin_surface", probe_args(stub.url))])     # no OWNERWATCH_MCP_ALLOW_PRIVATE_RPC
    assert_failed_read(res, "Invalid request")
    assert stub.methods == []          # nothing reached the private address


def test_secret_in_rpc_url_stays_out_of_errors():
    s = Stub("http500")
    try:
        url = s.url + "/v2/SUPERSECRETKEY123?token=QUERYSECRET456"
        (res,) = call([("probe_admin_surface", probe_args(url))], env=LOCAL)
        assert_failed_read(res)
        assert "SUPERSECRETKEY123" not in text(res) and "QUERYSECRET456" not in text(res)
    finally:
        s.close()


def test_time_budget_is_an_error():
    s = Stub("slow")
    try:
        (res,) = call([("probe_admin_surface", probe_args(s.url))], env={**LOCAL, "OWNERWATCH_MCP_TIMEOUT": "10"})
        assert_failed_read(res)
        assert "time budget" in text(res) or "within 10 seconds" in text(res)
    finally:
        s.close()


def test_suggest_config_loads_in_ownerwatch(stub):
    (res,) = call([("suggest_monitoring_config", probe_args(stub.url, label="Stub"))], env=LOCAL)
    assert not res.is_error, text(res)
    cfg_text = res.structured_content["config_yaml"]
    assert cfg_text == text(res) and cfg_text.startswith("# ownerwatch config")
    assert "does not show how a key is held" in cfg_text
    cfg = yaml.safe_load(cfg_text)
    assert cfg["contracts"] == [{"address": CONTRACT, "label": "Stub Contract"}] and cfg["rpc_url"] == stub.url
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.yaml").write_text(cfg_text)
        assert load_all(Path(tmp) / "config.yaml")[0].contracts[0].address == CONTRACT


def disclosure(owner: str | None) -> dict:
    nodes = [{"nodeId": "contract-c", "type": "CONTRACT", "address": CONTRACT}]
    powers = []
    if owner:
        nodes.append({"nodeId": "owner", "type": "EOA", "address": owner})
        powers.append({"powerId": "own", "kind": "OWNERSHIP", "enabled": True, "target": "c", "controllerNodeId": "owner"})
    return {"standard": "ERC-8241", "protocol": {"name": "Stub", "chainId": 1}, "nonce": 1, "nodes": nodes,
            "components": [{"componentId": "c", "address": CONTRACT, "nodeId": "contract-c", "upgradeable": False}],
            "powers": powers, "edges": []}


def test_check_disclosure(stub):
    good, wrong, omitted = disclosure(OWNER_EOA), disclosure(OTHER), disclosure(None)
    args = {"rpc_url": stub.url, "roles_from_logs": False}
    r_good, r_text, r_wrong, r_omit, r_bad, r_future = call([
        ("check_disclosure", {"disclosure_json": good, **args}),
        ("check_disclosure", {"disclosure_json": json.dumps(good), **args, "block": 1000}),
        ("check_disclosure", {"disclosure_json": wrong, **args}),
        ("check_disclosure", {"disclosure_json": omitted, **args}),
        ("check_disclosure", {"disclosure_json": "{\"nodes\": 3}", **args}),
        ("check_disclosure", {"disclosure_json": good, **args, "block": 10**9}),
    ], env=LOCAL)
    assert not r_good.is_error, text(r_good)
    d = r_good.structured_content
    assert d["status"] == "no_discrepancies" and d["exit_code_equivalent"] == 0 and d["summary"]["mismatch"] == 0
    assert "Chain view, not a verdict" in text(r_good) and "data" in d["notice"]
    assert r_text.structured_content["block"] == 1000
    assert r_wrong.structured_content["status"] == "discrepancies_found"
    assert r_wrong.structured_content["exit_code_equivalent"] == 1
    assert r_omit.structured_content["status"] == "discrepancies_found"
    assert r_omit.structured_content["undisclosed"][0]["holder"] == OWNER_EOA
    assert_failed_read(r_bad, "Invalid request")
    assert_failed_read(r_future, "after the RPC's head")


def test_check_disclosure_failed_read_is_an_error():
    s = Stub("rate_limited")
    try:
        (res,) = call([("check_disclosure", {"disclosure_json": disclosure(OWNER_EOA), "rpc_url": s.url,
                                             "roles_from_logs": False})], env={**LOCAL, "OWNERWATCH_MCP_TIMEOUT": "12"})
        assert_failed_read(res, "Could not read chain state")
    finally:
        s.close()


def test_stdout_carries_only_protocol_messages(stub):
    """A stray print on stdout would corrupt the stdio transport: every line the server writes must be JSON-RPC."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "probe_admin_surface", "arguments": probe_args(stub.url)}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "probe_admin_surface", "arguments": {"addresses": ["0x12"], "rpc_url": stub.url}}},
    ]
    proc = subprocess.Popen([sys.executable, "-m", "ownerwatch_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, **LOCAL})
    replies = {}
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
            if "id" in m:
                while True:
                    line = proc.stdout.readline()
                    assert line, proc.stderr.read()
                    msg = json.loads(line)              # raises on anything that is not JSON
                    assert msg.get("jsonrpc") == "2.0"
                    if msg.get("id") == m["id"]:
                        replies[m["id"]] = msg
                        break
    finally:
        proc.stdin.close()
        proc.wait(timeout=20)
    ok, refused = replies[2]["result"], replies[3]["result"]
    assert ok.get("isError") is not True and ok["structuredContent"]["targets"][0]["owner"] == OWNER_EOA
    assert refused["isError"] is True and "structuredContent" not in refused
