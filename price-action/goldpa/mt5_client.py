"""Thin MetaTrader5 wrapper. The `import MetaTrader5` happens lazily inside
connect() so this file imports cleanly on non-Windows dev machines — every
function here needs a connected `mt5` handle from connect(cfg).

Config keys (config.json): terminal_path, login, password, server, symbol.
"""
import datetime as dt

import pandas as pd

MAGIC = 20261006   # tags this bot's orders — the account also holds other bots' history


def connect(cfg: dict):
    import MetaTrader5 as mt5
    if not mt5.initialize(path=cfg["terminal_path"], login=int(cfg["login"]),
                          password=cfg["password"], server=cfg["server"]):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def fetch_bars(mt5, symbol: str, tf: str, count: int) -> pd.DataFrame:
    """Last `count` bars as the data.py schema: mid OHLC + bid/ask extremes.
    MT5 python candles are bid-based; ask is rebuilt from the spread column.
    Row -1 is the currently forming bar (callers process [-2] as last closed)."""
    rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"no rates for {symbol} {tf}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    sp = df["spread"] * mt5.symbol_info(symbol).point
    return pd.DataFrame({
        "open": df["open"] + sp / 2, "high": df["high"] + sp / 2,
        "low": df["low"] + sp / 2, "close": df["close"] + sp / 2,
        "bid_low": df["low"], "bid_high": df["high"],
        "ask_low": df["low"] + sp, "ask_high": df["high"] + sp,
    })


def spec(mt5, symbol: str) -> dict:
    i = mt5.symbol_info(symbol)
    if i is None:
        raise RuntimeError(f"symbol_info failed for {symbol}: {mt5.last_error()}")
    return {
        "digits": i.digits, "point": i.point,
        "volume_min": i.volume_min, "volume_step": i.volume_step, "volume_max": i.volume_max,
        "contract_size": i.trade_contract_size,
        "filling": _pick_filling(mt5, i),
    }


def _pick_filling(mt5, info) -> int:
    mt5f = __import__("MetaTrader5")
    if info.filling_mode & 1:
        return mt5f.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5f.ORDER_FILLING_IOC
    return mt5f.ORDER_FILLING_RETURN


def lots_for(sp: dict, equity: float, risk: float, dist: float) -> float:
    """Position size for `risk` fraction of equity over a `dist` price stop."""
    if dist <= 0:
        return 0.0
    raw = equity * risk / (dist * sp["contract_size"])
    step = sp["volume_step"] or 0.01
    lots = int(raw / step) * step
    return max(sp["volume_min"], min(lots, sp["volume_max"])) if lots >= sp["volume_min"] else 0.0


def place_limit(mt5, symbol: str, sp: dict, side: int, entry: float, sl: float,
                tp: float, lots: float, comment: str):
    """Resting limit at the zone edge with SL/TP attached. Returns ticket or None."""
    mt5f = __import__("MetaTrader5")
    d = sp["digits"]
    req = {
        "action": mt5f.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lots,
        "type": mt5f.ORDER_TYPE_BUY_LIMIT if side == 1 else mt5f.ORDER_TYPE_SELL_LIMIT,
        "price": round(entry, d), "sl": round(sl, d), "tp": round(tp, d),
        "type_time": mt5f.ORDER_TIME_GTC,
        "type_filling": sp["filling"],
        "magic": MAGIC, "comment": comment[:26],   # MT5 python rejects > 29 chars ("Invalid comment argument")
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5f.TRADE_RETCODE_DONE:
        raise RuntimeError(f"place_limit rejected: {res}")
    return res.order


def cancel(mt5, ticket: int) -> None:
    mt5f = __import__("MetaTrader5")
    res = mt5.order_send({"action": mt5f.TRADE_ACTION_REMOVE, "order": ticket})
    if res is None or res.retcode != mt5f.TRADE_RETCODE_DONE:
        raise RuntimeError(f"cancel {ticket} rejected: {res}")


def pendings(mt5, symbol: str) -> list:
    return list(mt5.orders_get(symbol=symbol) or [])


def positions(mt5, symbol: str) -> list:
    return list(mt5.positions_get(symbol=symbol) or [])


def equity(mt5) -> float:
    return float(mt5.account_info().equity)


def closed_positions(mt5, symbol: str) -> list:
    """This bot's fully closed positions on `symbol` from MT5 history:
    [(position_id, deals sorted by time, opening order or None)].
    Ownership is the ENTRY deal's magic — manual closes carry magic 0."""
    # +2 days: history is queried in server time, which runs ahead of local/UTC
    hist = mt5.history_deals_get(dt.datetime(2000, 1, 1),
                                 dt.datetime.now() + dt.timedelta(days=2)) or ()
    by_pos = {}
    for d in hist:
        if d.symbol == symbol and d.position_id:
            by_pos.setdefault(d.position_id, []).append(d)
    out = []
    for pid, ds in by_pos.items():
        ins = [d for d in ds if d.entry == mt5.DEAL_ENTRY_IN]
        outs = [d for d in ds if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
        if not ins or ins[0].magic != MAGIC or not outs:
            continue
        if sum(d.volume for d in ins) - sum(d.volume for d in outs) > 1e-9:
            continue                      # partially closed — still open
        orders = mt5.history_orders_get(ticket=pid) or ()
        out.append((pid, sorted(ds, key=lambda d: d.time_msc), orders[0] if orders else None))
    return out


def flatten(mt5, symbol: str, sp: dict) -> None:
    """Close every position and remove every pending order on `symbol`."""
    mt5f = __import__("MetaTrader5")
    for o in pendings(mt5, symbol):
        cancel(mt5, o.ticket)
    for p in positions(mt5, symbol):
        tick = mt5.symbol_info_tick(symbol)
        req = {
            "action": mt5f.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": p.volume, "type": mt5f.ORDER_TYPE_SELL if p.type == 0 else mt5f.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": tick.bid if p.type == 0 else tick.ask,
            "deviation": 50, "magic": MAGIC, "comment": "gp-flatten",
            "type_filling": sp["filling"],
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5f.TRADE_RETCODE_DONE:
            raise RuntimeError(f"flatten {p.ticket} rejected: {res}")
