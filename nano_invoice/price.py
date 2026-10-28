"""Price an invoice in USD: one reference rate, read once, turned into a fixed XNO amount.

XNO floats against the dollar, but an invoice does not: its raw amount is fixed
when it is created. So a fixed-fiat price needs a rate exactly once per invoice,
at creation, and the per-invoice quote is the re-quote. The rate is a REFERENCE
(by default the public node's `price` action, which names its own source); it is
not a swap quote and nobody is bound to it. The invoice records the USD amount,
the rate, where the rate came from and when it was read, and never changes them.

Arithmetic is Decimal throughout, never float: raw = floor(usd / usd_per_xno *
10**30), then rounded DOWN to a multiple of TAG_MODULUS so create_invoice never
refuses it. The rounding is below 1e-24 XNO.
"""
import datetime
import re
import time
from decimal import ROUND_FLOOR, Decimal, DecimalException, localcontext

from . import account as acct
from . import core


class PriceError(core.InvoiceError, RuntimeError):
    """The rate source gave no usable price. Never replaced by a guessed one.
    An InvoiceError, so the CLI reports it as a refusal (exit 2)."""


# Plain ASCII decimals only: no exponent, sign, underscores or non-ASCII digits,
# all of which Decimal() would otherwise accept. `[0-9]`, not `\d` (Unicode).
_PLAIN_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")
_MAX_TEXT = 80          # characters; a longer "number" is refused before parsing
_MAX_MAGNITUDE = 60     # |adjusted exponent|; bounds the work before any arithmetic

DEFAULT_MAX_QUOTE_AGE_S = 600   # a node quote older than this is refused
MAX_QUOTE_SKEW_S = 60           # a quote timestamp this far in the future is refused


def _positive(value, name, error=core.AmountError):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise error(f"{name} must be a positive decimal number, got {value!r}")
    try:
        if isinstance(value, str):
            text = value.strip()
            if len(text) > _MAX_TEXT or not _PLAIN_DECIMAL.fullmatch(text):
                raise error(f"{name} must be a plain decimal such as 0.25 (ASCII digits, optional "
                            f"fraction, no sign or exponent), got {value[:_MAX_TEXT]!r}")
            d = Decimal(text)
        elif isinstance(value, int):
            if value.bit_length() > 256:
                raise error(f"{name} is out of range")
            d = Decimal(value)
        else:
            d = Decimal(repr(value)) if isinstance(value, float) else value
        if not d.is_finite() or d <= 0:
            raise error(f"{name} must be a positive finite number, got {value!r}")
        if abs(d.adjusted()) > _MAX_MAGNITUDE:
            raise error(f"{name} is out of range: {value!r}"[:200])
    except DecimalException as e:
        raise error(f"{name} is not a usable decimal number: {value!r}"[:200]) from e
    return d


def usd_to_raw(amount_usd, usd_per_xno):
    """USD amount and USD-per-XNO rate (decimal strings or Decimals) -> raw int,
    floored to a multiple of TAG_MODULUS. Exact: no float anywhere."""
    usd = _positive(amount_usd, "amount_usd")
    rate = _positive(usd_per_xno, "usd_per_xno")
    with localcontext() as ctx:
        ctx.prec = 200
        ctx.rounding = ROUND_FLOOR  # never round a quotient up across a raw boundary
        raw = int((usd * core.RAW_PER_XNO / rate).to_integral_value(rounding=ROUND_FLOOR))
    raw -= raw % core.TAG_MODULUS
    if raw < core.TAG_MODULUS:
        raise core.AmountError(
            f"{amount_usd} USD at {usd_per_xno} USD/XNO is below {core.TAG_MODULUS} raw, the smallest "
            "amount an invoice can carry")
    return core.require_raw(raw, "amount_raw", minimum=core.TAG_MODULUS)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _band(rate, min_usd_per_xno, max_usd_per_xno, error):
    """The caller's optional sanity band: a rate outside it is refused."""
    lo = None if min_usd_per_xno is None else _positive(min_usd_per_xno, "min_usd_per_xno")
    hi = None if max_usd_per_xno is None else _positive(max_usd_per_xno, "max_usd_per_xno")
    if lo is not None and hi is not None and lo > hi:
        raise core.AmountError(f"min_usd_per_xno {lo} is above max_usd_per_xno {hi}")
    if lo is not None and rate < lo:
        raise error(f"rate {rate} USD/XNO is below the sanity floor {lo}; refusing it")
    if hi is not None and rate > hi:
        raise error(f"rate {rate} USD/XNO is above the sanity ceiling {hi}; refusing it")
    return rate


def _quote_time(reply, now, max_age_s):
    ts = reply.get("timestamp_unix")
    if isinstance(ts, str) and ts.strip().isascii() and ts.strip().isdigit() and len(ts.strip()) <= 12:
        ts = int(ts.strip())
    if isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
        raise PriceError(f"price: the node's reply has no usable timestamp_unix: {ts!r}"[:300])
    if ts > now + MAX_QUOTE_SKEW_S:
        raise PriceError(f"price: the quote's timestamp {ts} is {ts - now:.0f}s in the future")
    if now - ts > max_age_s:
        raise PriceError(f"price: the quote is {now - ts:.0f}s old (timestamp {ts}); "
                         f"refusing anything older than {max_age_s}s")
    return ts


