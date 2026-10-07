"""Fill simulation — the validated backtest semantics in ONE place.

Used by backtest.run() (regression-gated: metrics must stay byte-identical)
and by runner --dry (paper trading). Touch=fill limit orders, SL/TP on touch
(stop checked first, conservative), same-bar entry+stop handling, own-side
book (longs fill/exit on ask/bid respectively).
"""
from .strategy import ENTRY_HOURS

STOP_SLIP = 0.10   # $/oz adverse fill when a stop is hit


class SimBroker:
    def __init__(self, fill_qty=None):
        # fill_qty: callable(stop_dist)->qty resolved at FILL time (backtest's
        # compounding sizing). None = qty fixed at place (dry run, like real MT5).
        self.fill_qty = fill_qty
        self.pending = None   # dict(side, entry, stop, target, qty, ticket)
        self.pos = None       # dict(side, entry, stop, target, qty, i, ticket)
        self._t = 0

    def place(self, side: int, entry: float, stop: float, target: float, qty=None) -> int:
        self._t += 1
        if self.fill_qty is not None:
            qty = None                      # resolved at fill
        self.pending = {"side": side, "entry": entry, "stop": stop, "target": target,
                        "qty": qty, "ticket": self._t}
        return self._t

    def cancel_pending(self) -> None:
        self.pending = None

    def force_close(self, i: int, price: float) -> list:
        """Interlock flatten: close the open position at `price` (bar close)."""
        if not self.pos:
            return []
        p = self.pos
        ev = {"kind": "exit", "side": p["side"], "entry": p["entry"], "stop": p["stop"],
              "price": price, "qty": p["qty"], "i": i, "hi": p["i"],
              "reason": "flatten", "ticket": p["ticket"]}
        self.pos = None
        return [ev]

    def manage(self, i: int, bar) -> list:
        """Exit checks for the open position. Returns exit events."""
        evs = []
        p = self.pos
        if p:
            exit_price = reason = None
            if p["side"] == 1:
                if bar["bid_low"] <= p["stop"]:
                    exit_price, reason = p["stop"] - STOP_SLIP, "stop"
                elif bar["bid_high"] >= p["target"]:
                    exit_price, reason = p["target"], "target"
            else:
                if bar["ask_high"] >= p["stop"]:
                    exit_price, reason = p["stop"] + STOP_SLIP, "stop"
                elif bar["ask_low"] <= p["target"]:
                    exit_price, reason = p["target"], "target"
            if exit_price is not None:
                evs.append({"kind": "exit", "side": p["side"], "entry": p["entry"],
                            "stop": p["stop"], "price": exit_price, "qty": p["qty"],
                            "i": i, "hi": p["i"], "reason": reason, "ticket": p["ticket"]})
                self.pos = None
        return evs

    def try_fill(self, i: int, bar, ts) -> list:
        """Fill the resting limit on a touch of the own-side book, then check
        the same-bar stop. Returns fill/exit/reject events for the caller to
        account; the caller must call StrategyState.on_filled() on fill/reject."""
        evs = []
        pend = self.pending
        if pend is None or self.pos is not None or ts.hour not in ENTRY_HOURS:
            return evs
        side, entry, stop = pend["side"], pend["entry"], pend["stop"]
        dist = entry - stop if side == 1 else stop - entry
        touched = bar["ask_low"] <= entry if side == 1 else bar["ask_high"] >= entry
        if not touched:
            return evs
        if dist <= 0:
            self.pending = None             # dead order: consumed, no trade
            return [{"kind": "reject", "side": side, "entry": entry, "stop": stop,
                     "i": i, "ticket": pend["ticket"]}]
        qty = pend["qty"] if pend["qty"] is not None else self.fill_qty(dist)
        self.pos = {"side": side, "entry": entry, "stop": stop, "target": pend["target"],
                    "qty": qty, "i": i, "ticket": pend["ticket"]}
        self.pending = None
        evs.append({"kind": "fill", "side": side, "entry": entry, "stop": stop,
                    "target": pend["target"], "qty": qty, "i": i,
                    "ticket": self.pos["ticket"]})
        if (side == 1 and bar["bid_low"] <= stop) or (side == -1 and bar["ask_high"] >= stop):
            exit_price = stop - STOP_SLIP if side == 1 else stop + STOP_SLIP
            evs.append({"kind": "exit", "side": side, "entry": entry, "stop": stop,
                        "price": exit_price, "qty": qty, "i": i, "hi": i,
                        "reason": "same-bar stop", "ticket": self.pos["ticket"]})
            self.pos = None
        return evs
