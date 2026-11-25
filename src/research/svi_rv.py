#!/usr/bin/env python
"""
SVI SURFACE RELATIVE-VALUE study — professional-path doc study B2 (2026-07-06).

Pre-registered design (frozen before implementation; see task + report):
  - Universe: the full iv_history_full chain store = 60 single-name lineages
    + 7 ETFs + SPX (excluded: XSP duplicate of SPX, VIX futures-underlying,
    GOOGL duplicate of GOOG; FB+META stitched as one lineage with the
    Roundhill-ETF META rows pre-2022-06-09 dropped).
  - Per name/day/expiration with 7-90 DTE and >= 8 OTM strikes having
    nonzero bid, OI > 0, relative spread <= 25%: fit a smooth smile to
    MID-DERIVED IVs in log-moneyness (NOT the store's IV columns).
  - Signal: strike residual (mid IV - fitted IV) standardized by the smile's
    cross-sectional residual std; |z| >= 2.0.
      z >= +2 (rich): LONG BUTTERFLY centered on the rich strike
        (short 2x middle, long 1x each adjacent fitted strike) —
        profits if the rich strike's IV converges DOWN.
      z <= -2 (cheap): SHORT BUTTERFLY (long 2x middle, short 1x wings) —
        profits if the cheap strike's IV converges UP.
  - Max 5 new positions/day across the universe, ranked by |z|.
  - Exit: |z| < 0.5 at any subsequent close, OR 5 trading days elapsed,
    OR expiration within 3 DTE — first hit. Marks at actual chain quotes.
  - Fills: BID/ASK primary, MID diagnostic. r = P&L / max-loss capital.
  - Verdict rule frozen (see report()).

Implementation notes recorded as deviations-from-letter (documented in report):
  - divRate is 0 for every row in the store and the `spot` column's meaning
    shifts across ORATS schema eras (cash spot vs per-expiry forward).
    Forwards are therefore derived from PUT-CALL PARITY per name/day/expiry
    (median over near-ATM two-sided pairs, discounting at the row iRate),
    then mids are inverted through Black-76 on the forward. This uses only
    market prices + iRate, and is strictly more robust than the row fields.

Subcommands:
  check-iv       empirical IV-provenance check (mid-derived vs store columns)
  pilot-method   3-day SVI vs quadratic comparison -> choose ONE method
  pilot-runtime  3 names full-history to calibrate full-run cost
  pass1          fit all smiles, per-name checkpointed
  select         cross-universe daily ranking -> selected trades
  pass2          lifecycle simulation at actual chain quotes
  report         verdict + diagnostics -> md/csv
  full           pass1 -> select -> pass2 -> report
"""
import os
import sys
import time
import math
import datetime as dt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------- paths
ROOT = "/Users/you/projects/OptionsAgent"
DATA = os.path.join(ROOT, "data", "iv_history_full")
LOGP = os.path.join(ROOT, "logs", "research", "svi_rv_20260706.log")
CSVP = os.path.join(ROOT, "logs", "research", "svi_rv_20260706.csv")
MDP = os.path.join(ROOT, "logs", "research", "svi_rv_20260706.md")
SCRATCH = os.environ.get(
    "SVI_SCRATCH",
    "/private/tmp/claude-501/-Users-you-projects-OptionsAgent/"
    "801744be-bebd-4fe1-a660-496a6048bd53/scratchpad/svi_rv",
)
P1DIR = os.path.join(SCRATCH, "pass1")
os.makedirs(P1DIR, exist_ok=True)

# ---------------------------------------------------------------- frozen params
DTE_MIN, DTE_MAX = 7, 90
MIN_STRIKES = 8
MAX_RELSPREAD = 0.25
Z_ENTRY = 2.0
Z_EXIT = 0.5
MAX_HOLD_TD = 5
EXIT_DTE = 3
MAX_PER_DAY = 5
REENTRY_CAL_DAYS = 8  # no identical position re-entry within 8 calendar days
                      # (~5 trading days = the max holding period)
ERAS = [("2007-2012", 2007, 2012), ("2013-2017", 2013, 2017),
        ("2018-2019", 2018, 2019), ("2020-2022", 2020, 2022),
        ("2023-2026", 2023, 2026)]

EXCLUDE = {"XSP", "VIX", "GOOGL", "FB"}  # FB folded into META lineage
ETFS = {"SPY", "QQQ", "IWM", "XLF", "XLE", "TLT", "SLV"}

COLS = ["trade_date", "expiration", "strike", "spot", "yte", "iRate",
        "cBidPx", "cAskPx", "pBidPx", "pAskPx", "cOi", "pOi"]

# ---------------------------------------------------------------- logging
def log(msg):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOGP, "a") as f:
        f.write(line + "\n")

# ---------------------------------------------------------------- math (numpy-only)
def _norm_cdf(x):
    # Abramowitz & Stegun 7.1.26 erf approximation, |err| < 1.5e-7 — ample
    # for IV inversion against quotes with >= penny granularity.
    z = x / math.sqrt(2.0)
    s = np.sign(z)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741
              + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-a * a)
    return 0.5 * (1.0 + s * erf)

