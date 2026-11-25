"""RVOL-EXEC paper lane — UNVALIDATED — PAPER ONLY.

The frozen K18 construction (`scripts/rvolrank_sleeve.py::replay_k18`)
executed causally against the IBKR PAPER account by a SEPARATE process with
its OWN stock client (`src/data/ibkr_stock_orders.py`, clientId 61, orderRef
`UNVALIDATED-RVOL-PAPER`). Spec: `docs/rvol_exec_lane.md`.

Isolation contract (spec §0): nothing in this package may import
`src.engine.paper_trader`, `src.engine.pipeline`, `src.scheduler`,
`src.data.ibkr_orders`, `src.data.ibkr_thread` or `src.data.order_client`.
"""
