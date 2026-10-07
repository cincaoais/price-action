"""Dukascopy parquet dir -> M15 OHLC (mid) + bid/ask extremes for fills, H4 resample."""
from pathlib import Path

import pandas as pd

MID_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}


def load_m15(data_dir: str, spread: float | None = None) -> pd.DataFrame:
    """spread=None uses Dukascopy's real bid/ask; a number rebuilds a synthetic
    constant spread around the mid (your-broker cost modeling)."""
    files = sorted(Path(data_dir).glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files in {data_dir}")
    raw = pd.concat([pd.read_parquet(f) for f in files]).sort_index()
    mids = pd.DataFrame({
        "open": (raw["o_bid"] + raw["o_ask"]) / 2,
        "high": (raw["h_bid"] + raw["h_ask"]) / 2,
        "low": (raw["l_bid"] + raw["l_ask"]) / 2,
        "close": (raw["c_bid"] + raw["c_ask"]) / 2,
    })
    if spread is None:
        mids["bid_low"], mids["bid_high"] = raw["l_bid"], raw["h_bid"]
        mids["ask_low"], mids["ask_high"] = raw["l_ask"], raw["h_ask"]
    else:
        h = spread / 2
        mids["bid_low"], mids["bid_high"] = mids["low"] - h, mids["high"] - h
        mids["ask_low"], mids["ask_high"] = mids["low"] + h, mids["high"] + h
    return mids.resample("15min").agg({
        **MID_AGG, "bid_low": "min", "bid_high": "max", "ask_low": "min", "ask_high": "max",
    }).dropna()


def resample_h4(m15: pd.DataFrame) -> pd.DataFrame:
    return m15[["open", "high", "low", "close"]].resample("4h").agg(MID_AGG).dropna()