def black76(F, K, T, r, sig, is_call):
    sig = np.maximum(sig, 1e-9)
    st = sig * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    df = np.exp(-r * T)
    c = df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    p = df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))
    return np.where(is_call, c, p)

def black76_vega(F, K, T, r, sig):
    sig = np.maximum(sig, 1e-9)
    st = sig * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * st * st) / st
    return np.exp(-r * T) * F * np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * np.sqrt(T)

def implied_vol(mid, F, K, T, r, is_call):
    """Vectorized Newton with bisection safety. Returns NaN where invalid."""
    df = np.exp(-r * T)
    intr = df * np.where(is_call, np.maximum(F - K, 0), np.maximum(K - F, 0))
    ub = df * np.where(is_call, F, K)
    ok = (mid > intr + 1e-6) & (mid < ub - 1e-9) & (T > 0)
    sig = np.full_like(mid, np.nan, dtype=float)
    if not ok.any():
        return sig
    # Brenner-Subrahmanyam seed, clipped
    s = np.clip(np.sqrt(2 * math.pi / T[ok]) * mid[ok] / F[ok], 0.03, 3.0)
    m, Fo, Ko, To, ro, ico = mid[ok], F[ok], K[ok], T[ok], r[ok], is_call[ok]
    lo = np.full_like(s, 1e-4)
    hi = np.full_like(s, 10.0)
    for _ in range(100):
        px = black76(Fo, Ko, To, ro, s, ico)
        diff = px - m
        hi = np.where(diff > 0, np.minimum(hi, s), hi)
        lo = np.where(diff < 0, np.maximum(lo, s), lo)
        v = black76_vega(Fo, Ko, To, ro, s)
        step = diff / np.maximum(v, 1e-12)
        s_new = s - step
        bad = (s_new <= lo) | (s_new >= hi) | ~np.isfinite(s_new)
        s_new = np.where(bad, 0.5 * (lo + hi), s_new)
        if np.all(np.abs(s_new - s) < 1e-9):
            s = s_new
            break
        s = s_new
    px = black76(Fo, Ko, To, ro, s, ico)
    conv = np.abs(px - m) < np.maximum(1e-3 * m, 1e-4)
    s = np.where(conv, s, np.nan)
    out = sig.copy()
    out[np.where(ok)[0]] = s
    return out

# ---------------------------------------------------------------- smile fits
def fit_svi(k, iv, T):
    """Quasi-explicit SVI on total variance: w = a + b(rho(k-m)+sqrt((k-m)^2+s^2)).
    Grid over (m, sigma); linear solve for (a, b*rho, b). Returns fitted IV
    array or None if no valid parameterization."""
    w = iv * iv * T
    n = len(k)
    mg = np.linspace(k.min(), k.max(), 13)
    sg = np.geomspace(0.02, 0.8, 9)
    M, S = np.meshgrid(mg, sg)
    M = M.ravel()[:, None]          # (G,1)
    S = S.ravel()[:, None]
    D = k[None, :] - M              # (G,n)
    R = np.sqrt(D * D + S * S)
    G = M.shape[0]
    X = np.stack([np.ones((G, n)), D, R], axis=2)          # (G,n,3)
    ATA = np.einsum("gni,gnj->gij", X, X) + 1e-10 * np.eye(3)
    ATy = np.einsum("gni,n->gi", X, w)
    try:
        P = np.linalg.solve(ATA, ATy[..., None])[..., 0]    # (G,3)
    except np.linalg.LinAlgError:
        return None
    c1, c2 = P[:, 1], P[:, 2]
    wf = np.einsum("gni,gi->gn", X, P)                      # (G,n)
    valid = (c2 > 1e-12) & (np.abs(c1) <= c2) & (wf.min(axis=1) > 1e-12)
    if not valid.any():
        return None
    sse = np.where(valid, ((wf - w[None, :]) ** 2).sum(axis=1), np.inf)
    g = int(np.argmin(sse))
    return np.sqrt(wf[g] / T)

def fit_quad(k, iv, T=None):
    """Quadratic in log-moneyness on IV directly (fallback comparator)."""
    try:
        c = np.polyfit(k, iv, 2)
    except Exception:
        return None
    f = np.polyval(c, k)
    if (f <= 0).any():
        return None
    return f

FIT_FUNCS = {"svi": fit_svi, "quad": fit_quad}

# ---------------------------------------------------------------- data loading
def all_store_tickers():
    return sorted({f.split("_")[0] for f in os.listdir(DATA) if f.endswith(".parquet")})

def universe():
    return sorted(set(all_store_tickers()) - EXCLUDE)

def files_for(name):
    if name == "META":
        fs = [f for f in os.listdir(DATA) if f.startswith("FB_") or f.startswith("META_")]
    else:
        fs = [f for f in os.listdir(DATA)
              if f.startswith(name + "_") and f.endswith(".parquet")]
    return sorted(os.path.join(DATA, f) for f in fs if f.endswith(".parquet"))

