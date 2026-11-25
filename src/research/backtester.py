"""Stage 3 — Backtester.

Execution model:
  - Boolean signals are read at bar t close and fill at bar t+1 OPEN
    (the rule validator's no-lookahead guarantee survives execution).
  - Stop/target levels are captured at the signal bar and evaluated
    INTRABAR against each bar's high/low. Fills are gap-aware (a gap
    through the level fills at the open, not the level). If both stop and
    target are touched in the same bar, the STOP is assumed first —
    conservative by construction.
  - Long and short positions; one position at a time.
  - `flat_at_session_end=True` force-closes at each session's last bar
    close (day-trading mandate — no overnight holds). An entry signal on a
    session's last bar still fills at the next session's open.

Sharpe annualization is derived from the data: periods_per_year =
252 * (bars per session), so daily and 1h bars are both handled correctly.
Equity is marked to market every bar close.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.indicators.core import atr as atr_indicator
from src.research.signals import Signals, as_signals

TRADING_DAYS_PER_YEAR = 252
TRAIL_ATR_PERIOD = 14


@dataclass
class Trade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    bars_held: int
    direction: int = 1  # +1 long, -1 short

    @property
    def pnl_pct(self) -> float:
        return self.direction * (self.exit_price / self.entry_price - 1.0)


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series  # mark-to-market equity curve, starts at 1.0
    periods_per_year: float = float(TRADING_DAYS_PER_YEAR)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.pnl_pct > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        gross_loss = -sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0)
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else float("nan")
        return gross_profit / gross_loss

    @property
    def sharpe(self) -> float:
        bar_returns = self.equity.pct_change().dropna()
        if bar_returns.std() == 0:
            return float("nan")
        return bar_returns.mean() / bar_returns.std() * np.sqrt(self.periods_per_year)

    @property
    def max_drawdown(self) -> float:
        running_peak = self.equity.cummax()
        return float((self.equity / running_peak - 1.0).min())

    @property
    def avg_bars_held(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(t.bars_held for t in self.trades) / len(self.trades)

    @property
    def total_return(self) -> float:
        return float(self.equity.iloc[-1] - 1.0)


def _series_or(s: pd.Series | None, n: int, fill) -> np.ndarray:
    if s is None:
        return np.full(n, fill)
    return s.to_numpy(dtype=bool if fill is False else float)


def backtest(
    df: pd.DataFrame,
    signals,
    exit_signal: pd.Series | None = None,
    flat_at_session_end: bool = False,
    cost_bps: float = 0.0,
    trail_mult: float = 0.0,
) -> BacktestResult:
    """df needs OHLC columns. `signals` is a Signals object, a legacy
    (entry, exit) tuple, or a lone entry Series with exit_signal supplied.

    cost_bps: transaction cost PER SIDE in basis points (half-spread +
    slippage). Applied to every fill; recorded prices and equity are net.

    trail_mult: if > 0, the stop ratchets each bar close to
    close -/+ trail_mult * ATR(14) (long/short), starting from the
    signal-supplied stop level (or unset if that was NaN). ATR at bar i is
    applied to bar i+1's checks — causal."""
    if exit_signal is not None:
        sigs = Signals(entry_long=signals, exit_long=exit_signal)
    else:
        sigs = as_signals(signals)

    n = len(df)
    opens = df["Open"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)

    el = _series_or(sigs.entry_long, n, False)
    xl = _series_or(sigs.exit_long, n, False)
    es = _series_or(sigs.entry_short, n, False)
    xs = _series_or(sigs.exit_short, n, False)
    sl = _series_or(sigs.stop_long, n, float("nan"))
    tl = _series_or(sigs.target_long, n, float("nan"))
    ss = _series_or(sigs.stop_short, n, float("nan"))
    ts = _series_or(sigs.target_short, n, float("nan"))

    cost = cost_bps / 1e4
    trail_atr = atr_indicator(df, TRAIL_ATR_PERIOD).to_numpy(dtype=float) if trail_mult > 0 else None

    sessions = pd.DatetimeIndex(df.index).normalize()
    n_sessions = sessions.nunique()
    periods_per_year = TRADING_DAYS_PER_YEAR * (n / n_sessions) if n_sessions else float("nan")
    last_of_session = np.ones(n, dtype=bool)
    last_of_session[:-1] = sessions[:-1] != sessions[1:]

    trades: list[Trade] = []
    equity = np.ones(n)
    pos = 0  # +1 long, -1 short, 0 flat
    entry_idx = -1
    entry_price = stop = target = 0.0
    pending_entry: tuple[int, float, float] | None = None  # (dir, stop, target)
    pending_exit = False
    prev_mark = 1.0
    prev_price = 0.0

    def close_position(i: int, price: float):
        nonlocal pos, prev_mark, pending_exit
        price = price * (1 - pos * cost)  # pay half-spread + slippage on exit
        prev_mark = prev_mark * (1 + pos * (price / prev_price - 1))
        trades.append(
            Trade(
                entry_date=df.index[entry_idx],
                exit_date=df.index[i],
                entry_price=entry_price,
                exit_price=price,
                bars_held=i - entry_idx,
                direction=pos,
            )
        )
        pos = 0
        pending_exit = False

    for i in range(n):
        # 1. Fills at this bar's open (from signals read at bar i-1 close)
        if pos == 0 and pending_entry is not None:
            d, stop, target = pending_entry
            pos = d
            entry_idx = i
            entry_price = opens[i] * (1 + d * cost)  # pay half-spread + slippage on entry
            prev_price = entry_price
            pending_entry = None
        elif pos != 0 and pending_exit:
            close_position(i, opens[i])

        # 2. Intrabar stop/target (stop checked first — conservative)
        if pos != 0:
            fill = None
            if pos == 1:
                if not math.isnan(stop) and (opens[i] <= stop or lows[i] <= stop):
                    fill = opens[i] if opens[i] <= stop else stop
                elif not math.isnan(target) and (opens[i] >= target or highs[i] >= target):
                    fill = opens[i] if opens[i] >= target else target
            else:
                if not math.isnan(stop) and (opens[i] >= stop or highs[i] >= stop):
                    fill = opens[i] if opens[i] >= stop else stop
                elif not math.isnan(target) and (opens[i] <= target or lows[i] <= target):
                    fill = opens[i] if opens[i] <= target else target
            if fill is not None:
                close_position(i, fill)

        # 3. Mark to market at the close
        if pos != 0:
            prev_mark = prev_mark * (1 + pos * (closes[i] / prev_price - 1))
            prev_price = closes[i]
            # trailing stop ratchets at the close; governs bar i+1 checks
            if trail_atr is not None and not math.isnan(trail_atr[i]):
                candidate = closes[i] - pos * trail_mult * trail_atr[i]
                if math.isnan(stop):
                    stop = candidate
                else:
                    stop = max(stop, candidate) if pos == 1 else min(stop, candidate)
        equity[i] = prev_mark

        # 4. Day-trading mandate: never hold overnight
        if pos != 0 and flat_at_session_end and last_of_session[i]:
            close_position(i, closes[i])  # prev_price == close, mark unchanged

        # 5. Read signals at this close; fills happen at bar i+1
        if pos == 1 and xl[i]:
            pending_exit = True
        elif pos == -1 and xs[i]:
            pending_exit = True
        elif pos == 0 and pending_entry is None:
            if el[i]:
                pending_entry = (1, sl[i], tl[i])
            elif es[i]:
                pending_entry = (-1, ss[i], ts[i])

    # Force-close anything still open at the final close so it counts as a trade.
    if pos != 0:
        close_position(n - 1, closes[n - 1])
        equity[n - 1] = prev_mark

    return BacktestResult(
        trades=trades,
        equity=pd.Series(equity, index=df.index),
        periods_per_year=periods_per_year,
    )
