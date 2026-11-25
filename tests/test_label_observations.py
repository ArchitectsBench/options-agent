"""Labeler tests (src/research/label_observations.py) — pins lesson L15:
a posted recommendation must yield exactly ONE candidate label row, never a
phantom 'skipped_below_threshold' twin (posted recs carry strategy only inside
trade{}, which broke the old (ticker, strategy) dedup key).

Hermetic (L6): obs file in a tmpdir; bar fetch + forward labels patched.

Run: .venv/bin/python -m unittest tests.test_label_observations -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research import label_observations as lo  # noqa: E402

LABELS = {"fwd_30m": 0.001, "fwd_60m": 0.002, "fwd_eod": 0.003}


def _record():
    return {
        "ts": "2026-07-08T10:00:00", "cycle": 1244,
        "signals": {"MSFT": {"last": 500.0}, "NFLX": {"last": 80.0}},
        "regime": {"bias": "bullish"},
        "local_llm": {"synthesis": {"candidates": [
            # same ticker as the POSTED rec — must NOT create a phantom row
            {"ticker": "MSFT", "strategy": "debit_spread", "final_confidence": 0.88},
            # genuinely below threshold, no rec entry — MUST create its row
            {"ticker": "NFLX", "strategy": "debit_spread", "final_confidence": 0.55},
        ]}},
        "recommendations": [{
            "rec_id": "2026-07-08-005", "ticker": "MSFT", "cycle": 1244,
            # note: strategy ONLY inside trade{} — the shape that broke the old key
            "trade": {"strategy": "debit_spread", "direction": "short"},
            "local_confidence": 0.88,
            "validation_chain": {"claude": {"status": "approved"}},
        }],
    }


class LabelDedupTest(unittest.TestCase):
    def _run(self):
        with tempfile.TemporaryDirectory() as td:
            obs = Path(td) / "2026-07-08.jsonl"
            obs.write_text(json.dumps(_record()) + "\n")
            with patch.object(lo, "batch_intraday",
                              return_value={"MSFT": object(), "NFLX": object()}), \
                 patch.object(lo, "forward_labels", return_value=dict(LABELS)):
                lo.label_file(obs, force=True)
            out = obs.parent / f"labels-{obs.stem}.jsonl"
            rows = [json.loads(l) for l in out.read_text().splitlines()]
            return [r for r in rows if r.get("kind") == "candidate"]

    def test_posted_rec_gets_exactly_one_row(self):
        cands = self._run()
        msft = [r for r in cands if r["ticker"] == "MSFT"]
        self.assertEqual(len(msft), 1, f"expected 1 MSFT row, got {msft}")
        self.assertEqual(msft[0]["status"], "posted_approved")
        self.assertEqual(msft[0]["rec_id"], "2026-07-08-005")

    def test_true_below_threshold_still_labeled(self):
        cands = self._run()
        nflx = [r for r in cands if r["ticker"] == "NFLX"]
        self.assertEqual(len(nflx), 1)
        self.assertEqual(nflx[0]["status"], "skipped_below_threshold")
        self.assertAlmostEqual(nflx[0]["local_confidence"], 0.55)


if __name__ == "__main__":
    unittest.main()