def parse_dates(s):
    try:
        return pd.to_datetime(s, format="%m/%d/%Y")
    except (ValueError, TypeError):
        return pd.to_datetime(s)

def load_year(path, name, cols=COLS):
    df = pd.read_parquet(path, columns=cols)
    df["date"] = parse_dates(df["trade_date"])
    df["exp"] = parse_dates(df["expiration"])
    if name == "META" and os.path.basename(path).startswith("META_"):
        # META rows before 2022-06-09 are the Roundhill Metaverse ETF — drop.
        df = df[df["date"] >= pd.Timestamp("2022-06-09")]
    df["dte"] = (df["exp"] - df["date"]).dt.days
    df = df[(df["dte"] >= DTE_MIN) & (df["dte"] <= DTE_MAX) & (df["yte"] > 0)]
    return df

# ---------------------------------------------------------------- per-smile prep
def parity_forward(g):
    """Median parity forward from near-ATM two-sided pairs. None if < 3 pairs."""
    spot = g["spot"].iloc[0]
    for band in (0.05, 0.15):
        m = ((g["strike"] / spot - 1).abs() <= band) & \
            (g["cBidPx"] > 0) & (g["pBidPx"] > 0)
        if m.sum() >= 3:
            gg = g[m]
            cm = (gg["cBidPx"] + gg["cAskPx"]).to_numpy() / 2
            pm = (gg["pBidPx"] + gg["pAskPx"]).to_numpy() / 2
            F = (cm - pm) * np.exp(gg["iRate"].to_numpy() * gg["yte"].to_numpy()) \
                + gg["strike"].to_numpy()
            return float(np.median(F))
    return None

def prep_smile(g):
    """Apply frozen filters; invert mid IVs. Returns (df, F) or (None, reason)."""
    F = parity_forward(g)
    if F is None or F <= 0:
        return None, "no_forward"
    is_call = g["strike"].to_numpy() >= F
    bid = np.where(is_call, g["cBidPx"], g["pBidPx"])
    ask = np.where(is_call, g["cAskPx"], g["pAskPx"])
    oi = np.where(is_call, g["cOi"], g["pOi"])
    mid = (bid + ask) / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(mid > 0, (ask - bid) / mid, np.inf)
    keep = (bid > 0) & (ask >= bid) & (oi > 0) & (rel <= MAX_RELSPREAD)
    if keep.sum() < MIN_STRIKES:
        return None, "few_strikes"
    gg = g[keep].copy()
    gg["is_call"] = is_call[keep]
    gg["side_bid"] = bid[keep]
    gg["side_ask"] = ask[keep]
    gg["mid"] = mid[keep]
    gg["F"] = F
    iv = implied_vol(gg["mid"].to_numpy(), np.full(len(gg), F),
                     gg["strike"].to_numpy(), gg["yte"].to_numpy(),
                     gg["iRate"].to_numpy(), gg["is_call"].to_numpy())
    gg["mid_iv"] = iv
    gg = gg[np.isfinite(iv) & (iv > 0.005) & (iv < 9.0)]
    if len(gg) < MIN_STRIKES:
        return None, "few_ivs"
    gg = gg.sort_values("strike")
    gg["k"] = np.log(gg["strike"].to_numpy() / F)
    return gg, None

def fit_smile(gg, method):
    """Fit + z-scores. Returns df with fit_iv, resid, z, vega or None."""
    k = gg["k"].to_numpy()
    iv = gg["mid_iv"].to_numpy()
    T = float(gg["yte"].iloc[0])
    f = FIT_FUNCS[method](k, iv, T)
    if f is None:
        return None
    resid = iv - f
    sd = resid.std(ddof=1)
    if not np.isfinite(sd) or sd < 1e-6:
        return None
    out = gg.copy()
    out["fit_iv"] = f
    out["resid"] = resid
    out["z"] = resid / sd
    out["vega"] = black76_vega(out["F"].to_numpy(), out["strike"].to_numpy(),
                               out["yte"].to_numpy(), out["iRate"].to_numpy(),
                               out["mid_iv"].to_numpy())
    out["rmse"] = float(np.sqrt((resid ** 2).mean()))
    out["n_strikes"] = len(out)
    return out

# ---------------------------------------------------------------- pass 1
P1_COLS = ["date", "exp", "dte", "strike", "is_call", "side_bid", "side_ask",
           "mid_iv", "fit_iv", "resid", "z", "vega", "F", "spot", "yte",
           "iRate", "rmse", "n_strikes"]

