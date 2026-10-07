# price-action — XAUUSD (forex gold) auto trading system

A fully local, price-action auto trading system for forex gold (XAUUSD),
built to trade an **FTMO challenge via MetaTrader 5 on Windows**, developed
and backtested on macOS.

## What it does

**Strategy — "Sweep → Shift → Retrace"** (H4 bias + M15 setups, long and short):

1. **Bias** — H4 market structure (breaks of swing highs/lows) sets the only
   allowed trade direction.
2. **Sweep** — on M15, price wicks through a confirmed swing level (where
   stop losses rest) but *closes back inside* it: a liquidity grab / stop
   hunt. The manipulation is the signal.
3. **Shift** — price then closes through the opposite swing (BOS/CHoCH)
   within 8 hours: proof the direction actually flipped.
4. **Retrace entry** — a limit order rests at the fair-value-gap (or order
   block) left by the displacement leg; price returning there fills it.
5. **Stop** beyond the sweep wick (already-hunted territory); **target** at
   2–3× the stop distance (liquidity-pool targets available).
6. **Risk** — 0.3% of equity per trade; every parameter re-optimized
   periodically from trailing data (walk-forward).

Every definition (swing, sweep, zone, target) is pinned mechanically in code —
nothing is read off a chart by eye.

## Validated performance (Dukascopy data 2021-01 → 2026-08, M15, ~165k bars)

| test | result |
|---|---|
| Full sample | 1,887 trades · PF 1.29 · maxDD −24.7% |
| Time split (3 windows) | profitable in all: PF 1.41 / 1.32 / 1.20 |
| Walk-forward (48-param grid, 9 folds, out-of-sample) | PF 1.41 · expectancy +0.235R |
| Walk-forward @ FTMO costs (spread $0.20 + metals commission) | **PF 1.43 · +0.243R · maxDD −5.7% @ 0.3% risk** |
| Monte Carlo (1,000× bootstrap) | worst-5% maxDD −9% @ 0.3% risk → fits FTMO's 10% rule |

Passing bars (pre-registered): PF > 1.3 out-of-sample ✓ · maxDD < 20% ✓ · > 100 trades ✓.

## Layout

```
price-action/
├── goldpa/                  all Python code (one package)
│   ├── pa.py                price-action detectors (swings, sweeps, BOS/CHoCH, FVGs)
│   ├── strategy.py          shared strategy core — ONE state machine for backtest & live
│   ├── simbroker.py         fill simulation (shared by backtester and dry runs)
│   ├── backtest.py          backtester: full / --split / --wf / --mc / --selftest
│   ├── data.py              Dukascopy parquet loader (M15 mid + bid/ask)
│   ├── mt5_client.py        MT5 adapter: connect-by-path, bars, spec, sizing, orders
│   ├── runner.py            live runner (--once / --loop / --dry / --replay)
│   ├── notify.py            Telegram notifications (stdlib HTTP)
│   └── wfo.py               walk-forward re-optimization → params.json
├── PLAN.md                  phase-by-phase build & rollout plan
├── config.example.json      copy to config.json, fill in your account
├── params.json              current deployment parameters (written by wfo.py)
├── requirements.txt
└── journal/                 runtime output (gitignored): event log + trade ledger
    ├── runner.jsonl         every event: place, fill, close, reject, interlock, heartbeat
    └── trades.jsonl         one complete record per closed trade (entry/exit/PnL/reason)
```

## What gets recorded

- **`journal/runner.jsonl`** — every event in order: heartbeat (equity), order
  placed (entry/SL/TP/lots), fill, position closed, rejections, interlock
  triggers, Telegram messages.
- **`journal/trades.jsonl`** — one line per closed trade: open/close time,
  side, entry, SL, TP, lots, exit price, **PnL** (from MT5 deal history live,
  simulated on dry runs), and the exit reason (stop / target).

## Dry run & paper mode — the full workflow, no orders, no risk

