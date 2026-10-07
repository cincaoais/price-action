"""Price-action detectors for XAUUSD. Point-in-time safe: a swing is only
visible `right` bars after it forms, so bar i uses nothing from the future."""
from dataclasses import dataclass

import pandas as pd


@dataclass
class Swing:
    index: int        # bar where the pivot formed
    confirmed: int    # first bar it was knowable
    price: float
    kind: str         # "high" (buyside liquidity) | "low" (sellside)


@dataclass
class FVG:
    index: int
    kind: str         # "bull" | "bear"
    bottom: float
    top: float


@dataclass
class Event:
    bar: int
    type: str         # sweep_low | sweep_high | bos_up | bos_down | choch_up | choch_down
    price: float
    ref: int = -1     # bar of the swing involved


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def raw_swings(df: pd.DataFrame, left: int, right: int) -> list[Swing]:
    """Fractal pivots: strictly the extreme of the (left+right+1) window."""
    h, l = df["high"].values, df["low"].values
    out = []
    for i in range(left, len(df) - right):
        wh, wl = h[i - left:i + right + 1], l[i - left:i + right + 1]
        if h[i] >= wh.max() and (wh == h[i]).sum() == 1:
            out.append(Swing(i, i + right, float(h[i]), "high"))
        if l[i] <= wl.min() and (wl == l[i]).sum() == 1:
            out.append(Swing(i, i + right, float(l[i]), "low"))
    return out


def scan(df: pd.DataFrame, left: int = 2, right: int = 2,
         snaps: dict | None = None) -> list[Event]:
    """Walk bars, emit liquidity sweeps (wick through a confirmed swing,
    close back inside) and structure breaks (CLOSE through a swing):
    with trend = bos, against trend = choch.
    Pass `snaps` to also record, per bar, the pools knowable strictly
    BEFORE that bar (point-in-time liquidity for target selection)."""
    by_confirm: dict[int, list[Swing]] = {}
    for s in raw_swings(df, left, right):
        by_confirm.setdefault(s.confirmed, []).append(s)

    events: list[Event] = []
    highs, lows = [], []      # unswept confirmed swings
    trend = 0
    last_sh: Swing | None = None
    last_sl: Swing | None = None

    for i in range(len(df)):
        if snaps is not None:
            snaps[i] = (tuple(s.price for s in highs), tuple(s.price for s in lows))
        for s in by_confirm.get(i, []):
            (highs if s.kind == "high" else lows).append(s)
            if s.kind == "high" and (last_sh is None or s.index > last_sh.index):
                last_sh = s
            if s.kind == "low" and (last_sl is None or s.index > last_sl.index):
                last_sl = s

        hi, lo, close = df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]

        for s in list(highs):      # wick above, close below = buyside swept
            if hi > s.price and close < s.price:
                s.swept = True
                events.append(Event(i, "sweep_high", s.price, s.index))
                highs.remove(s)
        for s in list(lows):       # wick below, close above = sellside swept
            if lo < s.price and close > s.price:
                s.swept = True
                events.append(Event(i, "sweep_low", s.price, s.index))
                lows.remove(s)

        if last_sh is not None and close > last_sh.price:
            events.append(Event(i, "choch_up" if trend == -1 else "bos_up",
                                last_sh.price, last_sh.index))
            trend = 1
            last_sh = None
        if last_sl is not None and close < last_sl.price:
            events.append(Event(i, "choch_down" if trend == 1 else "bos_down",
                                last_sl.price, last_sl.index))
            trend = -1
            last_sl = None

    return events


def find_fvgs(df: pd.DataFrame) -> list[FVG]:
    """3-candle imbalance: bull FVG at i when low[i] > high[i-2]."""
    h, l = df["high"].values, df["low"].values
    out = []
    for i in range(2, len(df)):
        if l[i] > h[i - 2]:
            out.append(FVG(i, "bull", float(h[i - 2]), float(l[i])))
        elif h[i] < l[i - 2]:
            out.append(FVG(i, "bear", float(h[i]), float(l[i - 2])))
    return out
