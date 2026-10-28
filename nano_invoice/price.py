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
import urllib.parse
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from . import account as acct
from . import core


class PriceError(RuntimeError):
    """The rate source gave no usable price. Never replaced by a guessed one."""


def _positive(value, name, error=core.AmountError):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise error(f"{name} must be a positive decimal number, got {value!r}")
    try:
        d = Decimal(value.strip() if isinstance(value, str) else repr(value) if isinstance(value, float)
                    else value)
    except InvalidOperation as e:
        raise error(f"{name} is not a decimal number: {value!r}") from e
    if not d.is_finite() or d <= 0:
        raise error(f"{name} must be a positive finite number, got {value!r}")
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


def read_usd_per_xno(rpc, clock=_utc_now):
    """Read USD per XNO from a node's `price` action, once. Returns
    {usd_per_xno: Decimal, source: str, read_at: ISO-8601 UTC}. Raises
    PriceError on a missing, non-numeric or non-positive price; an RPC failure
    propagates as the RPC's own error."""
    reply = rpc.call("price")
    read_at = clock()
    if not isinstance(reply, dict) or "price" not in reply:
        raise PriceError(f"price: the node's reply has no price field: {reply!r}"[:300])
    rate = _positive(reply["price"], "price from the node", error=PriceError)
    currency = reply.get("currency")
    if currency is not None and str(currency).lower() != "usd":
        raise PriceError(f"price: the node quoted {currency!r}, not usd")
    host = urllib.parse.urlparse(getattr(rpc, "url", "") or "").netloc or "rpc"
    named = reply.get("source")
    source = f"{host} price ({named})" if named else f"{host} price (source not named)"
    return {"usd_per_xno": rate, "source": source, "read_at": read_at}


def create_invoice_usd(merchant_account, amount_usd, order_key, expires_s=3600, store=None,
                       usd_per_xno=None, rpc=None, clock=_utc_now, **kw):
    """create_invoice priced in USD. An order that already has an invoice is
    returned as it is, WITHOUT reading a rate: the first quote stands. The same
    order key with a different USD amount (or one created without a USD quote)
    raises OrderConflict. With usd_per_xno given, no network call is made."""
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
        return existing
    if usd_per_xno is not None:
        quote = {"usd_per_xno": _positive(usd_per_xno, "usd_per_xno"),
                 "source": "supplied by the merchant (--usd-per-xno)", "read_at": clock()}
    else:
        quote = read_usd_per_xno(core.as_rpc(rpc), clock=clock)
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