def pass1_name(name, method, years=None):
    outp = os.path.join(P1DIR, f"{name}.parquet")
    if os.path.exists(outp):
        log(f"pass1 {name}: checkpoint exists, skip")
        return outp
    t0 = time.time()
    rows = []
    stats = {"smiles": 0, "fit_fail": 0, "no_forward": 0, "few": 0}
    for path in files_for(name):
        yr = int(os.path.basename(path).rsplit("_", 1)[1].split(".")[0])
        if years and yr not in years:
            continue
        try:
            df = load_year(path, name)
        except Exception as e:
            log(f"pass1 {name} {yr}: LOAD ERROR {e}")
            continue
        if len(df) == 0:
            continue
        for (_, _), g in df.groupby(["date", "exp"], sort=False):
            gg, why = prep_smile(g)
            if gg is None:
                stats["no_forward" if why == "no_forward" else "few"] += 1
                continue
            fit = fit_smile(gg, method)
            if fit is None:
                stats["fit_fail"] += 1
                continue
            stats["smiles"] += 1
            rows.append(fit[P1_COLS])
    if rows:
        res = pd.concat(rows, ignore_index=True)
    else:
        res = pd.DataFrame(columns=P1_COLS)
    res.to_parquet(outp, index=False)
    log(f"pass1 {name}: {stats['smiles']} smiles fit ({stats['fit_fail']} fit-fail, "
        f"{stats['no_forward']} no-forward, {stats['few']} too-few) "
        f"{len(res)} strike-rows in {time.time()-t0:.0f}s")
    return outp

def cmd_pass1(method):
    for i, name in enumerate(universe()):
        log(f"pass1 [{i+1}/{len(universe())}] {name}")
        pass1_name(name, method)

# ---------------------------------------------------------------- selection
def cmd_select():
    """Cross-universe daily ranking with frozen cap. Writes selected.parquet
    and candidates.parquet (for the dislocation-vs-spread diagnostic)."""
    cands = []
    for name in universe():
        p = os.path.join(P1DIR, f"{name}.parquet")
        if not os.path.exists(p):
            log(f"select: MISSING pass1 for {name} — aborting")
            sys.exit(1)
        df = pd.read_parquet(p)
        if len(df) == 0:
            continue
        df = df.sort_values(["date", "exp", "strike"]).reset_index(drop=True)
        gkey = df.groupby(["date", "exp"], sort=False)
        # adjacent fitted strikes (wings) — edge strikes have none
        df["k_lo"] = gkey["strike"].shift(1)
        df["k_hi"] = gkey["strike"].shift(-1)
        df["wlo_bid"] = gkey["side_bid"].shift(1)
        df["whi_bid"] = gkey["side_bid"].shift(-1)
        c = df[(df["z"].abs() >= Z_ENTRY)
               & df["k_lo"].notna() & df["k_hi"].notna()].copy()
        if len(c) == 0:
            continue
        c["name"] = name
        cands.append(c)
    cand = pd.concat(cands, ignore_index=True)
    cand["direction"] = np.where(cand["z"] > 0, "long_fly", "short_fly")
    cand.to_parquet(os.path.join(SCRATCH, "candidates.parquet"), index=False)
    log(f"select: {len(cand)} raw candidates (|z|>={Z_ENTRY}, wings exist) "
        f"across {cand['name'].nunique()} names, {cand['date'].nunique()} days")

    # daily cap: rank by |z|, no identical re-entry within REENTRY_CAL_DAYS
    cand = cand.sort_values(["date", "z"], key=lambda s: s.abs() if s.name == "z" else s,
                            ascending=[True, False])
    last_entry = {}
    picks = []
    for date, day in cand.groupby("date", sort=True):
        day = day.reindex(day["z"].abs().sort_values(ascending=False).index)
        n = 0
        for _, r in day.iterrows():
            if n >= MAX_PER_DAY:
                break
            key = (r["name"], r["exp"], r["strike"], r["direction"])
            if key in last_entry and (date - last_entry[key]).days < REENTRY_CAL_DAYS:
                continue
            last_entry[key] = date
            picks.append(r)
            n += 1
    sel = pd.DataFrame(picks).reset_index(drop=True)
    sel.to_parquet(os.path.join(SCRATCH, "selected.parquet"), index=False)
    log(f"select: {len(sel)} selected trades after {MAX_PER_DAY}/day cap "
        f"+ {REENTRY_CAL_DAYS}d re-entry block; "
        f"{sel['name'].nunique()} names, {sel['date'].nunique()} days, "
        f"z range {sel['z'].min():.2f}..{sel['z'].max():.2f}")

# ---------------------------------------------------------------- pass 2
def _leg_quotes(chain, date, exp, K, use_call):
    """(bid, ask, mid) for one leg from the raw chain, NaN-safe."""
    try:
        row = chain.loc[(date, exp, K)]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    b = row["cBidPx"] if use_call else row["pBidPx"]
    a = row["cAskPx"] if use_call else row["pAskPx"]
    if not (np.isfinite(b) and np.isfinite(a)) or a <= 0 or a < b:
        return None
    return float(b), float(a), (float(b) + float(a)) / 2.0

