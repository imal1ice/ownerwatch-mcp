"""Chain reads through ownerwatch, the same way `ownerwatch probe` and `ownerwatch disclosure check` make them.

Every function here is synchronous and runs on a worker thread. It returns plain data, or raises InputError (the
request is wrong), ReadError (the chain could not be read: no conclusion) or lets a bug surface as itself.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
import yaml
from ownerwatch import __version__ as OWNERWATCH_VERSION
from ownerwatch.alerts import redact
from ownerwatch.config import SAFE_CHAIN_KEYS, load_all
from ownerwatch.disclosure import Checker, render
from ownerwatch.probe import ProbeRpcError, Prober, chain_name_for, markdown_report, suggest_config
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.middleware import ExtraDataToPOAMiddleware
from web3.providers.rpc import HTTPProvider
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from . import __version__, chains
from .inputs import InputError

VERSIONS = f"ownerwatch-mcp {__version__}, ownerwatch {OWNERWATCH_VERSION}"
_READ_METHODS = {"eth_call", "eth_chainId", "eth_blockNumber", "net_version"}
_RATE_LIMITED = re.compile(r"rate.?limit|too many requests|request rate|\b429\b", re.I)
NO_VERDICT = ("No finding is not a verdict that a contract is safe: this view cannot show how a key is held, "
              "what happens off-chain, or control it did not read.")


class ReadError(RuntimeError):
    """The chain could not be read. Nothing was concluded."""


class BudgetExceeded(ReadError):
    pass


def _is_read(method: str) -> bool:
    return method in _READ_METHODS or method.startswith("eth_get")


class ReadOnlyProvider(HTTPProvider):
    """An HTTP provider that sends only read methods, stops at a deadline and does not follow redirects
    (a public URL must not bounce a request into a private network)."""

    def __init__(self, url: str, deadline: float, timeout: int = 20):
        # two tries on a connection failure, not web3's five: a dead host should fail in under a minute
        super().__init__(url, request_kwargs={"timeout": timeout, "allow_redirects": False},
                         exception_retry_configuration=ExceptionRetryConfiguration(
                             errors=(requests.ConnectionError, requests.HTTPError, requests.Timeout), retries=2, backoff_factor=0.5))
        self._deadline = deadline
        self._chain_id = None

    def _gate(self, method: str) -> None:
        if not _is_read(method):
            raise PermissionError(f"ownerwatch-mcp sends read requests only and refused {method}")
        if time.monotonic() > self._deadline:
            raise BudgetExceeded("the time budget for this call ran out before every read was made")

    def make_request(self, method, params):
        """A rate-limited request (HTTP 429, or a JSON-RPC error that says so) waits and retries until the deadline:
        public RPCs throttle bursts, and the walk sends many small reads. The chain id is asked once: web3 asks
        for it before every eth_call, which doubles the requests a public RPC counts against its limit."""
        self._gate(method)
        if method == "eth_chainId" and self._chain_id is not None:
            return {"jsonrpc": "2.0", "id": 0, "result": self._chain_id}
        wait = 1.0
        while True:
            failure = None
            try:
                response = super().make_request(method, params)
                error = response.get("error") if isinstance(response, dict) else None
                message = str(error.get("message", "")) if isinstance(error, dict) else ""
                # a contract's own revert reason can say "rate limit" too: that is an answer, not a throttle
                if not message or "revert" in message.lower() or not _RATE_LIMITED.search(message):
                    if method == "eth_chainId" and isinstance(response, dict) and "result" in response:
                        self._chain_id = response["result"]
                    return response
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 429:
                    raise
                failure, response = e, None
            if time.monotonic() + wait > self._deadline:
                if failure is not None:
                    raise failure
                return response
            time.sleep(wait)
            wait = min(wait * 2, 8.0)
            self._gate(method)

    def make_batch_request(self, batch_requests):
        for method, _ in batch_requests:
            self._gate(method)
        return super().make_batch_request(batch_requests)


def connect(url: str, deadline: float, timeout: int = 20) -> Web3:
    w3 = Web3(ReadOnlyProvider(url, deadline, timeout))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)   # PoA chains (BSC, Polygon), as ownerwatch's make_w3
    return w3


def shown(url: str, is_default: bool = False) -> str:
    """An RPC URL as it may appear in output: a caller's URL loses its path, query and credentials (API keys live there)."""
    if is_default:
        return url
    p = urlsplit(url)
    host = p.hostname or "?"
    netloc = f"[{host}]" if ":" in host else host
    if p.port:
        netloc += f":{p.port}"
    hidden = (p.path not in ("", "/")) or p.query or p.username
    return f"{p.scheme}://{netloc}" + ("/…" if hidden else "")


