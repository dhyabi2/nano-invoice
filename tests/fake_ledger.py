"""An in-memory Nano ledger answering the RPC actions nano-invoice reads.
Response shapes copied from a live node (rpc.nano.to)."""
import hashlib

from nano_invoice.account import from_public_key
from nano_invoice.rpc import RpcError


def account(n):
    return from_public_key(hashlib.sha256(f"test-account-{n}".encode()).digest())


class FakeLedger:
    def __init__(self):
        self.blocks = {}
        self.history = {}
        self.receivable = {}
        self.calls = []
        self._n = 0

    def _hash(self):
        self._n += 1
        return hashlib.sha256(f"block-{self._n}".encode()).hexdigest().upper()

    def send(self, sender, dest, amount, ts, confirmed=True, received=True, receive_ts=None):
        send_hash = self._hash()
        self.blocks[send_hash] = {
            "block_account": sender, "amount": str(amount), "local_timestamp": str(ts),
            "confirmed": "true" if confirmed else "false", "subtype": "send",
            "contents": {"type": "state", "account": sender, "link_as_account": dest,
                         "link": "00" * 32},
        }
        self.history.setdefault(sender, []).insert(0, {
            "type": "send", "account": dest, "amount": str(amount), "local_timestamp": str(ts),
            "hash": send_hash, "confirmed": "true" if confirmed else "false"})
        if not received:
            self.receivable.setdefault(dest, {})[send_hash] = {"amount": str(amount), "source": sender}
            return send_hash, None
        return send_hash, self.receive(dest, send_hash, receive_ts or ts + 1, confirmed)

    def receive(self, dest, send_hash, ts, confirmed=True):
        self.receivable.get(dest, {}).pop(send_hash, None)
        rh = self._hash()
        amount = self.blocks[send_hash]["amount"]
        self.blocks[rh] = {
            "block_account": dest, "amount": amount, "local_timestamp": str(ts),
            "confirmed": "true" if confirmed else "false", "subtype": "receive",
            "contents": {"type": "state", "account": dest, "link": send_hash},
        }
        self.history.setdefault(dest, []).insert(0, {
            "type": "receive", "account": self.blocks[send_hash]["block_account"], "amount": amount,
            "local_timestamp": str(ts), "hash": rh, "confirmed": "true" if confirmed else "false"})
        return rh

    def confirm(self, *hashes):
        for h in hashes:
            self.blocks[h]["confirmed"] = "true"
            for hist in self.history.values():
                for e in hist:
                    if e["hash"] == h:
                        e["confirmed"] = "true"

    def call(self, action, **p):
        self.calls.append((action, p))
        if action == "block_info":
            b = self.blocks.get(p["hash"].upper())
            if b is None:
                raise RpcError("block_info: Block not found")
            return dict(b)
        if action == "receivable":
            blocks = self.receivable.get(p["account"], {})
            return {"blocks": dict(blocks) if blocks else ""}
        if action == "account_history":
            hist = self.history.get(p["account"], [])
            start = 0
            if p.get("head"):
                start = next(i for i, e in enumerate(hist) if e["hash"] == p["head"])
            count = int(p.get("count", 100))
            page = hist[start:start + count]
            out = {"account": p["account"], "history": page}
            if start + count < len(hist):
                out["previous"] = hist[start + count]["hash"]
            return out
        raise RpcError(f"fake ledger does not serve {action}")