def _fly_value(chain, date, exp, K1, K2, K3, use_call, entering, is_long):
    """Fly execution value per share. Long fly = +K1 +K3 -2*K2.
    entering & long  -> pay: wings at ask, middle at bid  (net debit)
    exiting  & long  -> receive: wings at bid, middle at ask
    Short fly mirrors. Returns (bidask_value, mid_value, legs_found) where
    value is SIGNED cash flow of the LONG-fly portfolio (debit positive)."""
    q1 = _leg_quotes(chain, date, exp, K1, use_call)
    q2 = _leg_quotes(chain, date, exp, K2, use_call)
    q3 = _leg_quotes(chain, date, exp, K3, use_call)
    if q1 is None or q2 is None or q3 is None:
        return None
    buy_wings = (is_long and entering) or (not is_long and not entering)
    if buy_wings:   # buy wings at ask, sell middle at bid
        ba = q1[1] + q3[1] - 2 * q2[0]
    else:           # sell wings at bid, buy middle at ask
        ba = q1[0] + q3[0] - 2 * q2[1]
    mid = q1[2] + q3[2] - 2 * q2[2]
    return ba, mid

def _fly_payoff_nodes(K1, K2, K3, use_call):
    """Terminal payoff of the long-fly portfolio at its extreme points."""
    nodes = [0.0, K1, K2, K3, K3 * 10 + 1000]
    def intr(K, S):
        return max(S - K, 0.0) if use_call else max(K - S, 0.0)
    pays = [intr(K1, S) + intr(K3, S) - 2 * intr(K2, S) for S in nodes]
    return min(pays), max(pays)

def cmd_pass2():
    sel = pd.read_parquet(os.path.join(SCRATCH, "selected.parquet"))
    trades = []
    skipped = {"entry_quotes": 0, "capital": 0}
    for name, snm in sel.groupby("name", sort=True):
        t0 = time.time()
        years_needed = sorted({d.year for d in snm["date"]} |
                              {(d + pd.Timedelta(days=21)).year for d in snm["date"]})
        raw = []
        for path in files_for(name):
            yr = int(os.path.basename(path).rsplit("_", 1)[1].split(".")[0])
            if yr not in years_needed:
                continue
            df = load_year(path, name, cols=["trade_date", "expiration", "strike",
                                             "cBidPx", "cAskPx", "pBidPx", "pAskPx",
                                             "spot", "yte", "iRate"])
            exps = set(snm["exp"])
            df = df[df["exp"].isin(exps)]
            raw.append(df)
        if not raw:
            continue
        raw = pd.concat(raw, ignore_index=True)
        chain = raw.set_index(["date", "exp", "strike"]).sort_index()
        tdates = np.array(sorted(raw["date"].unique()))
        # z lookup from pass1
        p1 = pd.read_parquet(os.path.join(P1DIR, f"{name}.parquet"),
                             columns=["date", "exp", "strike", "z"])
        zmap = p1.set_index(["date", "exp", "strike"])["z"]
        zmap = zmap[~zmap.index.duplicated()]

        for _, r in snm.iterrows():
            K1, K2, K3 = float(r["k_lo"]), float(r["strike"]), float(r["k_hi"])
            exp, ed = r["exp"], r["date"]
            use_call = bool(K2 >= r["F"])   # legs in the middle strike's OTM type
            is_long = r["direction"] == "long_fly"
            ev = _fly_value(chain, ed, exp, K1, K2, K3, use_call, True, is_long)
            if ev is None:
                skipped["entry_quotes"] += 1
                continue
            entry_ba, entry_mid = ev
            minpay, maxpay = _fly_payoff_nodes(K1, K2, K3, use_call)
            if is_long:
                cap_ba = entry_ba - minpay      # debit paid + worst wing asymmetry
                cap_mid = entry_mid - minpay
            else:
                # short fly: credit received = entry_ba (signed long-fly value we sold at);
                # max loss = peak terminal payoff we owe minus the credit.
                cap_ba = maxpay - entry_ba
                cap_mid = maxpay - entry_mid
            if cap_ba <= 0.005 or cap_mid <= 0.005:
                skipped["capital"] += 1
                continue
            # lifecycle
            idx = np.searchsorted(tdates, np.datetime64(ed))
            marks = tdates[idx + 1: idx + 1 + MAX_HOLD_TD + 3]
            exit_date = exit_reason = None
            exit_ba = exit_mid = z_exit = np.nan
            stale = False
            held = 0
            last_val = None
            for j, md in enumerate(marks, start=1):
                mdt = pd.Timestamp(md)
                dte_now = (exp - mdt).days
                zval = zmap.get((mdt, exp, K2), np.nan)
                xv = _fly_value(chain, mdt, exp, K1, K2, K3, use_call, False, is_long)
                if xv is not None:
                    last_val = (mdt, xv, j)
                trig = None
                if np.isfinite(zval) and abs(zval) < Z_EXIT:
                    trig = "z_convergence"
                elif dte_now <= EXIT_DTE:
                    trig = "dte"
                elif j >= MAX_HOLD_TD:
                    trig = "time"
                if trig:
                    if xv is None:
                        if last_val is None:
                            continue   # cannot mark yet; hold to next day
                        mdt, xv, j = last_val
                        stale = True
                    exit_date, exit_reason, held = mdt, trig, j
                    exit_ba, exit_mid = xv
                    z_exit = zval
                    break
            if exit_date is None:
                if last_val is None:
                    skipped["entry_quotes"] += 1
                    continue
                exit_date, (exit_ba, exit_mid), held = last_val
                exit_reason, stale = "forced_last_mark", True
            # P&L per fly (x100 multiplier); signed long-fly cash flows:
            # long fly:  pnl = exit_value - entry_value
            # short fly: pnl = entry_value - exit_value  (we sold the fly)
            sgn = 1.0 if is_long else -1.0
            pnl_ba = sgn * (exit_ba - entry_ba) * 100.0
            pnl_mid = sgn * (exit_mid - entry_mid) * 100.0
            cap = cap_ba * 100.0
            disloc = abs(r["resid"]) * r["vega"] * 2 * 100.0
            # entry cost = adverse of executing vs mid; for long fly entry_ba >= entry_mid
            entry_cost = abs(entry_ba - entry_mid) * 100.0
            exit_cost = abs(exit_ba - exit_mid) * 100.0
            trades.append(dict(
                name=name, entry_date=ed, exit_date=exit_date, exp=exp,
                dte_entry=int(r["dte"]), K1=K1, K2=K2, K3=K3,
                otype="C" if use_call else "P", direction=r["direction"],
                z_entry=float(r["z"]), z_exit=float(z_exit) if np.isfinite(z_exit) else np.nan,
                exit_reason=exit_reason, held_td=held, stale_exit=stale,
                entry_ba=entry_ba * 100, entry_mid=entry_mid * 100,
                exit_ba=exit_ba * 100, exit_mid=exit_mid * 100,
                pnl_ba=pnl_ba, pnl_mid=pnl_mid, capital=cap,
                r_ba=pnl_ba / cap, r_mid=pnl_mid / cap,
                mid_iv=float(r["mid_iv"]), fit_iv=float(r["fit_iv"]),
                resid_iv=float(r["resid"]), vega=float(r["vega"]),
                disloc_usd=disloc, entry_cost_usd=entry_cost, exit_cost_usd=exit_cost,
                spot=float(r["spot"]), F=float(r["F"]),
            ))
        log(f"pass2 {name}: {len(snm)} selected -> cumulative {len(trades)} trades "
            f"({time.time()-t0:.0f}s)")
    tr = pd.DataFrame(trades)
    tr.to_parquet(os.path.join(SCRATCH, "trades.parquet"), index=False)
    tr.to_csv(CSVP, index=False)
    log(f"pass2: {len(tr)} executed trades ({skipped['entry_quotes']} skipped no-entry-quotes, "
        f"{skipped['capital']} skipped nonpositive-capital). CSV -> {CSVP}")

