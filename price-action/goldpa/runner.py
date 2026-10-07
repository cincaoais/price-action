"""Live runner — MT5 on Windows. See PLAN.md phases 2-5.

State model: the BROKER holds the truth (pending order = working setup,
position = filled setup); the strategy state is rebuilt deterministically on
every start by replaying the warmup window through backtest.run() — one code
path with the backtest, zero duplicated logic.

Every trade is recorded twice: event-by-event in journal/runner.jsonl, and
one complete record per closed trade in journal/trades.jsonl (real PnL from
MT5 deal history on live; simulated PnL on dry runs).

Usage (from the project root):
  python -m goldpa.runner --once              # latest closed M15 bar (Task Scheduler)
  python -m goldpa.runner --loop              # long-running: wake at each M15 close
  python -m goldpa.runner --paper [--loop]    # PAPER: live data from the MT5 demo account,
                                              #   full workflow, NO orders sent — behaviour
                                              #   recorded only, fills simulated on live bars
  python -m goldpa.runner --dry ../M5/M5      # DRY: same but over historical parquet data,
                                              #   ends with a self-check vs the backtest
  python -m goldpa.runner --replay ../M5/M5   # quick single-cycle smoke test
"""
import json
import sys
import time
from pathlib import Path

import pandas as pd

from . import data
from . import mt5_client as mt5c
from . import notify
from .simbroker import SimBroker
from .strategy import Params, StrategyState, pool_target, precompute

HERE = Path(__file__).resolve().parent          # package dir
ROOT = HERE.parent                              # project root: config, params, journal, STOP
JOURNAL = ROOT / "journal"
STOP_FILE = ROOT / "STOP"
WARMUP = 3000          # M15 bars fetched per cycle (~31 days) — state rebuild + bias history
RISK = 0.003           # 0.3% per trade — FTMO-fit (MC worst-5% DD -9% < 10% rule)
MAX_DAILY_LOSS = 0.02  # self-limit, half of FTMO's 5%
MAX_TOTAL_DD = 0.06    # self-limit from challenge-start equity, vs FTMO 10%
FRIDAY_FLAT_HOUR = 20  # UTC hour on Friday after which everything is flattened
PAPER_START = 10_000.0             # dry-run starting equity
PAPER_COMMISSION_PCT = 0.000014    # dry-run round-turn commission, FTMO metals style


def load_cfg() -> dict:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"missing {cfg_path} (copy config.example.json)")
    return json.loads(cfg_path.read_text())


def load_params() -> Params:
    p = ROOT / "params.json"
    if not p.exists():
        return Params()          # defaults until first wfo.py run
    d = json.loads(p.read_text())
    return Params(d["confirm_within"], d["zone_valid"], d["rr"], d["pool"])


def jwrite(kind: str, rec: dict) -> None:
    JOURNAL.mkdir(exist_ok=True)
    (JOURNAL / "runner.jsonl").open("a").write(
        json.dumps({"kind": kind, **rec}, default=str) + "\n")


def twrite(rec: dict) -> None:
    """One complete record per closed trade."""
    JOURNAL.mkdir(exist_ok=True)
    (JOURNAL / "trades.jsonl").open("a").write(json.dumps(rec, default=str) + "\n")


