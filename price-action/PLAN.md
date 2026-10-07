# gold-pa Implementation Plan — FTMO via MT5

System: Sweep → Shift → Retrace (H4 bias, M15 setups, FVG + OB fallback, both sides).
Validated: walk-forward OOS PF 1.43 @ FTMO costs, maxDD −5.7% at 0.3% risk.
Deployment: Windows + MT5 (multiple terminals installed → every account is config with its own terminal path).
Dev box: macOS (backtesting only).

## Config model (Phase 2, used by everything after)

`config.json` (never synced anywhere, contains account password). One flat account —
moving from trial/demo to the paid challenge only changes `login`, `password`, `server`:

```json
{
  "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "login": 12345678,
  "password": "...",
  "server": "FTMO-Server",
  "symbol": "XAUUSD",
  "telegram": { "bot_token": "...", "chat_id": "..." }
}
```

Runner always `mt5.initialize(path=<terminal_path>, login=..., server=...)` — one process
per terminal; point it at whichever terminal64.exe belongs to the active account.

## Phase 0 — Environment (user, ~1 session)
- FTMO account: start with the free trial (or demo) before any paid challenge.
- Windows: Python 3.11+, `pip install -r requirements.txt`, MT5 terminals logged in once manually
  (so credentials/timing are cached), note each terminal64.exe path into config.json.
- Exit: `python -c "import MetaTrader5; print(MetaTrader5.initialize())"` attaches to the chosen terminal.

## Phase 1 — Extract the shared strategy core (mac, ~1 session) — **DONE 2026-10-06**
- Pull the per-bar setup state machine out of `backtest.run()` into `strategy.py`
  (`StrategyState.advance()` → fills → `StrategyState.arm()`; that call order is itself part of
  the validated semantics — a sweep on the fill bar must be able to arm). Definitions moved:
  Params, stops/zones/targets, h4_bias, precompute. **No logic change.**
- Exit: selftest OK + full-sample metrics byte-identical — **met**: 1887 trades / PF 1.29 /
  +4011.2% / DD −24.7% / Sharpe 2.28, split windows 1.41/1.32/1.20 all exact.
  (First attempt failed the gate: arm-before-fill ordering bug — caught by regression, fixed.)

## Phase 2 — MT5 adapter (mac-dev against a Windows terminal or via remote, ~1–2 sessions) — **CODE BUILT 2026-10-06 (`mt5_client.py`), untested on a real broker**
- `connect(cfg)` initializes by config terminal_path/login/server; `fetch_bars` pulls M15 as the
  exact data.py schema (bid candles + spread column → ask/mid rebuild); `spec` reads point,
  volume min/step/max, contract size, filling mode; `lots_for` sizing; `place_limit` (SL/TP
  attached), `cancel`, `pendings`, `positions`, `equity`, `flatten`.
- **Feed validation (pending, needs Windows):** pull FTMO XAUUSD history → `backtest.py` on it →
  compare vs Dukascopy (PF within ~10%).

## Phase 3 — Live runner (Windows, ~2 sessions) — **CODE BUILT 2026-10-06 (`runner.py`, `notify.py`, `wfo.py`), broker testing pending**
- `python -m goldpa.runner --once` (Task Scheduler every 15 min) or `--loop`; `--replay` smoke mode for Mac.
- State model: broker = truth (pending = working setup, position = filled setup). On every start
  the warmup window is replayed through **backtest.run() itself** and the end state adopted —
  one code path, zero duplicated logic. Per bar: `advance()` → place/cancel at broker →
  fill-sync → `arm()` (validated order). JSONL journal (`journal/runner.jsonl`).
- `notify.py`: Telegram via stdlib HTTP; announces place/fill/close/halts/errors; silent no-op
  without bot_token.
- `wfo.py`: re-optimizes params on trailing 12m (`python -m goldpa.wfo --source mt5`), writes `params.json`.
- Exit: 24h+ unattended on demo without crash or unexplained missed signal.

## Phase 4 — Risk interlocks (Windows, ~1 session) — **CODE BUILT 2026-10-06 (inside `runner.py`), trigger tests pending**
- Daily-loss tracker: self-limit **−2%** day PnL → flat + no new entries until next day
  (FTMO hard limit 5%; we fail at half).
- Total-DD tracker: self-limit **−6%** from challenge-start equity → kill (FTMO 10%).
- Friday-flat: close all ~20:00 UTC Friday (weekend gap risk; 29% of trades hold overnight).
- Kill switch: `STOP` file in the run dir halts entries immediately.
- Sizing: **0.3% risk per trade** (MC worst-5% DD −9% < FTMO 10%).
- Exit: interlock unit checks + one deliberate trigger on demo.

## Phase 5 — Dry run (FTMO free trial / demo, 2–4 weeks, mostly waiting)
- Runner live on trial account. Weekly: diff runner journal vs offline backtest on the same bars
  (missed signals, fill slippage vs touch=fill, spread reality vs 0.20 assumption, swap cost check).
- Exit: live PF ≥ 50% of backtest PF, fills within tolerance, zero unexplained misses.
  Fail → fix modeling, re-run phase 5. **No paid challenge before this passes.**

## Phase 6 — Challenge (paid, ~5 months at ~1 trade/day)
- 0.3% risk, 10% target ≈ ~137 trades; re-opt params monthly from trailing 12m.
- Swap: verify FTMO XAUUSD swap rates; holding median 2.8h / 29% overnight — small but unmodeled.
- Exit: funded. (Verification phase: same, 5% target.)

## Phase 7 — Funded ops (ongoing)
- Monthly: refresh validation on rolling data; quarterly: re-run gauntlet (`--split --wf --mc`).
- Drift alarm: live expectancy < 0 for 30 consecutive trades → pause, re-validate.

## Skipped (add when needed)
Django/any web framework (no web surface; a read-only journal page can be added later if ever
wanted), Docker/service wrappers (Task Scheduler + one loop is enough), multi-symbol support.
Telegram is IN scope (user approved — Phase 3 `notify.py`) but read-only: notifications only,
no remote-control commands.
