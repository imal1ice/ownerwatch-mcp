"""A small JSON-RPC node on 127.0.0.1 for offline tests: one Ownable contract owned by an EOA, plus failure modes."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT = "0x1111111111111111111111111111111111111111"
OWNER_EOA = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
OWNER_SEL = "0x8da5cb5b"          # owner()
CHAIN_ID = 1
HEAD = 1000


class Stub:
    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.methods: list[str] = []
        self.paths: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                stub.paths.append(self.path)
                reqs = body if isinstance(body, list) else [body]
                stub.methods += [r["method"] for r in reqs]
                if stub.mode == "http500":
                    return self._send(500, b"internal error", "text/plain")
                if stub.mode == "html":
                    return self._send(200, b"<html>hello</html>", "text/html")
                if stub.mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return None
                out = [stub.answer(r) for r in reqs]
                data = json.dumps(out if isinstance(body, list) else out[0]).encode()
                return self._send(200, data, "application/json")

            def _send(self, code, data, ctype):
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, r: dict) -> dict:
        m, p, rid = r["method"], r.get("params") or [], r.get("id")

        def ok(result):
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code, msg):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

        if m == "eth_chainId":
            return ok(hex(5 if self.mode == "wrong_chain" else CHAIN_ID))
        if m == "eth_blockNumber":
            return ok(hex(HEAD))
        if m == "eth_getBlockByNumber":
            return ok({"number": hex(HEAD), "timestamp": hex(1_790_000_000), "hash": "0x" + "ab" * 32,
                       "parentHash": "0x" + "cd" * 32, "transactions": []})
        if self.mode == "slow":
            time.sleep(2)
        if self.mode == "rate_limited" and m in ("eth_call", "eth_getStorageAt"):
            return err(-32005, "rate limit exceeded")
        if m == "eth_getCode":
            if self.mode == "no_code":
                return ok("0x")
            return ok("0x6080604052" + "00" * 200 if p[0].lower() == CONTRACT else "0x")
        if m == "eth_getStorageAt":
            return ok("0x" + "00" * 32)
        if m == "eth_call":
            tx = p[0]
            data = (tx.get("data") or tx.get("input") or "").lower()
            if tx["to"].lower() == CONTRACT and data == OWNER_SEL:
                return ok("0x" + "00" * 12 + OWNER_EOA[2:])
            return err(3, "execution reverted")
        return err(-32601, f"method {m} not supported by the stub")