def read_usd_per_xno(rpc, clock=_utc_now, now=time.time, max_age_s=DEFAULT_MAX_QUOTE_AGE_S,
                     min_usd_per_xno=None, max_usd_per_xno=None):
    """Read USD per XNO from a node's `price` action, once. Returns
    {usd_per_xno: Decimal, source: str, read_at: ISO-8601 UTC}. The source is
    the URL we queried, never a string from the reply. Raises PriceError on a
    missing, non-numeric or non-positive price; on a currency that is missing
    or not usd; on a missing, zero, future or older-than-max_age_s
    timestamp_unix; and on a rate outside the optional sanity band. An RPC
    failure propagates as the RPC's own error."""
    if isinstance(max_age_s, bool) or not isinstance(max_age_s, int) or max_age_s <= 0:
        raise core.InvoiceError("max_age_s must be a positive int (seconds)")
    reply = rpc.call("price")
    read_at = clock()
    if not isinstance(reply, dict) or "price" not in reply:
        raise PriceError(f"price: the node's reply has no price field: {reply!r}"[:300])
    currency = reply.get("currency")
    if not isinstance(currency, str) or currency.strip().lower() != "usd":
        raise PriceError(f"price: the node's quote is not in usd (currency {currency!r})"[:300])
    rate = _positive(reply["price"], "price from the node", error=PriceError)
    _quote_time(reply, now(), max_age_s)
    _band(rate, min_usd_per_xno, max_usd_per_xno, PriceError)
    url = getattr(rpc, "url", None)
    source = f"{url} price action" if isinstance(url, str) and url else "rpc price action (url unknown)"
    return {"usd_per_xno": rate, "source": source, "read_at": read_at}


def create_invoice_usd(merchant_account, amount_usd, order_key, expires_s=3600, store=None,
                       usd_per_xno=None, rpc=None, clock=_utc_now, max_age_s=DEFAULT_MAX_QUOTE_AGE_S,
                       min_usd_per_xno=None, max_usd_per_xno=None, **kw):
    """create_invoice priced in USD. An order that already has an invoice is
    returned as it is, WITHOUT reading a rate: the first quote stands - but only
    after the same checks the XNO path makes on a re-run (bound terms, intent
    hash, idempotency key), so a re-run with different binding raises
    IdempotencyConflict. The same order key with a different USD amount (or one
    created without a USD quote) raises OrderConflict. With usd_per_xno given,
    no network call is made (the sanity band still applies)."""
    store = core._require_store(store)
    usd = _positive(amount_usd, "amount_usd")
    merchant = acct.normalise(merchant_account)
    inv_id = core.invoice_id_for(merchant, core.order_key_hash(order_key))
    try:
        existing = store.get(inv_id)
    except core.NotFound:
        existing = None
    if existing is not None:
        _same_usd(existing, usd)
        # Re-run through create_invoice at the recorded amount: it finds the row
        # and applies the XNO path's binding check before returning it.
        inv = core.create_invoice(merchant, existing.amount_raw, order_key, expires_s, store=store,
                                  quote={"usd": existing.quote_usd,
                                         "usd_per_xno": existing.quote_usd_per_xno,
                                         "source": existing.quote_source,
                                         "read_at": existing.quote_read_at}, **kw)
        _same_usd(inv, usd)
        return inv
    if usd_per_xno is not None:
        quote = {"usd_per_xno": _band(_positive(usd_per_xno, "usd_per_xno"), min_usd_per_xno,
                                      max_usd_per_xno, PriceError),
                 "source": "supplied by the merchant (--usd-per-xno)", "read_at": clock()}
    else:
        quote = read_usd_per_xno(core.as_rpc(rpc), clock=clock, max_age_s=max_age_s,
                                 min_usd_per_xno=min_usd_per_xno, max_usd_per_xno=max_usd_per_xno)
    raw = usd_to_raw(usd, quote["usd_per_xno"])
    inv = core.create_invoice(merchant, raw, order_key, expires_s, store=store, quote={
        "usd": str(usd), "usd_per_xno": str(quote["usd_per_xno"]),
        "source": quote["source"], "read_at": quote["read_at"]}, **kw)
    _same_usd(inv, usd)  # a concurrent creator may have won with another amount
    return inv


def _same_usd(inv, usd):
    if inv.quote_usd is None:
        raise core.OrderConflict(
            f"order already invoiced as {inv.id} for {inv.amount_raw} raw without a USD quote; "
            f"refusing to re-price it as {usd} USD")
    if Decimal(inv.quote_usd) != usd:
        raise core.OrderConflict(
            f"order already invoiced as {inv.id} for {inv.quote_usd} USD; refusing {usd} USD")
