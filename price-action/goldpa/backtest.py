"""Sweep -> Shift -> Retrace backtester for XAUUSD.

Strategy logic lives in strategy.py (shared with the live runner); this file
is only the backtest execution simulation: bid/ask fills, same-bar stop
handling, commission, sizing, metrics.

Usage (from the project root, gold-pa/):
  python -m goldpa.backtest [data_dir]     # backtest, data_dir default ../M5/M5
  python -m goldpa.backtest --selftest     # detector asserts, no data needed
  python -m goldpa.backtest [data_dir] --split   # disjoint time windows
  python -m goldpa.backtest [data_dir] --wf      # walk-forward optimization
  python -m goldpa.backtest [data_dir] --mc      # Monte Carlo drawdown bootstrap
"""
import sys

import numpy as np
import pandas as pd

from . import data
from . import pa
from .simbroker import SimBroker
from .strategy import (Params as P, StrategyState, pool_target, precompute)

# --- backtest execution constants (strategy definitions live in strategy.py) ---
RISK = 0.01           # fraction of equity risked per trade
COMMISSION = 0.0      # $/oz round-trip; broker-cost test sets 0.30 (or 0.60 if per-side)
COMMISSION_PCT = 0.0  # round-turn fraction of entry notional (FTMO metals 0.0014% = 0.000014)
CAPITAL = 10_000.0
BARS_PER_YEAR = 24_192  # ponytail: 4*24*252, ignores weekends; fine for ranking


def commission_of(pos: dict) -> float:
    return (COMMISSION_PCT * pos["entry"] + COMMISSION) * pos["qty"]


def run(m15: pd.DataFrame, h4: pd.DataFrame, trade_from=None, p: P | None = None,
        cache: dict | None = None) -> dict:
    p = p or P()
    if cache is None:
        cache = precompute(m15, h4)
    ev_by_bar, snaps = cache["ev"], cache["snaps"]
    st = StrategyState(p, cache, m15)
    sb = SimBroker(fill_qty=lambda dist: equity * RISK / dist)

    equity = CAPITAL
    trades, curve = [], []

    for i in range(len(m15)):
        bar = m15.iloc[i]

        # --- exits for the open position (stop checked first: conservative) ---
        for ev in sb.manage(i, bar):
            pnl = ev["side"] * (ev["price"] - ev["entry"]) * ev["qty"] - commission_of(ev)
            equity += pnl
            trades.append({"i": ev["i"], "hi": ev["hi"], "pnl": pnl,
                           "r": pnl / (abs(ev["entry"] - ev["stop"]) * ev["qty"])})

        # --- strategy core: setup machine, then fills, then re-arm
        #     (this exact order is what the validated backtest semantics require) ---
        evs = ev_by_bar.get(i, [])
        act = st.advance(i, bar, evs, flat=sb.pos is None)

        if act and act.kind == "place":
            dist = abs(act.entry - act.stop)
            target = pool_target(act.side, act.entry, dist, snaps[i], p)
            sb.place(act.side, act.entry, act.stop, target)
        elif act and act.kind == "cancel":
            sb.cancel_pending()

        for ev in sb.try_fill(i, bar, m15.index[i]):
            if ev["kind"] in ("fill", "reject"):
                st.on_filled()
            elif ev["kind"] == "exit":
                pnl = ev["side"] * (ev["price"] - ev["entry"]) * ev["qty"] - commission_of(ev)
                equity += pnl
                r = -1.0 if ev["reason"] == "same-bar stop" else \
                    pnl / (abs(ev["entry"] - ev["stop"]) * ev["qty"])
                trades.append({"i": ev["i"], "hi": ev["hi"], "pnl": pnl, "r": r})

        # --- arm new setups from this bar's sweeps (after fills, as validated) ---
        st.arm(i, bar, evs, may_arm=sb.pos is None and (trade_from is None or m15.index[i] >= trade_from))

        pos = sb.pos
        unreal = pos["side"] * (bar["close"] - pos["entry"]) * pos["qty"] if pos else 0.0
        curve.append(equity + unreal)

    return _metrics(pd.Series(curve, index=m15.index), trades, st, sb.pos)


def _metrics(equity: pd.Series, trades: list, st=None, pos=None) -> dict:
    ret = equity.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(BARS_PER_YEAR)) if ret.std() > 0 else 0.0
    dd = float((equity / equity.cummax() - 1).min())
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    _m = {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": pf,
        "total_return": float(equity.iloc[-1] / CAPITAL - 1),
        "max_drawdown": dd,
        "sharpe": sharpe,
        "equity": equity,
        "trades_list": trades,
    }
    # live-runner warmup: replay a window through run() and adopt the end state
    _m["state"] = st
    _m["pos_open"] = pos is not None
    return _m


