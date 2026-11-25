"""Principled next experiments after the naive condor failed (see TRACK_A_STATUS).
NOT param-mining — two specific hypotheses:
  A. wing-SCALED condor (3% of spot, not a fixed $5) — fixes the spec error.
  B. same, NO STOP (let defined-risk wings be the only stop; stops may lock in
     noise that reverts).
  C. PUT CALENDAR — the term-structure/contango edge the VRP screen rated stronger.

  python src/backtest/experiments.py
"""
from functools import partial

from src.backtest.backtest import LIQUID_ETFS, report
from src.backtest.regime import GateParams, RegimeGate
from src.backtest.structures import calendar, iron_condor

if __name__ == "__main__":
    gate = RegimeGate(GateParams())
    tk = LIQUID_ETFS

    report(tk, partial(iron_condor, dte=35, short_delta=0.16, wing_pct=0.03),
           "A) WING-SCALED iron condor 35DTE 0.16Δ 3%-wing — WITH stop (2× credit)",
           gate, profit_target=0.50, stop_mult=2.0)

    report(tk, partial(iron_condor, dte=35, short_delta=0.16, wing_pct=0.03),
           "B) WING-SCALED iron condor 35DTE 0.16Δ 3%-wing — NO stop (defined-risk only)",
           gate, profit_target=0.50, stop_mult=999)

    report(tk, partial(calendar, right="P", front_dte=30, back_dte=60, delta=0.50),
           "C) PUT CALENDAR 30/60 DTE ATM — term-structure/contango edge",
           gate, profit_target=0.50, stop_mult=2.0)
