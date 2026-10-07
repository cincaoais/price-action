"""Shared strategy core: Sweep -> Shift -> Retrace.

backtest.run() and the live MT5 runner both drive StrategyState.step() once
per closed M15 bar — one state machine, two callers. Definitions (stops,
zones, targets, bias) live here so they cannot diverge between backtest
and live.
"""
from dataclasses import dataclass

import pandas as pd

from . import pa

STOP_ATR = 0.1        # stop buffer beyond the sweep wick, in ATR(14) units
FVG_MIN_ATR = 0.3     # ignore FVGs smaller than this * ATR
ENTRY_HOURS = range(24)  # session gate tested (6-20 UTC): PF 1.29->1.07, DD worse. Off-hours entries carry the edge.


@dataclass(frozen=True)
class Params:
    confirm_within: int = 32   # M15 bars (8h) sweep -> structure break
    zone_valid: int = 48       # M15 bars (12h) a zone stays entryable
    rr: float = 2.0            # minimum target multiple (pool target may exceed it)
    pool: bool = False         # True: target nearest liquidity pool >= rr; False: fixed rr


@dataclass
class Action:
    """What the caller should do with the broker/backtest after step()."""
    kind: str      # "place" | "cancel"
    side: int
    entry: float
    stop: float
    # ponytail: target is NOT in the action — backtest picks it at fill bar,
    # live at place bar; pool_target() below is shared so selection can't diverge.


def h4_bias(h4: pd.DataFrame, m15_index: pd.DatetimeIndex) -> pd.Series:
    """H4 trend after each structure break, ffill'd onto M15. An H4 event is
    only usable once that H4 bar closes, hence the +4h timestamp."""
    close_time = h4.index + pd.Timedelta(hours=4)
    by_bar = {}
    for e in pa.scan(h4):
        if e.type.startswith(("bos", "choch")):
            by_bar[e.bar] = 1 if e.type.endswith("up") else -1
    if not by_bar:
        return pd.Series(0.0, index=m15_index)
    s = pd.Series({close_time[b]: d for b, d in by_bar.items()}).sort_index()
    return s.reindex(s.index.union(m15_index)).ffill().reindex(m15_index).fillna(0.0)


def find_zone(fvgs, a: pd.Series, m15: pd.DataFrame, side: int,
              sweep_bar: int, confirm_bar: int, close: float):
    """Entry zone for the retrace: newest bull FVG in the displacement leg
    (long) sized >= FVG_MIN_ATR*ATR; fallback = last opposite candle (OB).
    Returns (bottom, top, kind) so the journal can record WHY the zone."""
    if side == 1:
        cands = [f for f in fvgs if f.kind == "bull" and sweep_bar < f.index <= confirm_bar
                 and f.top <= close
                 and f.top - f.bottom >= FVG_MIN_ATR * a.iloc[f.index]]
        if cands:
            z = max(cands, key=lambda f: f.index)
            return z.bottom, z.top, "fvg"
        for j in range(confirm_bar, sweep_bar, -1):
            b = m15.iloc[j]
            if b["close"] < b["open"]:
                return b["low"], b["high"], "order_block"
    else:
        cands = [f for f in fvgs if f.kind == "bear" and sweep_bar < f.index <= confirm_bar
                 and f.bottom >= close
                 and f.top - f.bottom >= FVG_MIN_ATR * a.iloc[f.index]]
        if cands:
            z = max(cands, key=lambda f: f.index)
            return z.bottom, z.top, "fvg"
        for j in range(confirm_bar, sweep_bar, -1):
            b = m15.iloc[j]
            if b["close"] > b["open"]:
                return b["low"], b["high"], "order_block"
    return None


def pool_target(side: int, entry: float, dist: float, snaps_i, p: Params) -> float:
    """Nearest resting liquidity pool at least p.rr away; fixed multiple if none
    (or when pool targeting is off)."""
    if not p.pool:
        return entry + p.rr * dist if side == 1 else entry - p.rr * dist
    highs, lows = snaps_i
    if side == 1:
        c = [x for x in highs if x >= entry + p.rr * dist]
        return min(c) if c else entry + p.rr * dist
    c = [x for x in lows if x <= entry - p.rr * dist]
    return max(c) if c else entry - p.rr * dist