class Runner:
    def __init__(self, mode: str, cfg: dict | None):
        self.mode = mode                      # once | loop | dry | paper | replay
        self.cfg = cfg or {}
        self.symbol = self.cfg.get("symbol", "XAUUSD")
        self.params = load_params()
        live = mode in ("once", "loop")       # orders allowed
        paperish = mode in ("dry", "paper")   # simulated fills, no orders
        self.mt5 = mt5c.connect(cfg) if (live or mode == "paper") else None
        self.sp = mt5c.spec(self.mt5, self.symbol) if self.mt5 else {
            "digits": 2, "point": 0.01, "volume_min": 0.01,
            "volume_step": 0.01, "volume_max": 100.0, "contract_size": 100.0, "filling": 0}
        self.pos_ticket = None
        self.open_info = None                 # dict at fill: ts/side/entry/sl/tp/lots
        self.halt_day = None
        self.told = {}                        # one-shot telegram guards
        self.paper_equity = PAPER_START       # dry/paper mode
        self.start_equity = PAPER_START
        self.day_start = None
        self.cur_day = None
        self.sim = SimBroker() if paperish else None
        self.blocked_bars = []                # dry: bars where an interlock blocked entries
        self.ctx = None                       # (win, i, bar) for interlock flatten in paper

    # ---------- journal / telegram ----------
    def tg(self, key: str, text: str, once: bool = False) -> None:
        if once and self.told.get(key):
            return
        self.told[key] = True
        jwrite("tg", {"key": key, "text": text})
        notify.send(self.cfg, f"[price-action] {text}")

    # ---------- data ----------
    def window(self) -> pd.DataFrame:
        if self.mode == "replay":
            return data.load_m15(ROOT / self.cfg.get("data_dir", "../M5/M5"))
        if self.mode == "dry":
            return data.load_m15(ROOT / self.cfg.get("data_dir", "../M5/M5"))
        return mt5c.fetch_bars(self.mt5, self.symbol, "M15", WARMUP)

    # ---------- interlocks (True = new entries blocked) ----------
    # dry: flatten triggers are recorded as would_* only (pure rehearsal).
    # paper: flatten acts on the SIMULATED book (cancel paper pending, close
    #        paper position at bar close) — behaviour is real, money is not.
    def blocked(self, ts, equity: float, day_start: float, start_equity: float) -> bool:
        if STOP_FILE.exists():
            self.br_flatten(ts, "STOP file")
            self.tg("stop", "STOP file present — flattened, entries halted", once=True)
            return True
        if ts.weekday() == 4 and ts.hour >= FRIDAY_FLAT_HOUR:
            self.br_flatten(ts, "Friday flat")
            self.tg("friday", "Friday flat — weekend gap protection", once=True)
            return True
        if equity <= day_start * (1 - MAX_DAILY_LOSS):
            self.br_flatten(ts, "daily loss")
            self.tg("daily", f"daily loss limit hit ({MAX_DAILY_LOSS:.0%}) — flat, halted today",
                    once=True)
            self.halt_day = ts.date()
            return True
        if start_equity and equity <= start_equity * (1 - MAX_TOTAL_DD):
            if self.mode in ("dry", "paper"):
                jwrite("would_dd_kill", {"ts": ts, "equity": equity})
                self.tg("dd", f"would hit TOTAL DD limit ({MAX_TOTAL_DD:.0%})", once=True)
            else:
                STOP_FILE.write_text(f"total drawdown breach {ts}")
                self.tg("dd", f"TOTAL DD limit hit ({MAX_DAILY_LOSS:.0%}) — STOP file written",
                        once=True)
            return True
        return (self.halt_day == ts.date()) if self.halt_day else False

    def br_flatten(self, ts, why: str) -> None:
        if self.mode == "dry":
            jwrite("would_flatten", {"ts": ts, "why": why})
            return
        if self.mode == "paper" and self.sim is not None:
            win, i, bar = self.ctx
            self.sim.cancel_pending()
            for ev in self.sim.force_close(i, float(bar["close"])):
                self.record_dry_exit(ev, win)
                jwrite("paper_flatten", {"ts": ts, "why": why})
            return
        mt5c.flatten(self.mt5, self.symbol, self.sp)

    # ---------- one closed bar ----------
    def process(self, i: int, win: pd.DataFrame, st, cache) -> None:
        bar = win.iloc[i]
        ts = win.index[i]
        paperish = self.sim is not None
        self.ctx = (win, i, bar)
        equity = self.paper_equity if paperish else (
            mt5c.equity(self.mt5) if self.mt5 else PAPER_START)

        day_start, start_equity = self.baselines(equity, ts)
        if self.mode != "dry":
            jwrite("heartbeat", {"ts": ts, "equity": equity, "mode": self.mode})

        evs = cache["ev"].get(i, [])

        # exits first (paperish: simulated; live: broker already executed them)
        if paperish:
            for ev in self.sim.manage(i, bar):
                self.record_dry_exit(ev, win)

        act = st.advance(i, bar, evs, flat=self.pos_ticket is None)

        blocked = self.blocked(ts, equity, day_start, start_equity)
        if blocked:
            self.blocked_bars.append(i)

        # place / cancel
        if act and act.kind == "place" and not blocked:
            dist = abs(act.entry - act.stop)
            lots = mt5c.lots_for(self.sp, equity, RISK, dist)
            if lots <= 0:
                jwrite("reject", {"ts": ts, "why": "lots<min", "dist": dist})
            else:
                target = pool_target(act.side, act.entry, dist, cache["snaps"][i], self.params)
                if paperish:
                    ticket = self.sim.place(act.side, act.entry, act.stop, target, qty=lots)
                else:
                    ticket = mt5c.place_limit(self.mt5, self.symbol, self.sp, act.side,
                                              act.entry, act.stop, target, lots,
                                              f"gp cfm={win.index[st.setup['confirm_bar']]}")
                s = st.setup
                signal = {
                    "bias": "h4_up" if cache["bias"].iloc[i] > 0 else "h4_down",
                    "sweep_ts": str(win.index[s["sweep_bar"]]),
                    "swept_level": s.get("swept_level"),       # the liquidity pool taken
                    "sweep_wick": s["extreme"],                # extreme of the stop hunt
                    "confirm_ts": str(win.index[s["confirm_bar"]]),
                    "confirm_event": s.get("confirm_type"),    # bos_up/choch_up/...
                    "zone_kind": s.get("zone_kind"),           # fvg | order_block
                    "zone": [s.get("zone_bottom"), s.get("zone_top")],
                }
                jwrite("signal", {"ts": ts, "side": act.side, **signal,
                                  "entry": act.entry, "sl": act.stop, "tp": target,
                                  "lots": lots})
                self.open_info = {"ts": ts, "side": act.side, "entry": act.entry,
                                  "sl": act.stop, "tp": target, "lots": lots, **signal}
                jwrite("place", {"ts": ts, "ticket": ticket, "side": act.side,
                                 "entry": act.entry, "sl": act.stop, "tp": target,
                                 "lots": lots, "mode": self.mode})
                self.tg("place", f"[{self.mode}] LIMIT {'BUY' if act.side == 1 else 'SELL'} "
                                 f"{lots} {self.symbol} @ {act.entry} sl {act.stop} tp {target}")
        elif act and act.kind == "cancel":
            if paperish:
                self.sim.cancel_pending()
                jwrite("cancel", {"ts": ts, "why": "zone expired"})
            else:
                for o in mt5c.pendings(self.mt5, self.symbol):
                    mt5c.cancel(self.mt5, o.ticket)
                    jwrite("cancel", {"ts": ts, "ticket": o.ticket})

        # fills
        if paperish:
            for ev in self.sim.try_fill(i, bar, ts):
                if ev["kind"] in ("fill", "reject"):
                    st.on_filled()
                if ev["kind"] == "fill":
                    self.pos_ticket = ev["ticket"]
                    self.open_info.update({"ts": ts, "lots": ev["qty"]})
                    jwrite("fill", {"ts": ts, "ticket": ev["ticket"], "price": ev["entry"]})
                    self.tg("fill", f"[{self.mode}] FILLED {self.symbol} @ {ev['entry']}")
                elif ev["kind"] == "reject":
                    jwrite("reject", {"ts": ts, "why": "dist<=0 at touch"})
                elif ev["kind"] == "exit":
                    self.record_dry_exit(ev, win)
        elif self.mt5:
            self.sync_live(st, ts, equity)

        # arm AFTER fills, as validated
        st.arm(i, bar, evs, may_arm=self.pos_ticket is None and not blocked)

    # ---------- live-only syncs ----------
    def sync_live(self, st, ts, equity) -> None:
        if st.working:
            if not mt5c.pendings(self.mt5, self.symbol):
                posl = mt5c.positions(self.mt5, self.symbol)
                if posl:
                    self.pos_ticket = posl[0].ticket
                    self.open_info.update({"ts": ts, "lots": posl[0].volume,
                                           "entry": posl[0].price_open})
                    st.on_filled()
                    jwrite("fill", {"ts": ts, "ticket": self.pos_ticket,
                                    "price": posl[0].price_open})
                    self.tg("fill", f"FILLED {self.symbol} @ {posl[0].price_open}")
                else:
                    st.on_filled()
                    jwrite("warn", {"ts": ts, "why": "pending vanished unexplained"})
        if not st.working:
            for o in mt5c.pendings(self.mt5, self.symbol):
                mt5c.cancel(self.mt5, o.ticket)
                jwrite("cancel", {"ts": ts, "ticket": o.ticket, "why": "stale"})
        if self.pos_ticket:
            if not any(p.ticket == self.pos_ticket for p in mt5c.positions(self.mt5, self.symbol)):
                self.record_live_close(ts, equity)

    def record_live_close(self, ts, equity) -> None:
        ticket, info = self.pos_ticket, self.open_info or {}
        self.pos_ticket = None
        deals = mt5c.deals(self.mt5, ticket)
        pnl = sum(d.profit + d.commission + d.swap for d in deals)
        exit_price = deals[-1].price if deals else None
        reason = deals[-1].comment if deals else ""
        jwrite("close", {"ts": ts, "ticket": ticket, "equity": equity, "pnl": pnl})
        twrite({**info, "closed": ts, "ticket": ticket, "exit_price": exit_price,
                "pnl": pnl, "reason": reason, "mode": "live"})
        self.tg("close", f"position closed, pnl {pnl:.2f}, equity {equity:.0f}")

    def record_dry_exit(self, ev, win) -> None:
        pnl = ev["side"] * (ev["price"] - ev["entry"]) * ev["qty"] \
            - PAPER_COMMISSION_PCT * ev["entry"] * ev["qty"]
        self.paper_equity += pnl
        self.pos_ticket = None
        twrite({**(self.open_info or {}), "closed": win.index[ev["i"]],
                "open_bar": ev["hi"], "close_bar": ev["i"], "ticket": ev["ticket"],
                "exit_price": ev["price"], "pnl": pnl, "reason": ev["reason"],
                "mode": self.mode})
        self.tg("close", f"[{self.mode}] position closed ({ev['reason']}), pnl {pnl:.2f}, "
                         f"equity {self.paper_equity:.0f}")

    # ---------- equity baselines ----------
    def baselines(self, equity: float, ts):
        if self.sim is not None:
            if self.cur_day != ts.date():
                self.cur_day, self.day_start = ts.date(), equity
            return self.day_start, self.start_equity
        recs = JOURNAL / "runner.jsonl"
        day_s = start_s = None
        if recs.exists():
            for line in recs.read_text().splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("kind") == "day":
                    if r["ts"][:10] == str(ts.date()):
                        day_s = r["equity"]
                    start_s = r["equity"]
        if day_s is None:
            jwrite("day", {"ts": ts, "equity": equity})
            day_s = equity
        return day_s, start_s or equity