def selftest():
    # sweep: bar1 is a pivot high (104), bar3 wicks to 105 but closes 103.5
    idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "open": [100.0, 100.5, 103.8, 101.8, 103.0],
        "high": [101.0, 104.0, 103.9, 105.0, 103.5],
        "low": [99.0, 100.2, 101.5, 101.7, 102.0],
        "close": [100.5, 103.8, 101.8, 103.0, 103.2],
    }, index=idx)
    evs = [e.type for e in pa.scan(df, left=1, right=1)]
    assert "sweep_high" in evs, f"sweep not detected: {evs}"

    # FVG: bar2 low 102 > bar0 high 101 -> bull gap
    df2 = pd.DataFrame({
        "open": [100.0, 102.0, 103.0], "high": [101.0, 102.5, 104.0],
        "low": [99.0, 101.5, 102.0], "close": [100.5, 102.2, 103.5],
    }, index=idx[:3])
    fvgs = pa.find_fvgs(df2)
    assert fvgs and fvgs[0].kind == "bull" and fvgs[0].bottom == 101.0, fvgs
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)

    data_dir = next((a for a in sys.argv[1:] if not a.startswith("-")), "../M5/M5")
    m15 = data.load_m15(data_dir)
    h4 = data.resample_h4(m15)
    print(f"{data_dir}: {len(m15)} M15 bars  {m15.index[0]} -> {m15.index[-1]}")

    if "--split" in sys.argv:
        wins = [("develop  2021-2023", "2021-01-01", "2024-01-01"),
                ("validate 2024-2025", "2024-01-01", "2026-01-01"),
                ("test     2026    ", "2026-01-01", None)]
        print(f"{'window':20}{'trades':>7}{'win':>7}{'PF':>6}{'return':>9}{'maxDD':>8}{'sharpe':>8}")
        for name, s, e in wins:
            tz = m15.index.tz
            warm = str((pd.Timestamp(s) - pd.Timedelta(days=90)).date())
            m = m15.loc[warm:e]
            r = run(m, data.resample_h4(m15.loc[:e] if e else m15),
                    trade_from=pd.Timestamp(s).tz_localize(tz))
            print(f"{name}{r['trades']:>7}  {r['win_rate']:>6.1%} {r['profit_factor']:>6.2f} "
                  f"{r['total_return']:>8.1%} {r['max_drawdown']:>7.1%} {r['sharpe']:>8.2f}")
        sys.exit(0)

    if "--wf" in sys.argv:   # walk-forward: 12m train -> 6m out-of-sample, rolling
        grid = [P(a, b, c, pool) for a in (24, 48) for b in (24, 48)
                for c in (1.5, 2.0, 3.0) for pool in (False, True)]
        folds = pd.date_range("2022-04-01", "2026-04-01", freq="6MS", tz=m15.index.tz)
        oos_r: list[float] = []
        print(f"{'fold':>10} {'cw zv rr pool':>14} {'trSharpe':>9} {'OOS n':>6} {'OOS PF':>8} {'OOS R':>8}")
        for f in folds:
            tr_s = str((f - pd.DateOffset(months=12) - pd.Timedelta(days=90)).date())
            tr_e, te_e = str(f.date()), str((f + pd.DateOffset(months=6)).date())
            warm_s = str((f - pd.Timedelta(days=90)).date())
            train, test = m15.loc[tr_s:tr_e], m15.loc[warm_s:te_e]
            tc = precompute(train, data.resample_h4(m15.loc[:tr_e]))
            best, best_s = None, -9.9
            for cand in grid:   # pick params by train Sharpe (return per unit pain)
                r = run(train, None, None, cand, cache=tc)
                if r["sharpe"] > best_s:
                    best_s, best = r["sharpe"], cand
            sc = precompute(test, data.resample_h4(m15.loc[:te_e]))
            r = run(test, None, trade_from=f, p=best, cache=sc)
            oos_r += [t["r"] for t in r["trades_list"]]
            tag = f"{best.confirm_within} {best.zone_valid} {best.rr} {'P' if best.pool else 'F'}"
            print(f"{str(f.date()):>10} {tag:>14} {best_s:>9.2f} {r['trades']:>6} "
                  f"{r['profit_factor']:>8.2f} {sum(t['r'] for t in r['trades_list']):>8.1f}")
        r_arr = np.array(oos_r)
        wins, losses = r_arr[r_arr > 0], r_arr[r_arr <= 0]
        eq = np.cumprod(1 + 0.01 * r_arr)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        print(f"\ncombined OOS: {len(r_arr)} trades  PF {wins.sum()/abs(losses.sum()):.2f}  "
              f"expectancy {r_arr.mean():.3f}R  compounded {eq[-1]-1:.1%}  maxDD {dd:.1%}")
        sys.exit(0)

    if "--mc" in sys.argv:    # Monte Carlo: bootstrap trade R sequence, DD distribution
        r = run(m15, h4)
        rs = np.array([t["r"] for t in r["trades_list"]])
        rng = np.random.default_rng(7)
        sims = rng.choice(rs, size=(1000, len(rs)), replace=True)
        eq = np.cumprod(1 + 0.01 * sims, axis=1)
        dds = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
        finals = eq[:, -1] - 1
        print(f"based on {len(rs)} trades, expectancy {rs.mean():.3f}R")
        print(f"maxDD  median {np.median(dds):.1%}  mean {dds.mean():.1%}  "
              f"worst-5% {np.percentile(dds, 5):.1%}")
        print(f"final  median {np.median(finals):.1%}  P(loss) {(finals < 0).mean():.1%}")
        sys.exit(0)

    r = run(m15, h4)
    print(f"trades {r['trades']}  win {r['win_rate']:.1%}  PF {r['profit_factor']:.2f}  "
          f"return {r['total_return']:.1%}  maxDD {r['max_drawdown']:.1%}  sharpe {r['sharpe']:.2f}")