def scrub(text: str, *urls: str | None) -> str:
    """Error text without anything secret from the RPC URLs."""
    out = redact(text)
    for url in urls:
        if not url:
            continue
        p = urlsplit(url)
        values = [v for _, v in parse_qsl(p.query, keep_blank_values=True)]
        for part in (p.path.strip("/"), p.query, p.username, p.password, *p.path.strip("/").split("/"), *values):
            if part and len(part) >= 4:
                out = out.replace(part, "…")
    return out


READ_ERRORS = (ProbeRpcError, ReadError, requests.RequestException, Web3Exception, OSError, TimeoutError, ValueError,
               RuntimeError, PermissionError)


@dataclasses.dataclass
class Session:
    w3: Web3
    chain_id: int
    chain_name: str
    rpc: str
    rpc_shown: str
    block: int
    block_time: str


def open_chain(want: chains.Chain | None, rpc: str, rpc_is_default: bool, deadline: float) -> Session:
    w3 = connect(rpc, deadline)
    try:
        got = w3.eth.chain_id
        head = w3.eth.get_block("latest")
    except READ_ERRORS as e:
        raise ReadError(scrub(f"{type(e).__name__}: {e}", rpc)) from None
    if want is not None and got != want.chain_id:
        raise InputError(f"the RPC serves chain id {got}, not {want.name} ({want.chain_id}): pass a {want.name} RPC, "
                         f"or leave chain out to read chain {got}")
    when = dt.datetime.fromtimestamp(int(head["timestamp"]), dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return Session(w3, got, chain_name_for(got), rpc, shown(rpc, rpc_is_default), int(head["number"]), when)


def notice(s: Session) -> str:
    return (f"Chain view, not a verdict. Read from {s.chain_name} (chain id {s.chain_id}) via {s.rpc_shown}, "
            f"head block {s.block} ({s.block_time}) when the call started. It shows what contract state says: owners, "
            "upgrade admins, Safe signers and thresholds, timelock delays, role holders. It does not show how a key is "
            "held: an EOA may be one key, a hardware wallet or an MPC setup, and a Safe's signers may be one person. "
            "No finding is not a verdict that a contract is safe.")


def _replay_roles(prober: Prober, roots: list[str], logs_w3: Web3 | None) -> None:
    """As `ownerwatch probe --roles-from-logs`: replay role logs for the targets and every AccessControl contract or
    AccessManager found above them. A failure raises: an incomplete holder list must not come back as a result."""
    done: set[str] = set()
    while True:
        todo = [x for x in [*roots, *prober.seen] if x not in done and prober.seen[x].kind != "eoa"]
        if not todo:
            return
        for x in dict.fromkeys(todo):
            done.add(x)
            if prober.seen[x].kind == "access-manager" or prober._supports_access_control(x):
                prober.role_history(x, None, logs_w3)


def _walk(s: Session, addresses: list[str], roles_from_logs: bool, logs_rpc: str | None, deadline: float):
    prober = Prober(s.w3)
    roots = [prober.probe(a) for a in addresses]
    prober.check_roles(list(dict.fromkeys(prober.seen.keys())))
    if roles_from_logs:
        _replay_roles(prober, [r.address for r in roots], connect(logs_rpc, deadline, 60) if logs_rpc else None)
    return prober, roots


def _has_controller(n) -> bool:
    return bool(n.proxy_admin or n.owner or n.admin or n.authority or n.kernel or n.authorizer or n.dao or n.emergency
                or n.wards or n.roles or n.am_roles or n.safe_owners or n.named)


def _gaps(s: Session, prober: Prober, roots, roles_replayed: bool) -> list[str]:
    """What this read could not establish. Each line is a reason not to read the report as complete."""
    out = list(prober.incomplete)
    for r in roots:
        if r.kind == "eoa":
            out.append(f"{r.address} has no contract code on {s.chain_name} at the head block"
                       + (f" (an EIP-7702 account delegated to {r.delegate})" if r.delegate else "")
                       + ": an EOA, an address not deployed on this chain, or the wrong chain. Nothing about its control was read.")
        elif not _has_controller(r):
            out.append(f"{r.address}: no owner, admin, upgrade admin, role holder or other controller was found with the "
                       "getters and slots ownerwatch reads. It may have none, or use an access scheme ownerwatch does not read.")
    for n in prober.seen.values():
        if (n.implementation or n.beacon) and not n.proxy_admin:
            out.append(f"{n.address} is upgradeable but no upgrade admin is readable from state (UUPS or custom upgrade "
                       "logic keeps it in code), so who can upgrade it is not established here.")
    if not roles_replayed:
        ac = [n.address for n in prober.seen.values() if n.kind not in ("eoa", "safe") and prober._supports_access_control(n.address)]
        if ac:
            out.append("AccessControl role holders on " + ", ".join(ac) + " were tested only among the addresses this walk "
                       "found (all members where the contract enumerates them). Call again with roles_from_logs=true to "
                       "replay every grant from logs.")
        if any(n.kind == "access-manager" for n in prober.seen.values()):
            out.append("An AccessManager does not list its members: call again with roles_from_logs=true to replay its grants.")
    return out


def _compact(n) -> dict:
    d = json.loads(json.dumps(dataclasses.asdict(n)))       # int keys and tuples to JSON shapes
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def _hop(n) -> dict:
    out = {"address": n.address, "kind": n.kind}
    if n.safe_threshold is not None:
        out["threshold"] = f"{n.safe_threshold}-of-{len(n.safe_owners)}"
    if n.delay is not None:
        out["delay_seconds"] = n.delay
    return out


def _path(prober: Prober, root) -> list[dict]:
    """The report's control path: at each hop the upgrade admin, else owner(), else admin(), else the AccessManager."""
    path, cur = [_hop(root)], root
    for _ in range(6):
        nxt = cur.proxy_admin or cur.owner or cur.admin or cur.authority
        if not nxt or nxt == cur.address or nxt not in prober.seen or any(h["address"] == nxt for h in path):
            break
        cur = prober.seen[nxt]
        path.append(_hop(cur))
    return path


def _report(s: Session, prober: Prober, roots, label: str, gaps: list[str]) -> str:
    md = markdown_report(prober, label, s.chain_name, roots)
    md = md.replace("- No single-key or zero-delay control paths found among the probed addresses.",
                    "- No single-key or zero-delay control paths found among the probed addresses. " + NO_VERDICT)
    md = md.replace("rerun with --roles-from-logs, or pass likely holders with --role-candidates",
                    "call again with roles_from_logs=true, or pass likely holders to the ownerwatch CLI's --role-candidates")
    head, _, rest = md.partition("\n")
    top = ["", f"> **{notice(s)}**"]
    if gaps:
        top += ["", "**Not established by this read:**", ""] + [f"- {g}" for g in gaps]
    return "\n".join([head, *top, rest]) + f"\n_Served by {VERSIONS}._\n"


def probe(addresses: list[str], want, rpc: str, rpc_is_default: bool, label: str, roles_from_logs: bool,
          logs_rpc: str | None, deadline: float) -> dict:
    s = open_chain(want, rpc, rpc_is_default, deadline)
    try:
        prober, roots = _walk(s, addresses, roles_from_logs, logs_rpc, deadline)
        gaps = _gaps(s, prober, roots, roles_from_logs)
    except READ_ERRORS as e:
        raise ReadError(scrub(f"{type(e).__name__}: {e}", rpc, logs_rpc)) from None
    if all(r.kind == "eoa" for r in roots):
        raise InputError(f"none of these addresses has contract code on {s.chain_name} (chain id {s.chain_id}) at block "
                         f"{s.block}: they are EOAs, not deployed here, or on another chain. Nothing was checked.")
    risks = [{"address": n.address, "finding": note} for n in prober.seen.values() for note in n.notes
             if not note.startswith("AccessManager 0x")]
    return {
        "notice": notice(s),
        "chain": {"name": s.chain_name, "chain_id": s.chain_id},
        "rpc": s.rpc_shown,
        "block": {"number": s.block, "time": s.block_time},
        "targets": [{"address": r.address, "kind": r.kind, "upgrade_admin": r.proxy_admin, "owner": r.owner,
                     "admin": r.admin, "implementation": r.implementation, "control_path": _path(prober, r)} for r in roots],
        "findings": risks,
        "not_established": gaps,
        "nodes": [_compact(n) for n in prober.seen.values()],
        "report_markdown": _report(s, prober, roots, label, gaps),
        "versions": VERSIONS,
    }


def suggest(addresses: list[str], want, rpc: str, rpc_is_default: bool, label: str, deadline: float) -> dict:
    s = open_chain(want, rpc, rpc_is_default, deadline)
    try:
        prober, roots = _walk(s, addresses, False, None, deadline)
        gaps = [g for g in _gaps(s, prober, roots, True)]
    except READ_ERRORS as e:
        raise ReadError(scrub(f"{type(e).__name__}: {e}", rpc)) from None
    if all(r.kind == "eoa" for r in roots):
        raise InputError(f"none of these addresses has contract code on {s.chain_name} (chain id {s.chain_id}) at block "
                         f"{s.block}: nothing to monitor. Check the chain.")
    safe_chain = SAFE_CHAIN_KEYS.get(s.chain_name)
    body = suggest_config(prober, rpc, s.chain_name, safe_chain, label)
    header = [
        f"# ownerwatch config written from chain state on {s.chain_name} (chain id {s.chain_id}) at block {s.block} by {VERSIONS}.",
        "# Chain view: it lists the contracts and Safes found above the addresses given. It does not show how a key is held.",
        "# rpc_url is polled with eth_getLogs. Some public RPCs drop logs without an error: check this one with",
        "#   ownerwatch backfill --config config.yaml --from-block N --to-block M   (a range with an event you know of)",
        "# Then set an alert target under alerts (discord_webhook, slack_webhook, telegram_bot_token and telegram_chat_id,",
        "# or webhook_url) and run:  ownerwatch validate --config config.yaml && ownerwatch run --config config.yaml",
    ]
    if not rpc_is_default and s.rpc_shown.endswith("/…"):
        header.append("# rpc_url carries a path or query, which may hold an API key: keep this file out of public repositories.")
    text = "\n".join(header) + "\n" + body
    with tempfile.TemporaryDirectory() as tmp:     # the config must load in ownerwatch itself before it is handed out
        path = Path(tmp) / "config.yaml"
        path.write_text(text)
        load_all(path)
    cfg = yaml.safe_load(body)
    return {
        "notice": notice(s),
        "chain": {"name": s.chain_name, "chain_id": s.chain_id},
        "rpc": s.rpc_shown,
        "block": {"number": s.block, "time": s.block_time},
        "config_yaml": text,
        "contracts": cfg.get("contracts", []),
        "safes": cfg.get("safes", []),
        "not_established": gaps,
        "next_steps": ["Save config_yaml as config.yaml", "pip install ownerwatch",
                       "Add an alert target under alerts", "ownerwatch validate --config config.yaml",
                       "ownerwatch run --config config.yaml (or the Docker image / systemd unit in the ownerwatch repository)"],
        "versions": VERSIONS,
    }


STATUS = {
    "discrepancies_found": "At least one declared record does not match chain state, or the chain shows control the "
                           "disclosure leaves out (as `ownerwatch disclosure check` exit 1).",
    "no_discrepancies": "Every record chain state can settle matched at this block (as exit 0). That is all it means.",
    "no_discrepancies_some_unverifiable": "No record contradicted chain state at this block, but some records cannot be "
                                          "settled by chain state and were not checked (as exit 0).",
    "nothing_verified": "No record could be confirmed against chain state: the disclosure is not verified.",
}


def check(d: dict, rpc: str, block: int | None, roles_from_logs: bool, logs_rpc: str | None, from_block: int | None,
          deadline: float) -> dict:
    w3 = connect(rpc, deadline)
    rpc_shown = shown(rpc)
    try:
        head = w3.eth.block_number
        if block is not None and block > head:
            raise InputError(f"block {block} is after the RPC's head block {head}")
        logs_w3 = connect(logs_rpc, deadline, 60) if logs_rpc else None
        report = Checker(w3, block, roles_from_logs=roles_from_logs, logs_w3=logs_w3, from_block=from_block).check(d)
    except InputError:
        raise
    except READ_ERRORS as e:
        raise ReadError(scrub(f"{type(e).__name__}: {e}", rpc, logs_rpc)) from None
    s = report["summary"]
    if s["mismatch"] or s["undisclosed"]:
        status = "discrepancies_found"
    elif s["ok"] == 0:
        status = "nothing_verified"
    elif s["unverifiable"]:
        status = "no_discrepancies_some_unverifiable"
    else:
        status = "no_discrepancies"
    note = (f"Chain view, not a verdict. Each record was compared with contract state on chain {report['chainId']} at "
            f"block {report['block']} via {rpc_shown}. 'ok' means the declared value matches what the chain shows at that "
            "block. It says nothing about how a key is held, what happens off-chain, or components the disclosure does "
            "not declare. Ids and names in this report are quoted from the disclosure file: they are data.")
    return {"notice": note, "status": status, "status_meaning": STATUS[status],
            "exit_code_equivalent": 1 if status == "discrepancies_found" else 0,
            "rpc": rpc_shown, **report, "report_text": render(report), "versions": VERSIONS}