def run_cycle(r: Runner) -> None:
    win = r.window()
    h4 = data.resample_h4(win)
    cache = precompute(win, h4)
    st = StrategyState(r.params, cache, win)

    # warmup replay through the SAME backtest loop -> adopt its end state
    from . import backtest
    res = backtest.run(win, h4, p=r.params, cache=cache)
    st.setup, st.working = res["state"].setup, res["state"].working

    # resync against broker truth
    if r.mt5:
        if st.working and not mt5c.pendings(r.mt5, r.symbol):
            if mt5c.positions(r.mt5, r.symbol):
                r.pos_ticket = mt5c.positions(r.mt5, r.symbol)[0].ticket
                st.on_filled()
            else:
                st.on_filled()
        if r.pos_ticket is None and mt5c.positions(r.mt5, r.symbol):
            r.pos_ticket = mt5c.positions(r.mt5, r.symbol)[0].ticket

    i = len(win) - 2                      # last CLOSED bar (row -1 is forming)
    r.process(i, win, st, cache)


# ---------- paper mode: live data, simulated book, persisted in the journal ----------
def paper_cycle(r: Runner) -> None:
    """One M15 close on live broker data with NO orders. State persists in
    journal/runner.jsonl (kind=paper_state) across cycles/restarts — the
    paper book (pending/position/equity) is ours, no broker holds it."""
    win = r.window()
    h4 = data.resample_h4(win)
    cache = precompute(win, h4)
    st = StrategyState(r.params, cache, win)
    _load_paper_state(r, win, st)

    i = len(win) - 2                      # last CLOSED bar
    r.process(i, win, st, cache)
    _save_paper_state(r, win, st, i)
    print(f"[paper] {win.index[i]} equity {r.paper_equity:,.0f} "
          f"{'pos open' if r.sim.pos else 'flat'}")


