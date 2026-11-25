"""Forward paper-test for the residual-momentum + earnings book (Track A OOS proof).

Daily (after close): refresh the price panel via Polygon grouped-daily, mark the
held book to market on the new day, append to the track record, rebalance to the
new target book (costed), and report cumulative FORWARD stats vs the backtest
expectation (full Sharpe ~0.4 / recent-OOS ~1.0-1.5). NOTIONAL book — validates the
SIGNAL forward on unseen data; no orders placed.

  python src/research/forward_test.py          # run one daily step
  python src/research/forward_test.py --report # just print the track record
"""
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.data.polygon_data import get_client, BASE
from src.research.momentum_book import CLOSE, target_book

DIR = ROOT / "data" / "fwdtest"
STATE = DIR / "state.json"
TRACK = DIR / "track.csv"
COST_BPS = 2.0

# Pre-committed stop rule (2026-07-01, user decision — see
# docs/live-readiness-review-2026-07-01.md). The forward test halts itself when
# falsified; amending these thresholds after looking at results invalidates the
# test. At adoption the track stood at 5 sessions, -1.14% cum, 4.37% drawdown —
# below both triggers, so the rule is forward-looking, not retroactive.
STOP_MAX_DD = 0.05           # stop if equity drawdown from peak >= 5% ...
STOP_SHARPE_SESSIONS = 20    # ... or once >= 20 sessions are on record ...
STOP_SHARPE_FLOOR = 0.0      # ... annualized Sharpe is <= 0


def refresh_panel() -> pd.DataFrame:
    panel = pd.read_parquet(CLOSE)
    uni = set(panel.columns)
    c = get_client()
    last = panel.index.max()
    end = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    new = {}
    d = last + pd.Timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:                       # skip weekends
            out = c._get(f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{d.date()}?adjusted=true")
            res = out.get("results", []) if out and "error" not in out else []
            closes = {r["T"]: r["c"] for r in res if r.get("T") in uni}
            if closes:
                new[d] = closes
        d += pd.Timedelta(days=1)
    if new:
        panel = pd.concat([panel, pd.DataFrame(new).T]).sort_index()
        panel = panel[~panel.index.duplicated(keep="last")]
        panel.to_parquet(CLOSE)
        print(f"  refreshed panel: +{len(new)} session(s) -> {panel.index.max().date()}")
    else:
        print("  no new sessions to add")
    return panel


def _load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else None


def _check_stop() -> str | None:
    """Evaluate the pre-committed stop rule against the track record."""
    if not TRACK.exists():
        return None
    t = pd.read_csv(TRACK)
    eq = t["equity"]
    dd = 1.0 - eq.iloc[-1] / eq.cummax().iloc[-1]
    if dd >= STOP_MAX_DD:
        return f"drawdown {dd:.2%} >= {STOP_MAX_DD:.0%} stop"
    r = t["net_ret"]
    if len(r) >= STOP_SHARPE_SESSIONS and r.std() > 0:
        sharpe = float(r.mean() / r.std() * np.sqrt(252))
        if sharpe <= STOP_SHARPE_FLOOR:
            return f"Sharpe {sharpe:.2f} <= {STOP_SHARPE_FLOOR:.1f} after {len(r)} sessions"
    return None


def _halt(state: dict, reason: str):
    state["status"] = "STOPPED"
    state["stopped"] = {"date": str(pd.Timestamp.now().date()), "reason": reason}
    STATE.write_text(json.dumps(state))
    msg = f"forward test STOPPED per pre-committed rule: {reason}"
    print(f"\n!!! {msg}")
    print("!!! No further rebalances. Rule frozen 2026-07-01; see docs/live-readiness-review-2026-07-01.md")
    try:
        from src.engine.notify import send
        send("Forward test STOPPED", msg, priority="high", tags="octagonal_sign")
    except Exception as exc:                      # notification is best-effort
        print(f"  (notify failed: {exc})")


def run():
    DIR.mkdir(parents=True, exist_ok=True)
    prior = _load_state()
    if prior and prior.get("status") == "STOPPED":
        info = prior.get("stopped", {})
        print(f"forward test is STOPPED ({info.get('date')}: {info.get('reason')}) — no action")
        return
    panel = refresh_panel()
    state = prior
    asof, w = target_book()                      # latest target book
    w = w.to_dict()

    if state is None:
        STATE.write_text(json.dumps({"asof": str(asof.date()), "weights": w, "equity": 1.0}))
        print(f"INITIALIZED forward test asof {asof.date()} — {len(w)} names held, equity 1.0000")
        print("Track record begins tomorrow.")
        return

    d0 = pd.Timestamp(state["asof"])
    held = pd.Series(state["weights"])
    if asof <= d0:
        print(f"  no new trading day since {d0.date()} — book unchanged")
        return

    # mark held book to market over the gap (held weights applied to the new return)
    px = panel.reindex(columns=held.index)
    day_ret = (px.loc[asof] / px.loc[d0] - 1.0)
    gross_ret = float((held * day_ret).dropna().sum())
    turn = float((pd.Series(w).reindex(held.index).fillna(0) - held).abs().sum()
                 + pd.Series(w).drop(held.index, errors="ignore").abs().sum())
    cost = turn * COST_BPS / 1e4
    net_ret = gross_ret - cost
    equity = state["equity"] * (1 + net_ret)

    pd.DataFrame([{"date": asof.date(), "gross_ret": round(gross_ret, 5),
                   "cost": round(cost, 5), "net_ret": round(net_ret, 5),
                   "equity": round(equity, 5), "n": len(held)}]).to_csv(
        TRACK, mode="a", header=not TRACK.exists(), index=False)
    new_state = {"asof": str(asof.date()), "weights": w, "equity": equity}
    STATE.write_text(json.dumps(new_state))
    print(f"  {d0.date()}->{asof.date()}: net {net_ret*100:+.2f}%  equity {equity:.4f}")
    reason = _check_stop()
    if reason:
        _halt(new_state, reason)
    report()


def report():
    if not TRACK.exists():
        print("no track record yet")
        return
    t = pd.read_csv(TRACK)
    r = t["net_ret"]
    n = len(r)
    state = _load_state()
    tag = " — STOPPED" if state and state.get("status") == "STOPPED" else ""
    print(f"\n── FORWARD TRACK ({n} sessions, {t['date'].iloc[0]}..{t['date'].iloc[-1]}){tag} ──")
    print(f"  cumulative: {(t['equity'].iloc[-1]-1)*100:+.2f}%  |  ann.return {r.mean()*252*100:+.1f}%")
    if n >= 5 and r.std() > 0:
        print(f"  forward Sharpe (ann): {r.mean()/r.std()*np.sqrt(252):.2f}  "
              f"(backtest: full ~0.4 / recent-OOS ~1.0-1.5)")
        print(f"  hit rate: {(r>0).mean():.0%}  |  worst day {r.min()*100:+.2f}%")
    else:
        print(f"  (need >=5 sessions for a Sharpe estimate; have {n})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    report() if a.report else run()
