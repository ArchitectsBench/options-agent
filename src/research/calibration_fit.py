"""Confidence calibration remap fitter (self-training layer #2 — PROPOSE ONLY).

The readiness gates in calibration_report.py passed (n >= 150, conf std >= 0.05,
>= 14 label days, >= 2 regime biases), so this tool fits the isotonic remap of
realized win rate on stated local_confidence — and STOPS at a proposal. It
NEVER touches the live path: no edits to src/engine/, src/indicators/, or
config/config.yaml, and it writes only under logs/research/ and
config/*.proposed. Applying the map is a human decision.

Pre-specified protocol (do not tune):
  - primary target:   win      := cf_eod > 0            (direction-signed
                       counterfactual underlying return, NOT option P&L)
  - sensitivity only: win_cost := cf_eod > 0.002         (~20bp cost floor)
  - fit: isotonic regression (pool-adjacent-violators) of win rate on
    local_confidence, pooled across ALL candidate rows; confidence takes few
    discrete values so this is per-bin empirical rates + monotonicity
  - validation: leave-one-day-out CV; Brier of raw vs remapped confidence
  - segmentation (report-only): same table by strategy and by status; the
    PROPOSED map is the pooled one regardless (pre-committed, no slicing)
  - threshold impact: raw level corresponding to remapped 0.75 (or "unreachable"),
    gate population per day at remapped >= 0.5 / 0.6 / 0.75 vs raw >= 0.75

Outputs:
  logs/research/calibration_proposal_YYYYMMDD.md   (human review document)
  config/calibration_remap.yaml.proposed           (machine-readable, NOT live)

Run: .venv/bin/python -m src.research.calibration_fit [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OBS_DIR = ROOT / "logs" / "observation"
RESEARCH_DIR = ROOT / "logs" / "research"
PROPOSED_YAML = ROOT / "config" / "calibration_remap.yaml.proposed"

WIN_EPS = 0.0          # primary target: cf_eod > 0
WIN_COST_EPS = 0.002   # sensitivity target only — NOT used for the proposed map
CLAUDE_GATE = 0.75     # config invoke_threshold (read-only reference)
GATE_LEVELS = (0.5, 0.6, 0.75)


# ── data ─────────────────────────────────────────────────────────────────────

def load_candidates(obs_dir: Path = OBS_DIR) -> list[dict]:
    """All labeled candidate rows with a usable confidence + outcome."""
    rows = []
    for path in sorted(obs_dir.glob("labels-*.jsonl")):
        day = path.stem.replace("labels-", "")
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("kind") != "candidate":
                continue
            conf, eod = r.get("local_confidence"), r.get("cf_eod")
            if not isinstance(conf, (int, float)) or not isinstance(eod, (int, float)):
                continue  # schema drift guard: skip rows missing either field
            r["_day"] = day
            rows.append(r)
    return rows


def strategy_group(row: dict) -> str:
    s = row.get("strategy")
    return s if s in ("0dte_directional", "debit_spread") else "other"


def status_group(row: dict) -> str:
    s = row.get("status") or ""
    return "actionable" if s.startswith("posted") else "skipped/vetoed"


# ── isotonic regression (pool-adjacent-violators), direct numpy ─────────────

def pav(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted isotonic (non-decreasing) fit of y, in the given order.

    Standard stack-based pool-adjacent-violators. Returns the fitted value
    for each input position.
    """
    y = np.asarray(y, dtype=float)
    w = np.asarray(w, dtype=float)
    if y.shape != w.shape or y.ndim != 1:
        raise ValueError("y and w must be 1-D arrays of equal length")
    if len(y) == 0:
        return y.copy()
    if np.any(w <= 0):
        raise ValueError("weights must be positive")
    # blocks: (weighted mean, total weight, count of source points)
    means: list[float] = []
    weights: list[float] = []
    counts: list[int] = []
    for yi, wi in zip(y, w):
        means.append(float(yi)); weights.append(float(wi)); counts.append(1)
        while len(means) > 1 and means[-2] > means[-1]:
            m2, w2, c2 = means.pop(), weights.pop(), counts.pop()
            m1, w1, c1 = means.pop(), weights.pop(), counts.pop()
            wt = w1 + w2
            means.append((m1 * w1 + m2 * w2) / wt)
            weights.append(wt)
            counts.append(c1 + c2)
    out = np.empty_like(y)
    i = 0
    for m, c in zip(means, counts):
        out[i:i + c] = m
        i += c
    return out