def _save_paper_state(r: Runner, win, st, i: int) -> None:
    def ts_of(idx):
        return str(win.index[idx]) if idx is not None else None

    s = st.setup
    rec = {
        "ts": str(win.index[i]), "equity": r.paper_equity,
        "day_start": r.day_start, "cur_day": str(r.cur_day), "halt_day": str(r.halt_day),
        "setup": None if s is None else {
            "side": s["side"], "extreme": s["extreme"],
            "sweep_ts": ts_of(s["sweep_bar"]), "confirm_ts": ts_of(s.get("confirm_bar")),
            "zone_bottom": s.get("zone_bottom"), "zone_top": s.get("zone_top"),
            "stop": s.get("stop"), "working": st.working},
        "pending": r.sim.pending, "pos": r.sim.pos and {
            **r.sim.pos, "i_ts": ts_of(r.sim.pos["i"]), "hi_ts": ts_of(r.sim.pos["hi"])},
        "pos_ticket": r.pos_ticket, "open_info": r.open_info,
        "start_equity": r.start_equity,
    }
    jwrite("paper_state", rec)


def _load_paper_state(r: Runner, win, st) -> None:
    f = JOURNAL / "runner.jsonl"
    if not f.exists():
        return
    rec = None
    for line in f.read_text().splitlines():
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if j.get("kind") == "paper_state":
            rec = j
    if rec is None:
        return                      # first paper cycle: starts flat, honest session start

    def loc(ts_str):
        try:
            return win.index.get_loc(pd.Timestamp(ts_str))
        except KeyError:
            return None

    r.paper_equity = rec.get("equity", PAPER_START)
    r.start_equity = rec.get("start_equity", PAPER_START)
    r.day_start = rec.get("day_start")
    r.cur_day = rec.get("cur_day")
    r.halt_day = rec.get("halt_day")
    r.halt_day = None if r.halt_day in (None, "None") else pd.Timestamp(r.halt_day).date()
    r.pos_ticket = rec.get("pos_ticket")
    r.open_info = rec.get("open_info")

    s = rec.get("setup")
    if s:
        sweep_i, confirm_i = loc(s["sweep_ts"]), loc(s["confirm_ts"])
        if sweep_i is not None:
            st.setup = {"side": s["side"], "sweep_bar": sweep_i, "extreme": s["extreme"],
                        "swept_level": s.get("swept_level"), "confirm_bar": confirm_i,
                        "confirm_type": s.get("confirm_type"),
                        "zone_bottom": s.get("zone_bottom"),
                        "zone_top": s.get("zone_top"), "zone_kind": s.get("zone_kind"),
                        "stop": s.get("stop")}
            st.working = bool(s.get("working"))
    pend, pos = rec.get("pending"), rec.get("pos")
    if pend:
        r.sim.pending = pend
    if pos:
        i_idx, hi_idx = loc(pos.pop("i_ts", None)), loc(pos.pop("hi_ts", None))
        if i_idx is not None and hi_idx is not None:
            pos["i"], pos["hi"] = i_idx, hi_idx
            r.sim.pos = pos
        else:
            # position older than the window: keep tracking via open_info only
            jwrite("warn", {"ts": rec.get("ts"), "why": "paper pos older than window"})