# ---------------------------------------------------------------- report
def _era_of(y):
    for lbl, a, b in ERAS:
        if a <= y <= b:
            return lbl
    return "other"

def cmd_report(method):
    tr = pd.read_parquet(os.path.join(SCRATCH, "trades.parquet"))
    cand = pd.read_parquet(os.path.join(SCRATCH, "candidates.parquet"))
    tr["year"] = pd.to_datetime(tr["entry_date"]).dt.year
    tr["era"] = tr["year"].map(_era_of)
    n = len(tr)
    L = []
    A = L.append
    A("# SVI Surface Relative-Value Study (B2) — 2026-07-06")
    A("")
    A(f"Method: **{method}** smile fit; mid-derived IVs (Black-76 on parity forward); "
      f"frozen design as pre-registered. Trades: per-trade CSV at `{os.path.basename(CSVP)}`.")
    A("")
    mean_ba, mean_mid = tr["r_ba"].mean(), tr["r_mid"].mean()
    win_ba = (tr["pnl_ba"] > 0).mean()
    A("## Headline")
    A("")
    A(f"- n = {n} executed trades ({tr['name'].nunique()} names, "
      f"{tr['entry_date'].min().date()} .. {tr['entry_date'].max().date()})")
    A(f"- mean r (BID/ASK, primary) = **{mean_ba:+.4f}**  |  mean r (MID, diagnostic) = {mean_mid:+.4f}")
    A(f"- total P&L: BID/ASK ${tr['pnl_ba'].sum():,.0f}  |  MID ${tr['pnl_mid'].sum():,.0f}")
    A(f"- win rate (BID/ASK): {win_ba:.1%}  |  median hold {tr['held_td'].median():.0f} td")
    A("")
    # per year
    A("## Per-year (BID/ASK)")
    A("")
    A("| year | n | mean r | total $ | win% |")
    A("|---|---|---|---|---|")
    yr_ok = 0
    yr_tot = 0
    for y, g in tr.groupby("year"):
        flag = ""
        if len(g) >= 20:
            yr_tot += 1
            if g["r_ba"].mean() > 0:
                yr_ok += 1
                flag = " +"
        A(f"| {y} | {len(g)} | {g['r_ba'].mean():+.4f}{flag} | "
          f"{g['pnl_ba'].sum():,.0f} | {(g['pnl_ba']>0).mean():.0%} |")
    A("")
    # per era
    A("## Per-era (BID/ASK)")
    A("")
    A("| era | n | mean r | total $ | win% | mean r MID |")
    A("|---|---|---|---|---|---|")
    era_pos = 0
    for lbl, a, b in ERAS:
        g = tr[tr["era"] == lbl]
        if len(g) == 0:
            A(f"| {lbl} | 0 | — | — | — | — |")
            continue
        if g["r_ba"].mean() > 0:
            era_pos += 1
        A(f"| {lbl} | {len(g)} | {g['r_ba'].mean():+.4f} | {g['pnl_ba'].sum():,.0f} | "
          f"{(g['pnl_ba']>0).mean():.0%} | {g['r_mid'].mean():+.4f} |")
    A("")
    # verdict (frozen)
    c1 = mean_ba > 0 and n >= 300
    c2 = yr_tot > 0 and (yr_ok / yr_tot) >= 0.60
    c3 = era_pos >= 3
    verdict = "CANDIDATE" if (c1 and c2 and c3) else "FAIL"
    A("## Verdict (frozen rule)")
    A("")
    A(f"1. mean r > 0 at BID/ASK with n >= 300: **{'PASS' if c1 else 'FAIL'}** "
      f"(mean r = {mean_ba:+.4f}, n = {n})")
    A(f"2. positive mean in >= 60% of years with >= 20 trades: **{'PASS' if c2 else 'FAIL'}** "
      f"({yr_ok}/{yr_tot})")
    A(f"3. positive in >= 3 of 5 eras: **{'PASS' if c3 else 'FAIL'}** ({era_pos}/5)")
    A("")
    A(f"### VERDICT: **{verdict}**")
    A("")
    # concentration
    A("## Concentration")
    A("")
    byname = tr.groupby("name")["pnl_ba"].sum().sort_values()
    top = byname.abs().idxmax()
    ex = tr[tr["name"] != top]
    A(f"- largest-|P&L| name: {top} (${byname[top]:,.0f}); "
      f"ex-{top}: mean r = {ex['r_ba'].mean():+.4f}, total ${ex['pnl_ba'].sum():,.0f}")
    A(f"- top-5 names by |P&L|: " + ", ".join(
        f"{k} ${v:,.0f}" for k, v in byname.reindex(byname.abs().sort_values(ascending=False).index[:5]).items()))
    A("")
    A("## Holding-period / exit mix")
    A("")
    A("| exit_reason | n | mean r (BA) | mean held td |")
    A("|---|---|---|---|")
    for k, g in tr.groupby("exit_reason"):
        A(f"| {k} | {len(g)} | {g['r_ba'].mean():+.4f} | {g['held_td'].mean():.1f} |")
    A(f"\nheld_td distribution: {tr['held_td'].value_counts().sort_index().to_dict()}")
    A(f"stale-exit marks: {int(tr['stale_exit'].sum())} / {n}")
    A("")
    # dislocation vs spread — the cost wall
    A("## Dislocation vs spread (the cost wall) — ALL candidates")
    A("")
    cd = cand.copy()
    cd["disloc_usd"] = cd["resid"].abs() * cd["vega"] * 2 * 100
    q = cd["disloc_usd"].quantile([.1, .25, .5, .75, .9]).round(1).to_dict()
    A(f"- candidates: {len(cd)}; |dislocation| $ per fly (2x100x|resid|xvega): "
      f"p10 {q[.1]}, p25 {q[.25]}, p50 {q[.5]}, p75 {q[.75]}, p90 {q[.9]}")
    if n:
        ec = tr["entry_cost_usd"] + tr["exit_cost_usd"]
        A(f"- executed trades round-trip bid/ask cost $ per fly: "
          f"p10 {ec.quantile(.1):.0f}, p50 {ec.median():.0f}, p90 {ec.quantile(.9):.0f}")
        cover = (tr["disloc_usd"] > ec).mean()
        A(f"- fraction of executed trades where |dislocation| exceeds round-trip cost: "
          f"**{cover:.1%}** (dislocation must ALSO fully converge to be captured)")
        A(f"- median dislocation/round-trip-cost ratio: "
          f"{(tr['disloc_usd']/ec.replace(0,np.nan)).median():.2f}")
    A("")
    with open(MDP, "a") as f:
        f.write("\n".join(L) + "\n")
    log(f"report: verdict {verdict} (c1={c1} c2={c2} c3={c3}) -> {MDP}")
    print("\n".join(L))

