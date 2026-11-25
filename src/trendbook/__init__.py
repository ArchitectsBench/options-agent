"""TRACK C — trend-book shadow manager (hybrid_trend_v3 frozen design).

Once-daily shadow-mode forward test of the v3 hybrid trend book, per
config/trend_forward_criteria.yaml. OBSERVATION-ONLY: no broker, no orders;
the IBKR paper executor attaches later and inherits this ledger format.

Entry point: python -m src.trendbook.manager  (see manager.py docstring).
"""
