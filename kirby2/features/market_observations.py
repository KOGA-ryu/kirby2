"""V1 blind observations from sequenced exchange evidence, never generator labels.

This projection deliberately separates executed depletion from cancellations and
counts replenishment only at the same side/price after execution in this window.
It does not rename static book imbalance as order-flow imbalance.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from kirby2.ui.simulation_contract import canonical_digest

WINDOW_US = 5_000_000


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(label + " must be a bounded integer")
    return value


def observe_market(events, books, *, cut_us, delivered_at_us=None, window_us=WINDOW_US,
                   coverage_start_us=0, baseline=None):
    """Evaluate (cut-window, cut], with explicit coverage and delivery state.

    Events have contiguous exchange sequence and normalized public quantities.
    Books contain exact boundary samples; no interpolation is claimed. A baseline
    contains independent prior notional sums/count, profile and population IDs.
    """
    _integer(cut_us,"cut"); _integer(window_us,"window",1); _integer(coverage_start_us,"coverage")
    delivered = cut_us if delivered_at_us is None else _integer(delivered_at_us,"delivery")
    if delivered < cut_us:
        raise ValueError("observations cannot be delivered before their source cut")
    # Restrict identity to public inputs at or before the cut. Extra labels and
    # future entries cannot affect a blind observation, including its digest.
    fields = ("sequence","time_us","kind","side","price_ticks","quantity")
    prefix = [{key: row[key] for key in fields} for row in events if row["time_us"] <= cut_us]
    for row in prefix:
        _integer(row["sequence"],"sequence",1); _integer(row["time_us"],"event time")
        _integer(row["quantity"],"quantity"); _integer(row["price_ticks"],"price")
        if row["kind"] not in {"TRADE","CANCEL","ADD","OTHER"} or row["side"] not in {"BUY","SELL",None}:
            raise ValueError("unsupported public event")
        if row["kind"] != "OTHER" and (row["side"] is None or row["quantity"] == 0):
            raise ValueError("economic events require side and positive quantity")
    times = [row["time_us"] for row in prefix]
    gap = ([row["sequence"] for row in prefix] != list(range(1,len(prefix)+1)) or times != sorted(times))
    book_fields = ("time_us","bid_ticks","ask_ticks","bid_shares","ask_shares")
    samples = [{key: row[key] for key in book_fields} for row in books if row["time_us"] <= cut_us]
    for row in samples:
        _integer(row["time_us"],"book time")
        for key in ("bid_shares","ask_shares"):
            _integer(row[key],key)
        for key in ("bid_ticks","ask_ticks"):
            if row[key] is not None: _integer(row[key],key,1)
    book_times = [row["time_us"] for row in samples]
    gap |= book_times != sorted(set(book_times))
    available = {row["time_us"]:row for row in samples}
    left = cut_us-window_us
    status = ("OBSERVATION_GAP" if gap else "INSUFFICIENT_WINDOW" if left < coverage_start_us
              else "MISSING_BOUNDARY" if left not in available or cut_us not in available else "AVAILABLE")
    basis = {"events":prefix,"books":samples,"cut_us":cut_us,"coverage_start_us":coverage_start_us}
    values = dict(executed_buy_shares=0,executed_sell_shares=0,executed_notional_tick_shares=0,
                  cancelled_bid_shares=0,cancelled_ask_shares=0,added_bid_shares=0,added_ask_shares=0,
                  replenished_bid_shares=0,replenished_ask_shares=0,trade_count=0)
    debt = defaultdict(int)
    for row in prefix:
        if not left < row["time_us"] <= cut_us: continue
        side, qty, price = row["side"], row["quantity"], row["price_ticks"]
        if row["kind"] == "TRADE":
            values["executed_buy_shares" if side == "BUY" else "executed_sell_shares"] += qty
            values["executed_notional_tick_shares"] += qty*price
            values["trade_count"] += 1
            debt[("SELL" if side == "BUY" else "BUY", price)] += qty
        elif row["kind"] in {"ADD","CANCEL"}:
            suffix = "bid" if side == "BUY" else "ask"
            values[("added_" if row["kind"] == "ADD" else "cancelled_")+suffix+"_shares"] += qty
            if row["kind"] == "ADD":
                replaced = min(qty, debt[(side,price)])
                values["replenished_"+suffix+"_shares"] += replaced
                debt[(side,price)] -= replaced
    aggressive = values["executed_buy_shares"]+values["executed_sell_shares"]
    values["aggressive_imbalance_ppm"] = (None if aggressive == 0 else int(
        (Decimal(values["executed_buy_shares"]-values["executed_sell_shares"])*1_000_000/aggressive).quantize(Decimal('1'))))
    book = available.get(cut_us)
    old = available.get(left)
    values["mid_change_half_ticks"] = None
    values["spread_ticks"] = None
    values["bid_depth_shares"] = None if book is None else book["bid_shares"]
    values["ask_depth_shares"] = None if book is None else book["ask_shares"]
    if book and book["bid_ticks"] is not None and book["ask_ticks"] is not None:
        values["spread_ticks"] = book["ask_ticks"]-book["bid_ticks"]
        if old and old["bid_ticks"] is not None and old["ask_ticks"] is not None:
            values["mid_change_half_ticks"] = book["bid_ticks"]+book["ask_ticks"]-old["bid_ticks"]-old["ask_ticks"]
    prior_status, ratio, prior_id = "MISSING_BASELINE", None, None
    if baseline is not None:
        expected = {"schema_id","profile_sha256","seeds","cut_us","window_us","notional_sum","sample_count"}
        if set(baseline) != expected or baseline["schema_id"] != "KIRBY2_SYNTHETIC_PRIOR_V1":
            raise ValueError("unsupported baseline")
        total = _integer(baseline["notional_sum"],"prior notional")
        count = _integer(baseline["sample_count"],"prior sample count",1)
        seeds = baseline["seeds"]
        if type(seeds) is not list or len(seeds) != count or len(set(seeds)) != count or any(type(seed) is not int for seed in seeds):
            raise ValueError("prior population mismatch")
        prior_id = canonical_digest(baseline)
        prior_status = ("BASELINE_COORDINATE_MISMATCH" if baseline["cut_us"] != cut_us or baseline["window_us"] != window_us
                        else "ZERO_BASELINE" if total == 0 else "AVAILABLE")
        if status == "AVAILABLE" and prior_status == "AVAILABLE":
            ratio = str((Decimal(values["executed_notional_tick_shares"])*count/total).quantize(Decimal('.000001')))
    return {"schema_id":"KIRBY2_MARKET_OBSERVATION_V1","schema_version":1,
            "cut_us":cut_us,"delivered_at_us":delivered,"age_us":delivered-cut_us,
            "delivery_status":"CURRENT" if delivered == cut_us else "STALE",
            "window_us":window_us,"interval":"(cut-window,cut]","status":status,
            "source_prefix_sha256":canonical_digest(basis),
            "values":values if status == "AVAILABLE" else {key:None for key in values},
            "relative_activity":{"status":status if status != "AVAILABLE" else prior_status,
                                 "ratio":ratio,"unit":"observed_notional / independent_synthetic_prior_notional",
                                 "baseline_id":prior_id},
            "provenance":"SYNTHETIC_MATCHING_ENGINE_PUBLIC_PREFIX; zero modeled delivery delay"}