# ---------------------------------------------------------------- pilots & checks
def cmd_check_iv():
    """Mandated empirical check: are the store's IV columns smoothed?
    Compare mid-derived IVs vs call_iv/put_iv and vs smv_vol."""
    cols = COLS + ["call_iv", "put_iv", "smv_vol"]
    out = []
    for tk, yr, day in [("AAPL", 2024, "6/3/2024"), ("SPX", 2016, "6/1/2016"),
                        ("MSFT", 2018, "6/1/2018"), ("NVDA", 2022, "6/1/2022")]:
        df = pd.read_parquet(os.path.join(DATA, f"{tk}_{yr}.parquet"), columns=cols)
        df["date"] = parse_dates(df["trade_date"])
        df["exp"] = parse_dates(df["expiration"])
        df["dte"] = (df["exp"] - df["date"]).dt.days
        d = df[(df["trade_date"] == day) & (df["dte"] >= DTE_MIN) & (df["dte"] <= DTE_MAX)]
        for (_, _), g in d.groupby(["date", "exp"], sort=False):
            gg, why = prep_smile(g)
            if gg is None:
                continue
            store_iv = np.where(gg["is_call"], gg["call_iv"], gg["put_iv"])
            m = store_iv > 0
            if m.sum() < 5:
                continue
            dd_store = np.abs(gg["mid_iv"].to_numpy()[m] - store_iv[m])
            dd_smv = np.abs(gg["mid_iv"].to_numpy()[m] - gg["smv_vol"].to_numpy()[m])
            out.append((tk, day, int(gg['dte'].iloc[0]), m.sum(),
                        float(np.median(dd_store)), float(np.median(dd_smv))))
    res = pd.DataFrame(out, columns=["ticker", "day", "dte", "n",
                                     "med|mine - store_iv|", "med|mine - smv_vol|"])
    log("check-iv results (vol points):\n" + res.to_string(index=False))
    agg = res[["med|mine - store_iv|", "med|mine - smv_vol|"]].median()
    log(f"check-iv AGGREGATE medians: vs raw store IV {agg.iloc[0]:.4f}, "
        f"vs smv_vol {agg.iloc[1]:.4f}")
    return res