def fit_isotonic_map(rows: list[dict], eps: float = WIN_EPS
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fit win-rate-vs-confidence isotonic map on candidate rows.

    Returns (xs, emp, fitted, ns): distinct raw confidences (sorted), the
    empirical win rate per bin, the PAV-fitted (monotone) value per bin, and
    the row count per bin.
    """
    conf = np.array([r["local_confidence"] for r in rows], dtype=float)
    win = np.array([1.0 if r["cf_eod"] > eps else 0.0 for r in rows])
    xs, inv = np.unique(conf, return_inverse=True)
    ns = np.bincount(inv, minlength=len(xs)).astype(float)
    emp = np.bincount(inv, weights=win, minlength=len(xs)) / ns
    fitted = pav(emp, ns)
    return xs, emp, fitted, ns


def remap(raw: np.ndarray | float, xs: np.ndarray, fitted: np.ndarray) -> np.ndarray:
    """Calibrated confidence for raw confidence(s): linear interpolation
    between fitted breakpoints, clamped to the endpoints outside the range."""
    return np.interp(raw, xs, fitted)


def invert_threshold(xs: np.ndarray, fitted: np.ndarray, target: float) -> float | None:
    """Smallest raw confidence whose remapped value reaches `target`.
    None if the map never reaches it (target unreachable)."""
    if len(xs) == 0 or fitted[-1] < target:
        return None
    if fitted[0] >= target:
        return float(xs[0])
    i = int(np.searchsorted(fitted, target, side="left"))  # fitted[i] >= target
    x0, x1, y0, y1 = xs[i - 1], xs[i], fitted[i - 1], fitted[i]
    if y1 == y0:  # flat segment already at target (shouldn't happen given branch)
        return float(x1)
    return float(x0 + (target - y0) * (x1 - x0) / (y1 - y0))


# ── validation ───────────────────────────────────────────────────────────────

def brier(pred: np.ndarray, outcome: np.ndarray) -> float:
    return float(np.mean((np.asarray(pred) - np.asarray(outcome)) ** 2))


def loo_cv(rows: list[dict], eps: float = WIN_EPS) -> dict:
    """Leave-one-day-out CV: fit on the other days, remap the held-out day.
    Returns pooled Brier for raw vs CV-remapped confidence, plus per-bin
    weighted-mean CV-fitted values (for the reliability table)."""
    days = sorted({r["_day"] for r in rows})
    all_conf = np.array([r["local_confidence"] for r in rows])
    all_win = np.array([1.0 if r["cf_eod"] > eps else 0.0 for r in rows])
    cv_pred = np.full(len(rows), np.nan)
    cv_base = np.full(len(rows), np.nan)  # constant train-fold base rate baseline
    day_arr = np.array([r["_day"] for r in rows])
    for day in days:
        held = day_arr == day
        train = [r for r, h in zip(rows, held) if not h]
        xs, _, fitted, _ = fit_isotonic_map(train, eps)
        cv_pred[held] = remap(all_conf[held], xs, fitted)
        cv_base[held] = np.mean(all_win[~held])
    assert not np.any(np.isnan(cv_pred))
    # per-bin weighted mean CV prediction (each bin spans several folds)
    xs_all = np.unique(all_conf)
    cv_by_bin = {float(x): float(np.mean(cv_pred[all_conf == x])) for x in xs_all}
    return {
        "days": days,
        "brier_raw": brier(all_conf, all_win),
        "brier_cv_remapped": brier(cv_pred, all_win),
        "brier_cv_base_rate": brier(cv_base, all_win),
        "cv_fitted_by_bin": cv_by_bin,
    }


# ── report pieces ────────────────────────────────────────────────────────────

def reliability_rows(rows: list[dict], eps: float = WIN_EPS,
                     cv_by_bin: dict | None = None) -> list[dict]:
    xs, emp, fitted, ns = fit_isotonic_map(rows, eps)
    out = []
    for x, e, f, n in zip(xs, emp, fitted, ns):
        rec = {"raw_conf": float(x), "n": int(n),
               "empirical_win_rate": round(float(e), 4),
               "fitted": round(float(f), 4)}
        if cv_by_bin is not None:
            rec["cv_fitted"] = round(cv_by_bin.get(float(x), float("nan")), 4)
        out.append(rec)
    return out


def gate_analysis(rows: list[dict], xs: np.ndarray, fitted: np.ndarray) -> dict:
    n_days = len({r["_day"] for r in rows})
    conf = np.array([r["local_confidence"] for r in rows])
    mapped = remap(conf, xs, fitted)
    raw_cross = int(np.sum(conf >= CLAUDE_GATE))
    out = {
        "n_days": n_days,
        "raw_gate_0.75": {"total": raw_cross, "per_day": round(raw_cross / n_days, 1)},
        "remapped_gates": {},
        "raw_conf_for_remapped_0.75": invert_threshold(xs, fitted, CLAUDE_GATE),
        "map_max": float(fitted[-1]) if len(fitted) else None,
    }
    for t in GATE_LEVELS:
        cross = int(np.sum(mapped >= t))
        out["remapped_gates"][t] = {
            "total": cross, "per_day": round(cross / n_days, 1),
            "raw_conf_equivalent": invert_threshold(xs, fitted, t),
        }
    return out


def segment_tables(rows: list[dict], eps: float = WIN_EPS) -> dict:
    segs = {"by_strategy": {}, "by_status": {}}
    for key, fn in (("by_strategy", strategy_group), ("by_status", status_group)):
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(fn(r), []).append(r)
        for g, grows in sorted(groups.items()):
            segs[key][g] = {"n": len(grows),
                            "win_rate": round(float(np.mean(
                                [1.0 if r["cf_eod"] > eps else 0.0 for r in grows])), 4),
                            "table": reliability_rows(grows, eps)}
    return segs


# ── artifact writers ─────────────────────────────────────────────────────────

def _table_md(rows: list[dict], cv: bool = False) -> str:
    hdr = "| raw conf | n | empirical win rate | fitted |"
    sep = "|---|---|---|---|"
    if cv:
        hdr += " CV fitted |"
        sep += "---|"
    lines = [hdr, sep]
    for r in rows:
        line = (f"| {r['raw_conf']:.2f} | {r['n']} | "
                f"{r['empirical_win_rate']:.3f} | {r['fitted']:.3f} |")
        if cv:
            line += f" {r['cv_fitted']:.3f} |"
        lines.append(line)
    return "\n".join(lines)


def write_proposal_md(path: Path, result: dict) -> None:
    r = result
    ga = r["gate_analysis"]
    fit_rows = r["reliability_primary"]
    inv75 = ga["raw_conf_for_remapped_0.75"]
    inv75_txt = (f"raw ≈ {inv75:.3f}" if inv75 is not None
                 else f"UNREACHABLE — the fitted map tops out at "
                      f"{ga['map_max']:.3f}, below 0.75")
    seg = r["segments"]

    def seg_md(d):
        parts = []
        for g, s in d.items():
            parts.append(f"**{g}** (n={s['n']}, pooled win rate {s['win_rate']:.3f})\n\n"
                         + _table_md(s["table"]))
        return "\n\n".join(parts)

    gates_md = "\n".join(
        f"| remapped >= {t} | {v['total']} | {v['per_day']}/day | "
        + (f"raw ≈ {v['raw_conf_equivalent']:.3f}" if v["raw_conf_equivalent"] is not None
           else "unreachable") + " |"
        for t, v in ga["remapped_gates"].items())

    md = f"""# Confidence Calibration Remap — PROPOSAL {r['fit_date']}

> ## ⚠ NOT APPLIED — awaiting user review
> This is a measure-and-propose artifact. Nothing in the live path
> (src/engine/, src/indicators/, config/config.yaml) has been modified.
> The machine-readable map is `config/calibration_remap.yaml.proposed`
> (note the `.proposed` suffix — no live `calibration_remap.yaml` exists).

## Method (pre-specified protocol, not tuned)

- **Data:** all `kind: candidate` rows in `logs/observation/labels-*.jsonl`
  with a numeric `local_confidence` and `cf_eod`: **n = {r['n']}** rows across
  **{r['n_days']} labeled days** ({r['days'][0]} … {r['days'][-1]}).
- **Primary target:** `win := cf_eod > 0` — direction-signed COUNTERFACTUAL
  underlying return from next-bar open (`underlying_directional_proxy`), NOT
  option P&L.
- **Secondary target (sensitivity only, not proposed):** `win_cost := cf_eod > 0.002`.
- **Fit:** weighted isotonic regression (pool-adjacent-violators, numpy) of
  empirical win rate on `local_confidence`, pooled across all rows. Raw
  confidence takes {len(fit_rows)} discrete values, so this is per-bin
  empirical rates with monotonicity enforced; between breakpoints the map
  interpolates linearly and clamps at the endpoints.
- **Validation:** leave-one-day-out CV ({r['n_days']} folds) — fit on the
  other days, remap the held-out day, pooled Brier score.

## Headline calibration numbers

| metric | value |
|---|---|
| mean raw confidence | {r['conf_mean']:.3f} |
| pooled realized win rate (cf_eod > 0) | {r['win_rate']:.3f} |
| Brier, raw confidence | {r['brier_raw']:.4f} |
| Brier, remapped (LOO-CV) | {r['brier_cv_remapped']:.4f} |
| Brier, constant base rate (LOO-CV baseline) | {r['brier_cv_base_rate']:.4f} |
| Brier, coin flip @ 0.5 (reference) | 0.2500 |

Raw confidence is grossly optimistic: it averages {r['conf_mean']:.2f} against a
{r['win_rate']:.2f} realized win rate, and its Brier ({r['brier_raw']:.3f}) is
WORSE than an uninformative constant 0.5 (0.25). The remap fixes the level —
but note the constant-base-rate baseline: out of sample the remapped score is
{'no better than' if r['brier_cv_remapped'] >= r['brier_cv_base_rate'] else 'only marginally better than'}
just predicting the pooled win rate for every candidate. The confidence score
carries essentially no *ranking* information on this target; the remap's value
is level correction, not discrimination.

## Reliability table (primary target, pooled)

{_table_md(fit_rows, cv=True)}

## Proposed mapping (pooled primary map — the only map proposed this round)

raw confidence → calibrated confidence (linear interpolation between rows,
clamped outside the range):

{_table_md([{**row, 'empirical_win_rate': row['empirical_win_rate']} for row in fit_rows])}

(The `fitted` column IS the proposed calibrated value at each breakpoint.)

## Sensitivity: cost-floor target (win := cf_eod > 0.002) — NOT proposed

{_table_md(r['reliability_cost'])}

## Threshold impact analysis

- Current Claude gate: raw confidence >= {CLAUDE_GATE} (config `invoke_threshold`).
- Under the proposed map, **remapped 0.75 corresponds to {inv75_txt}**.

| gate | historical candidates crossing | per day | raw-conf equivalent |
|---|---|---|---|
| raw >= 0.75 (current) | {ga['raw_gate_0.75']['total']} | {ga['raw_gate_0.75']['per_day']}/day | raw 0.750 |
{gates_md}

Counts are over ALL labeled candidate rows; in production, liquidity/veto/
position filters remove many of these before the Claude gate, so these are
upper bounds on gate traffic.

## Segmentation check (report-only — pooled map is still the proposal)

### By strategy

{seg_md(seg['by_strategy'])}

### By status

{seg_md(seg['by_status'])}

**Verdict:** {r['segment_verdict']}

## Caveats (read before applying)

{r['caveats_md']}

## Provenance

- Fitter: `src/research/calibration_fit.py` (pre-specified protocol)
- Fit date: {r['fit_date']} | n = {r['n']} | days = {r['n_days']} | regime biases seen: bearish, bullish, neutral
- Readiness gates: passed {r['gates_ts']} (`logs/research/calibration_history.jsonl`)
"""
    path.write_text(md)


def write_proposed_yaml(path: Path, result: dict) -> None:
    r = result
    lines = [
        "# PROPOSED confidence calibration remap — NOT LIVE",
        "# Generated by src/research/calibration_fit.py (measure-and-propose only).",
        "# The live pipeline does NOT read this file. To apply, a human must review",
        f"# logs/research/calibration_proposal_{r['fit_date'].replace('-', '')}.md and",
        "# explicitly promote it (rename without .proposed + wire consumption).",
        "proposed: true",
        "applied: false",
        "provenance:",
        f"  fit_date: '{r['fit_date']}'",
        f"  n_candidates: {r['n']}",
        f"  n_days: {r['n_days']}",
        f"  first_day: '{r['days'][0]}'",
        f"  last_day: '{r['days'][-1]}'",
        "  target: cf_eod > 0 (counterfactual underlying_directional_proxy, not option P&L)",
        "  method: isotonic (PAV) win rate on local_confidence, pooled",
        f"  brier_raw: {r['brier_raw']:.4f}",
        f"  brier_cv_remapped: {r['brier_cv_remapped']:.4f}",
        f"  brier_cv_base_rate: {r['brier_cv_base_rate']:.4f}",
        "interpolation: linear   # between breakpoints; clamp outside range",
        "breakpoints:            # raw_conf -> calibrated_conf",
    ]
    for row in r["reliability_primary"]:
        lines.append(f"  - raw: {row['raw_conf']:.2f}")
        lines.append(f"    calibrated: {row['fitted']:.4f}")
    inv = r["gate_analysis"]["raw_conf_for_remapped_0.75"]
    lines += [
        "threshold_notes:",
        f"  remapped_0.75_raw_equivalent: {f'{inv:.4f}' if inv is not None else 'null  # unreachable — map max ' + format(r['gate_analysis']['map_max'], '.4f')}",
    ]
    path.write_text("\n".join(lines) + "\n")


# ── main ─────────────────────────────────────────────────────────────────────

SEGMENT_VERDICT_NOTE = (
    "pre-committed to the pooled map this round; segmentation is reported for "
    "review only and any per-strategy split waits for more data.")


def run(obs_dir: Path = OBS_DIR, fit_date: str | None = None,
        write: bool = True) -> dict:
    rows = load_candidates(obs_dir)
    if not rows:
        raise SystemExit("no labeled candidate rows found — nothing to fit")
    fit_date = fit_date or f"{datetime.now():%Y-%m-%d}"

    conf = np.array([r["local_confidence"] for r in rows])
    win = np.array([1.0 if r["cf_eod"] > WIN_EPS else 0.0 for r in rows])
    xs, emp, fitted, ns = fit_isotonic_map(rows, WIN_EPS)
    cv = loo_cv(rows, WIN_EPS)

    result = {
        "fit_date": fit_date,
        "n": len(rows),
        "days": cv["days"],
        "n_days": len(cv["days"]),
        "conf_mean": float(np.mean(conf)),
        "win_rate": float(np.mean(win)),
        "brier_raw": cv["brier_raw"],
        "brier_cv_remapped": cv["brier_cv_remapped"],
        "brier_cv_base_rate": cv["brier_cv_base_rate"],
        "reliability_primary": reliability_rows(rows, WIN_EPS, cv["cv_fitted_by_bin"]),
        "reliability_cost": reliability_rows(rows, WIN_COST_EPS),
        "gate_analysis": gate_analysis(rows, xs, fitted),
        "segments": segment_tables(rows, WIN_EPS),
        "gates_ts": "2026-07-01T17:00:04",
    }

    # segmentation verdict: is the pooled map defensible?
    seg = result["segments"]["by_strategy"]
    wr = {g: s["win_rate"] for g, s in seg.items()}
    spread = max(wr.values()) - min(wr.values()) if wr else 0.0
    if spread <= 0.05:
        verdict = (f"strategy-level pooled win rates differ by only {spread:.3f} "
                   f"({ {g: round(v, 3) for g, v in wr.items()} }); one pooled map is "
                   "defensible. ")
    else:
        verdict = (f"strategy-level pooled win rates differ by {spread:.3f} "
                   f"({ {g: round(v, 3) for g, v in wr.items()} }); a per-strategy map "
                   "is indicated ONCE more data accumulates, but ")
    result["segment_verdict"] = verdict + SEGMENT_VERDICT_NOTE

    n_act = result["segments"]["by_status"].get("actionable", {}).get("n", 0)
    result["caveats_md"] = "\n".join([
        f"1. **14 days, one regime stretch.** All {result['n']} labels come from a "
        "single ~3-week window (2026-06-11 … 2026-07-01). Intraday regime bias "
        "rotated (bearish/bullish/neutral all observed) but this is still one "
        "benign, low-VIX macro stretch — the map has never seen a vol shock.",
        "2. **Counterfactual proxy, not option P&L.** `cf_eod` is the "
        "direction-signed underlying move from next-bar open. It ignores premium "
        "paid, theta, IV, spreads, and slippage. A 'win' here can easily be an "
        "options loss; the cost-floor sensitivity table (>0.2%) is a crude nod "
        "to that, and even it is generous for short-dated options.",
        "3. **Correlated labels — n_effective << n_rows.** The same ticker/day "
        "setup is re-labeled across overlapping 5-minute cycles, and cf_eod for "
        "same-day same-ticker rows shares the same closing print. "
        f"{result['n']} rows over {result['n_days']} days on a ~15-name watchlist "
        "is closer to a few hundred independent observations at best. Per-bin "
        "confidence intervals computed from raw n would be badly overstated; "
        "none are shown for exactly that reason.",
        f"4. **Almost no actionable rows.** Only {n_act} rows ever reached "
        "posted status; the fit is dominated by skipped/vetoed candidates. The "
        "map calibrates the *signal generator*, not the end-to-end posted-trade "
        "funnel.",
        "5. **Refit required as regimes accumulate.** This map is a snapshot. It "
        "must be refit (same pre-specified protocol) as new labeled days — "
        "especially different vol regimes — arrive, and re-reviewed before any "
        "refreshed version is applied.",
    ])

    if write:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        md_path = RESEARCH_DIR / f"calibration_proposal_{fit_date.replace('-', '')}.md"
        write_proposal_md(md_path, result)
        write_proposed_yaml(PROPOSED_YAML, result)
        result["artifacts"] = [str(md_path), str(PROPOSED_YAML)]
    return result


def print_human(result: dict) -> None:
    r = result
    print(f"── CALIBRATION FIT (PROPOSAL ONLY) {r['fit_date']} ──")
    print(f"  n={r['n']} candidate rows, {r['n_days']} days "
          f"({r['days'][0]} … {r['days'][-1]})")
    print(f"  mean raw conf {r['conf_mean']:.3f} vs realized win rate {r['win_rate']:.3f}")
    print(f"  Brier raw {r['brier_raw']:.4f} | Brier remapped (LOO-CV) "
          f"{r['brier_cv_remapped']:.4f} | constant base rate (LOO-CV) "
          f"{r['brier_cv_base_rate']:.4f} | coin-flip@0.5 = 0.25")
    print(f"  {'raw':>6}{'n':>6}{'emp WR':>9}{'fitted':>9}{'CV fit':>9}")
    for row in r["reliability_primary"]:
        print(f"  {row['raw_conf']:>6.2f}{row['n']:>6}{row['empirical_win_rate']:>9.3f}"
              f"{row['fitted']:>9.3f}{row['cv_fitted']:>9.3f}")
    ga = r["gate_analysis"]
    inv = ga["raw_conf_for_remapped_0.75"]
    print(f"  raw >= 0.75 (current gate): {ga['raw_gate_0.75']['total']} rows "
          f"({ga['raw_gate_0.75']['per_day']}/day)")
    print("  remapped 0.75 <=> raw "
          + (f"{inv:.3f}" if inv is not None
             else f"UNREACHABLE (map max {ga['map_max']:.3f})"))
    for t, v in ga["remapped_gates"].items():
        eq = (f"raw≈{v['raw_conf_equivalent']:.3f}"
              if v["raw_conf_equivalent"] is not None else "unreachable")
        print(f"  remapped >= {t}: {v['total']} rows ({v['per_day']}/day, {eq})")
    print(f"  segmentation: {r['segment_verdict']}")
    for a in r.get("artifacts", []):
        print(f"  wrote {a}")
    print("  => PROPOSAL ONLY — nothing applied to the live path.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="print machine-readable JSON result to stdout")
    ap.add_argument("--no-write", action="store_true",
                    help="skip writing the proposal .md and .yaml.proposed artifacts")
    ap.add_argument("--fit-date", default=None,
                    help="override fit date (YYYY-MM-DD, default today)")
    args = ap.parse_args(argv)
    result = run(fit_date=args.fit_date, write=not args.no_write)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