def precompute(m15: pd.DataFrame, h4: pd.DataFrame) -> dict:
    """Everything parameter-independent: scan, liquidity snaps, FVGs, ATR, bias."""
    snaps: dict = {}
    ev_by_bar: dict = {}
    for e in pa.scan(m15, snaps=snaps):
        ev_by_bar.setdefault(e.bar, []).append(e)
    return {"ev": ev_by_bar, "snaps": snaps, "fvgs": pa.find_fvgs(m15),
            "atr": pa.atr(m15), "bias": h4_bias(h4, m15.index)}


class StrategyState:
    """Setup lifecycle: armed (sweep seen) -> working (limit resting at the
    zone) -> filled/cancelled/expired. The CALLER fills orders (backtest
    simulates the touch, the broker really fills it) and calls on_filled()."""

    def __init__(self, p: Params, cache: dict, m15: pd.DataFrame):
        self.p = p
        self.fvgs, self.atr, self.bias = cache["fvgs"], cache["atr"], cache["bias"]
        self.m15 = m15
        self.setup = None
        self.working = False   # limit order resting (in broker live, unfilled setup in backtest)

    def has_working_order(self) -> bool:
        return self.working

    def on_filled(self) -> None:
        self.setup = None
        self.working = False

    def advance(self, i: int, bar: pd.Series, evs: list, flat: bool):
        """Setup machine for the tracked setup: armed -> confirm (returns
        "place") or working -> expired (returns "cancel"). Call ONCE per
        closed M15 bar, BEFORE fill handling. `flat`: caller holds no
        position (pauses expiry, matching validated backtest semantics)."""
        act = None
        s = self.setup
        if s is not None and not self.working:
            # armed: waiting for the structure break inside the window
            if i - s["sweep_bar"] > self.p.confirm_within:
                self.setup = None
            else:
                side = s["side"]
                s["extreme"] = min(s["extreme"], bar["low"]) if side == 1 \
                    else max(s["extreme"], bar["high"])
                killed = bar["close"] < s["extreme"] if side == 1 \
                    else bar["close"] > s["extreme"]
                if killed:
                    self.setup = None
                else:
                    want = ("bos_up", "choch_up") if side == 1 else ("bos_down", "choch_down")
                    for e in evs:
                        if e.type in want:
                            zone = find_zone(self.fvgs, self.atr, self.m15, side,
                                             s["sweep_bar"], e.bar, bar["close"])
                            if zone:
                                zbot, ztop, zkind = zone
                                stop = s["extreme"] - STOP_ATR * self.atr.iloc[e.bar] if side == 1 \
                                    else s["extreme"] + STOP_ATR * self.atr.iloc[e.bar]
                                s.update(confirm_bar=e.bar, zone_bottom=zbot, zone_top=ztop,
                                         stop=float(stop), confirm_type=e.type, zone_kind=zkind)
                                self.working = True
                                entry = ztop if side == 1 else zbot
                                act = Action("place", side, float(entry), float(stop))
                            else:
                                self.setup = None
                            break
        elif s is not None and self.working:
            # limit resting; the caller fills it, we only expire it
            if flat and i - s["confirm_bar"] > self.p.zone_valid:
                self.setup = None
                self.working = False
                act = Action("cancel", s["side"], 0.0, 0.0)
        return act

    def arm(self, i: int, bar: pd.Series, evs: list, may_arm: bool) -> None:
        """Arm new setups from sweep events. Call AFTER fill handling so a
        sweep on the fill bar can arm (validated backtest semantics)."""
        if self.setup is None and may_arm:
            for e in evs:
                if e.type == "sweep_low" and self.bias.iloc[i] > 0:
                    self.setup = {"side": 1, "sweep_bar": i, "extreme": bar["low"],
                                  "swept_level": e.price, "confirm_bar": None}
                    break
                if e.type == "sweep_high" and self.bias.iloc[i] < 0:
                    self.setup = {"side": -1, "sweep_bar": i, "extreme": bar["high"],
                                  "swept_level": e.price, "confirm_bar": None}
                    break