def cmd_pilot_method():
    """3-day pilot: SVI vs quadratic on every qualifying smile, full universe."""
    days = [("2012-06-15", "6/15/2012", 2012), ("2018-02-06", "2/6/2018", 2018),
            ("2024-06-03", "6/3/2024", 2024)]
    stats = {m: dict(fit=0, fail=0, rmse=[], ztail=0, nstrk=[], t=0.0)
             for m in ("svi", "quad")}
    for name in universe():
        for _, dstr, yr in days:
            paths = [p for p in files_for(name) if p.endswith(f"_{yr}.parquet")]
            if not paths:
                continue
            try:
                df = load_year(paths[0], name)
            except Exception:
                continue
            d = df[df["trade_date"] == dstr]
            for (_, _), g in d.groupby(["date", "exp"], sort=False):
                gg, why = prep_smile(g)
                if gg is None:
                    continue
                for m in ("svi", "quad"):
                    t0 = time.time()
                    fit = fit_smile(gg, m)
                    stats[m]["t"] += time.time() - t0
                    if fit is None:
                        stats[m]["fail"] += 1
                        continue
                    stats[m]["fit"] += 1
                    stats[m]["rmse"].append(fit["rmse"].iloc[0])
                    stats[m]["ztail"] += int((fit["z"].abs() >= Z_ENTRY).sum())
                    stats[m]["nstrk"].append(len(fit))
    for m in ("svi", "quad"):
        s = stats[m]
        log(f"pilot-method [{m}]: {s['fit']} fits, {s['fail']} failures, "
            f"median RMSE {np.median(s['rmse']):.4f} vol pts, "
            f"|z|>=2 hits {s['ztail']} ({s['ztail']/max(sum(s['nstrk']),1):.2%} of strikes), "
            f"total fit time {s['t']:.1f}s")
    return stats

def cmd_pilot_runtime(method):
    for name in ["AAPL", "MU", "SPX"]:
        p = os.path.join(P1DIR, f"{name}.parquet")
        if os.path.exists(p):
            os.remove(p)
        t0 = time.time()
        pass1_name(name, method)
        log(f"pilot-runtime {name}: {time.time()-t0:.0f}s full history")

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    method = sys.argv[2] if len(sys.argv) > 2 else "svi"
    t0 = time.time()
    log(f"=== svi_rv {cmd} (method={method}) start ===")
    if cmd == "check-iv":
        cmd_check_iv()
    elif cmd == "pilot-method":
        cmd_pilot_method()
    elif cmd == "pilot-runtime":
        cmd_pilot_runtime(method)
    elif cmd == "pass1":
        cmd_pass1(method)
    elif cmd == "select":
        cmd_select()
    elif cmd == "pass2":
        cmd_pass2()
    elif cmd == "report":
        cmd_report(method)
    elif cmd == "full":
        cmd_pass1(method)
        cmd_select()
        cmd_pass2()
        cmd_report(method)
    else:
        print(__doc__)
    log(f"=== svi_rv {cmd} done in {time.time()-t0:.0f}s ===")