def dry_run(r: Runner, data_dir: str) -> None:
    """Full workflow over historical data with paper fills — identical
    advance/fill/arm order as live; interlocks recorded as would-triggers."""
    from . import backtest
    win = r.window()
    h4 = data.resample_h4(win)
    cache = precompute(win, h4)
    st = StrategyState(r.params, cache, win)
    r.sim = SimBroker()                   # qty fixed at place, like a real broker

    for i in range(len(win)):
        r.process(i, win, st, cache)

    # self-check: the dry trade sequence must equal the validated backtest's,
    # EXCEPT where interlock windows (blocked arms/places, which the backtest
    # doesn't model) reorder or drop setups. Divergences form clusters; a
    # cluster is attributed when a blocked bar falls in its causal window.
    import difflib
    res = backtest.run(win, h4, p=r.params, cache=cache)
    dry_bars = [t["open_bar"] for t in trades()]
    ref_bars = [t["hi"] for t in res["trades_list"]]
    sm = difflib.SequenceMatcher(a=dry_bars, b=ref_bars, autojunk=False)
    zone_total = r.params.confirm_within + r.params.zone_valid
    clusters, missing, extra = [], 0, 0
    last_eq_ref = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            last_eq_ref = ref_bars[j2 - 1]
            continue
        missing += i2 - i1
        extra += j2 - j1
        span_end = ref_bars[j2 - 1] if j2 > j1 else dry_bars[i2 - 1]
        clusters.append((last_eq_ref, span_end, i2 - i1, j2 - j1))
    attributed = unexplained = 0
    # reject events (lots<min, dist<=0) are live-sizing realities the backtest
    # doesn't model — they explain divergences just like interlock windows do
    reject_bars = sorted(win.index.get_loc(rr["ts"]) for rr in rejects_in(win))
    for span_start, span_end, m, x in clusters:
        near = any(span_start - zone_total <= b <= span_end + zone_total
                   for b in r.blocked_bars + reject_bars)
        if near:
            attributed += 1
        else:
            unexplained += 1
    print(f"dry trades: {len(dry_bars)}  backtest trades: {len(ref_bars)}  "
          f"missing {missing} / extra {extra} in {len(clusters)} clusters")
    print(f"clusters attributed to interlock windows: {attributed}  "
          f"unexplained: {unexplained}")
    print(f"backtest metrics (1% sizing): PF {res['profit_factor']:.2f}  "
          f"return {res['total_return']:.1%}  maxDD {res['max_drawdown']:.1%}")
    print(f"dry paper equity (0.3% sizing, FTMO-style commission): "
          f"{r.paper_equity:,.0f} from {PAPER_START:,.0f}")
    if unexplained:
        print("WARNING: unexplained dry/backtest divergence — investigate before trusting the run")
    else:
        print("dry run OK: every difference cluster has an interlock trigger in its causal window")
    print(f"journal: {JOURNAL / 'runner.jsonl'}  trades: {JOURNAL / 'trades.jsonl'}")


