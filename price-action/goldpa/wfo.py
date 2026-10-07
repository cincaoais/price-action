"""Walk-forward re-optimization: trailing 12m train -> pick params by train
Sharpe -> write params.json for the runner. Run every 6 months (3 during the
challenge, per PLAN.md).

  python -m goldpa.wfo --source mt5            # Windows: pull history from the broker
  python -m goldpa.wfo --source parquet --data_dir ../M5/M5   # Mac dev
"""
import json
import sys
from pathlib import Path

import pandas as pd

from . import backtest
from . import data
from . import mt5_client as mt5c
from .strategy import Params, precompute

HERE = Path(__file__).resolve().parent          # package dir
ROOT = HERE.parent                              # project root
GRID = [Params(a, b, c, pool) for a in (24, 48) for b in (24, 48)
        for c in (1.5, 2.0, 3.0) for pool in (False, True)]


def trailing_year_m15(source: str, cfg: dict, data_dir: str) -> pd.DataFrame:
    if source == "mt5":
        mt5 = mt5c.connect(cfg)
        return mt5c.fetch_bars(mt5, cfg.get("symbol", "XAUUSD"), "M15", 35_000)
    m15 = data.load_m15(data_dir)
    return m15.iloc[-34_000:]                      # ponytail: ~12 months of M15 bars


def main() -> None:
    source = "mt5" if "--source" not in sys.argv else sys.argv[sys.argv.index("--source") + 1]
    data_dir = next((a for a in sys.argv[1:] if not a.startswith("-")
                     and a != source and a != "--data_dir" and not a.startswith("--")), "../M5/M5")
    cfg = json.loads((ROOT / "config.json").read_text()) if source == "mt5" else {}
    m15 = trailing_year_m15(source, cfg, data_dir)
    train = m15.iloc[-34_000:]                     # ~12 months of M15 bars
    print(f"train window: {train.index[0]} -> {train.index[-1]}  ({len(train)} bars)")

    best, best_s = None, -9.9
    tc = precompute(train, data.resample_h4(train))
    for cand in GRID:
        r = backtest.run(train, None, None, cand, cache=tc)
        if r["sharpe"] > best_s:
            best_s, best = r["sharpe"], cand
        print(f"  {cand.confirm_within}/{cand.zone_valid}/{cand.rr}/"
              f"{'P' if cand.pool else 'F'}  sharpe {r['sharpe']:.2f}", flush=True)
    print(f"best: {best}  train sharpe {best_s:.2f}")
    (ROOT / "params.json").write_text(json.dumps({
        "confirm_within": best.confirm_within, "zone_valid": best.zone_valid,
        "rr": best.rr, "pool": best.pool, "trained_through": str(train.index[-1])}, indent=2))
    print("params.json written")


if __name__ == "__main__":
    main()