**Paper mode** — live demo-account data, no orders sent (run on Windows with
`config.json` filled in):

```bash
python -m goldpa.runner --paper --loop     # long-running; or --once per 15-min bar
```

Pulls live M15 bars from your MT5 demo account every 15 minutes and runs the
complete workflow — signals, simulated order placement, simulated fills,
exits, interlocks, journal, Telegram — without sending a single order to the
broker. Fills are simulated on the live bars with the same validated fill
simulator as the backtester; interlock flattens act on the simulated book.
Paper state (pending orders, open position, paper equity) persists across
restarts in `journal/runner.jsonl`. This is the rehearsal on real live data
before the `--once` live mode.

**Dry run** — the same workflow over *historical* data (Mac, no account):

```bash
python -m goldpa.runner --dry ../M5/M5
```

Replays history through the identical pipeline and ends with a self-check:
every difference vs the validated backtest must be attributable to interlock
windows (Friday-flat entries dropped) or live sizing constraints (lot minimum
rejects) — anything unexplained is flagged as a bug.

Live/dry have identical order flow: `advance()` → place/cancel → fill-sync →
`arm()`, driven by one shared strategy core.

## Setup — Windows (live / FTMO)

1. **Python 3.11+**, then:
   ```bat
   pip install -r requirements.txt
   ```
2. **FTMO**: start with the free trial (or demo). Log the MT5 terminal in once
   manually; note its `terminal64.exe` full path.
3. **Config**: copy `config.example.json` → `config.json`, fill in
   `terminal_path`, `login`, `password`, `server`, `symbol`. Moving from
   trial/demo to the paid challenge only changes login/password/server.
   Add your Telegram bot token + chat id to get every notification.
4. **Parameters**: `python -m goldpa.wfo --source mt5` (trailing 12 months)
   — or keep the shipped `params.json`. Re-run every 6 months (3 during a
   challenge).
5. **Dry check on the live feed** (optional but recommended): temporarily run
   `--once` with orders disabled by… it isn't — instead run the feed
   validation: pull FTMO history, backtest it, compare PF vs Dukascopy
   (within ~10% expected).
6. **Go**: Task Scheduler → every 15 minutes →
   ```bat
   python -m goldpa.runner --once
   ```
   (or `python -m goldpa.runner --loop` in a terminal). Watch Telegram and
   `journal/`.
7. **Kill switch**: create a file named `STOP` in this folder — entries halt
   immediately and positions are flattened.

## Safety rails (built in)

| rail | setting | vs FTMO |
|---|---|---|
| risk per trade | 0.3% | sized so MC worst-5% DD −9% < 10% max loss |
| daily loss self-limit | −2% → flat + halt for the day | FTMO fails at −5% |
| total drawdown self-limit | −6% → kill switch | FTMO fails at −10% |
| weekend gap | flat after 20:00 UTC Friday | — |
| manual kill | `STOP` file | — |

## Setup — macOS (development / backtesting only)

```bash
python3 -m venv .venv && .venv/pip install pandas numpy pyarrow
# MetaTrader5 is Windows-only and excluded from installs on macOS
python -m goldpa.backtest ../M5/M5 --split     # time-split validation
python -m goldpa.backtest ../M5/M5 --wf        # walk-forward
python -m goldpa.runner --dry ../M5/M5         # dry run over history
```

## Honest caveats

- Backtest fills assume touch = fill on limit orders; real fills can be
  worse. The dry run + FTMO trial phase exist to measure exactly this.
- Swap (overnight financing) is not modeled; ~29% of trades hold overnight.
  Verify FTMO's XAUUSD swap rates before the paid challenge.
- The edge decays over time (PF 1.41 → 1.20 across 2021→2026 windows) —
  the walk-forward re-optimization cadence is part of the system, not optional.
- Prop-firm rules and commission rates change: verify current FTMO terms
  yourself before relying on them.