def trades() -> list:
    f = JOURNAL / "trades.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def rejects_in(win: pd.DataFrame) -> list:
    """Journal reject events with a resolvable bar index in `win`."""
    f = JOURNAL / "runner.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "reject" and rec.get("ts") in win.index:
            out.append(rec)
    return out


def main() -> None:
    mode = "once"
    loop = "--loop" in sys.argv
    for flag, m in (("--loop", "loop"), ("--replay", "replay"), ("--dry", "dry"),
                    ("--paper", "paper")):
        if flag in sys.argv:
            mode = m
    data_dir = next((a for a in sys.argv[1:] if not a.startswith("-")), "../M5/M5")
    cfg = load_cfg() if mode in ("once", "loop", "paper") else {"data_dir": data_dir}
    r = Runner(mode, cfg)
    print(f"runner mode={mode} params={r.params}")

    if mode == "replay":
        run_cycle(r)
        print(f"replay OK — journal at {JOURNAL / 'runner.jsonl'}")
        return
    if mode == "dry":
        dry_run(r, data_dir)
        return
    if mode == "paper":
        paper_cycle(r)
        if not loop:
            return
        while True:                     # paper loop: wake at each M15 close +3s
            now = time.time()
            nxt = (int(now // 900) + 1) * 900 + 3
            time.sleep(max(0, nxt - time.time()))
            try:
                paper_cycle(r)
            except Exception as e:
                r.tg("error", f"paper cycle error: {e!r}")
                print(f"paper cycle error: {e!r}", flush=True)
        return
    if mode == "once":
        run_cycle(r)
        return

    # loop: wake at each M15 close +3s
    while True:
        now = time.time()
        nxt = (int(now // 900) + 1) * 900 + 3
        time.sleep(max(0, nxt - time.time()))
        try:
            run_cycle(r)
        except Exception as e:
            r.tg("error", f"cycle error: {e!r}")
            print(f"cycle error: {e!r}", flush=True)


if __name__ == "__main__":
    main()
