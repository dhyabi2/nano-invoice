"""nano-invoice: bind a Nano (XNO) payment to the order it pays for.

Non-custodial and read-only: it never holds a key and never sends money.
"""
from .account import InvalidAccount, normalise as normalise_account, same_account
from .core import (
    CLOCK_SKEW_S, RAW_PER_XNO, RECEIPT_SCHEMA, RECEIPT_SCHEMA_V2, RECEIPT_SCHEMAS,
    TAG_MODULUS, TAG_QUARANTINE_S,
    AmountError, BlockAlreadyBound, IdempotencyConflict, IllegalTransition, Invoice, InvoiceError,
    NotFound, OrderConflict,
    Store, TagsExhausted, append_tombstone, append_verdict, check_invoice, create_invoice,
    invoice_id_for, order_key_hash, receipt, receipt_schema_of,
    refund_hints, refund_instruction, require_raw, scan, verify_log, verify_receipt, witness_payload,
    xno_to_raw,
)
from .receipt_v2 import (
    ASSET, SCALE, VERDICTS, LedgerBroken, TermsError, canonical_json, digest, normalise_terms,
)
from .rpc import DEFAULT_RPC, Rpc, RpcError

__version__ = "0.1.0"
