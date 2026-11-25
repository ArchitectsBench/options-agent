"""Frozen constants of the RVOL-EXEC paper lane (spec §1) — UNVALIDATED — PAPER ONLY.

Every value here is pinned by `tests/rvol_exec/test_lane_rules.py::
test_constants_pinned_and_holidays_equal_config`. Changing one
is a construction change and goes through the spec's decision log (§22), never
a quiet edit. `config/config.yaml` is NEVER read at runtime by the lane: the
holiday list is a verbatim COPY that a test asserts equal to the config.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

# ── identity / isolation (§0) ────────────────────────────────────────────────
TAG = "UNVALIDATED-RVOL-PAPER"          # Order.orderRef on EVERY lane order
STAMP = "UNVALIDATED — PAPER ONLY"      # on every output surface
CLIENT_ID = 61                          # fixed, no fallback family (F4)
GO_NOGO_CLIENT_ID = 161                 # probes; 61 only for the availability check (§17)
HOST = "127.0.0.1"
PORT = 4002                             # IBKR PAPER gateway
ACCOUNT_PREFIX = "D"                    # paper rail: managedAccounts() must be D-prefixed

# ── construction (replay_k18, frozen) ────────────────────────────────────────
SLOTS = 10
RISK_USD = 50.0                         # per slot
STOP_ATR_FRAC = 0.20                    # K18b
GUARD_FRAC = 0.10                       # limit offset = 0.10 * stop_dist (the sim's cancel guardrail)
PART_CAP = 0.01                         # 1% of the 09:30-bar volume
DOJI_MIN_BODY = 0.10                    # body >= 10% of range
LEVEL_TICK = 0.01
LOSS_HALT_USD = -300.0                  # 6 slots' risk; realized (net of known commissions) + unrealized
NOTIONAL_CAP_USD = 25_000.0             # per slot; a CONSTRUCTION DEVIATION (§2 row 19), ledgered name_skipped{cap}
MAX_ENTRIES = 20
MAX_HELD = 10                           # + overflow by cancel/fill race only

# ── timeline (all ET; §4) ────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")
T_DAEMON_START = time(9, 33, 0)         # launchd 06:33 PT
T_SNAPSHOT = time(9, 35, 0)
T_READY_DEADLINE = time(10, 10, 0)      # no T_ready by then -> session_skipped, zero entries
T_ENTRY_END = time(10, 25, 0)           # GTD on every parent + explicit cancel
T_NO_ENTRY_AFTER = time(15, 40, 0)      # assertion only (L-2026-09-13); the window closes at 10:25
T_FLATTEN = time(15, 55, 0)
T_HARD_STOP = time(16, 0, 0)            # anything still open -> UNFLATTENED marker, exit 2

# ── cadence / thresholds ─────────────────────────────────────────────────────
LOOP_S = 5
SNAPSHOT_POLL_S = 30
REALTIME_THRESHOLD_S = 90               # newest `updated` within 90 s of wall clock -> real-time
PRERANK_K = 60                          # stage-1 depth; 100 when contaminated
PRERANK_K_CONTAMINATED = 100
STAGE2_EXACT_BAR = True                 # frozen switch; decided by Monday item 2 before window_start
STAGE2_RETRIES = 2                      # bounded re-fetch of a missed exact 09:30 bar before excluding a name
STAGE2_RETRY_WAIT_S = 15                # between retries when the snapshot is CONTAMINATED (else back-to-back)
BASELINE_SPAN_DAYS = 30                 # baseline14 sessions must lie within 30 calendar days of D_prev (shadow scan)
PARENT_TIF = "GTD"                      # frozen switch; "DAY" if Monday item 4 shows GTD rejected
INTENT_RESOLVE_AGE_S = 120
SHORT_LOCATE_WAIT_S = 30                # 404 "held while securities are located" tolerated this long
GAP_CONFIRM_LOOPS = 2                   # post-trigger breach cancel after 2 consecutive loops (10 s)
CONNECT_RETRY_S = 30
RECONNECT_ATTEMPTS = 3                  # x RECONNECT_WAIT_S, then available()==False, loop continues
RECONNECT_WAIT_S = 20
CONNECT_TIMEOUT_S = 20                  # ib.connect(timeout=)
REQUEST_TIMEOUT_S = 10                  # every blocking broker request; write timeout = AMBIGUOUS, read = None
FLATTEN_RETRY_S = 15
FLATTEN_UNFILLED_S = 30                 # MKT unfilled this long -> flatten_failed + page
OCA_VERIFY_S = 10                       # after a flatten fill the child must show Cancelled within this
RESTART_BREAKER = 5                     # daemon starts per session before HOLD + exit 0
CLOCK_JUMP_S = 300
SNAPSHOT_MIN_TICKERS = 5_000
SNAPSHOT_MIN_CAND_COVERAGE = 0.70       # share of prep candidates with day.o present
PREP_MIN_CANDIDATES = 500
PREP_MIN_BASELINE_FRAC = 0.90           # with_baseline14 / candidates

# ── ex-ante filter (shadow scan) ─────────────────────────────────────────────
FILTER_OPEN_MIN = 5.0
FILTER_AVGVOL14_MIN = 1_000_000
FILTER_ATR14_MIN = 0.50
BASELINE_N = 14

# ── snapshot reference names (§5) — liquid names that print every minute ─────
SNAPSHOT_REF = ("SPY", "QQQ", "AAPL", "NVDA", "TSLA")

# Verbatim copy of config/config.yaml `agent.holidays_2026` (config is never
# read at runtime; test_constants asserts equality against the file).
HOLIDAYS_2026 = (
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
)

# The OPTIONS engine's tag — named here only so isolation tests can assert
# TAG != OPTIONS_ENGINE_TAG without importing the options engine.
OPTIONS_ENGINE_TAG = "UNVALIDATED-PAPER-ONLY"
