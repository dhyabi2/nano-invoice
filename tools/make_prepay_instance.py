"""Produce ONE example pre-payment binding + witness entry, from a throwaway key that is never saved.

The key is generated in memory, signs, and is dropped when the process ends: the output proves the format and
the verifier, and cannot be used to spend anything because the key no longer exists. The payee address in the
output is therefore a DEMO address, not anyone's wallet. A real payee runs the same two calls with its own key:

    doc = prepay_binding.sign_binding(its_private_key, fields)
    chain = [prepay_binding.witness_append([], doc["digest"], <the witness's clock>, "<witness name>")]

usage: python3 tools/make_prepay_instance.py OUT_DIR
"""
import json
import os
import sys
import time

from nano_invoice import nano_sig
from nano_invoice import prepay_binding as pb

TERMS = "Deliver the 40-page summary of document 8f3a within 10 minutes of a confirmed payment of 0.0001 XNO."


def main(out_dir):
    key = os.urandom(32)  # in memory only
    now = int(time.time())
    fields = {"v": pb.VERSION, "order_id": "ord_" + os.urandom(8).hex(), "payee": nano_sig.address(key),
              "amount_raw": "100000000000000000000000000", "asset": "XNO", "issued_at": now,
              "expires_at": now + 7 * 86400, "terms_sha256": pb.terms_hash(TERMS)}
    doc = pb.sign_binding(key, fields)
    chain = [pb.witness_append([], doc["digest"], now, "demo-issuer-log (self-witnessed; see README)")]
    del key
    os.makedirs(out_dir, exist_ok=True)
    for name, obj in (("binding.json", doc), ("witness.json", chain)):
        with open(os.path.join(out_dir, name), "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
    with open(os.path.join(out_dir, "terms.txt"), "w") as fh:
        fh.write(TERMS + "\n")
    print(json.dumps({"order_id": doc["order_id"], "payee": doc["payee"], "digest": doc["digest"]}))


if __name__ == "__main__":
    main(sys.argv[1])
