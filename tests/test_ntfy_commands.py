"""Phone confirm/decline command tests (2026-07-02).

Covers: command parsing, token verification (fail-closed), apply paths,
poll no-op short-circuits (no topic / nothing pending), exactly-once state
advancement, and that queued notifications carry tokenized action buttons.

Run: .venv/bin/python -m unittest tests.test_ntfy_commands -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.control.ntfy_commands as nc
import src.engine.confirm as cf

REC = {"rec_id": "2026-07-02-P1", "ticker": "TLT",
       "trade": {"structure": "put_debit_spread", "direction": "short",
                 "net_debit": 0.16, "max_loss": 0.16, "max_profit": 0.84,
                 "expiration": "2026-07-10", "legs": []}}


class TmpQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (cf.CONFIRM_FILE, nc.STATE_FILE)
        cf.CONFIRM_FILE = base / "pending_confirmations.json"
        nc.STATE_FILE = base / "ntfy_cmd_state.json"

    def tearDown(self):
        cf.CONFIRM_FILE, nc.STATE_FILE = self._orig
        self.tmp.cleanup()

    def _queue(self):
        with patch("src.engine.notify.send") as send:
            entry = cf.queue_recommendation(dict(REC), 10)
        return entry, send


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(nc.parse_command("confirm R1 abc"), ("confirm", "R1", "abc"))
        self.assertEqual(nc.parse_command("decline R1 abc"), ("decline", "R1", "abc"))
        for bad in ("", "confirm R1", "buy R1 abc", "confirm R1 abc extra", None):
            self.assertIsNone(nc.parse_command(bad))


class TokenTest(TmpQueue):
    def test_notification_carries_tokenized_buttons(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "t-secret"}):
            entry, send = self._queue()
        actions = send.call_args.kwargs.get("actions") or send.call_args[0][4] \
            if len(send.call_args[0]) > 4 else send.call_args.kwargs["actions"]
        joined = " ".join(actions)
        self.assertIn("t-secret-cmd", joined)
        self.assertIn(f"confirm 2026-07-02-P1 {entry['confirm_token']}", joined)
        self.assertIn(f"decline 2026-07-02-P1 {entry['confirm_token']}", joined)

    def test_good_token_confirms(self):
        entry, _ = self._queue()
        with patch("src.engine.notify.send"):
            out = nc.apply_command("confirm", "2026-07-02-P1", entry["confirm_token"])
        self.assertTrue(out["applied"])
        self.assertEqual(cf._load()[0]["status"], "confirmed")

    def test_bad_token_rejected_and_alerted(self):
        self._queue()
        with patch("src.engine.notify.send") as send:
            out = nc.apply_command("confirm", "2026-07-02-P1", "WRONG")
        self.assertFalse(out["applied"])
        self.assertEqual(cf._load()[0]["status"], "pending")  # untouched
        self.assertIn("Rejected", send.call_args_list[0][0][0])

    def test_unknown_rec_rejected(self):
        out = nc.apply_command("confirm", "nope", "x")
        self.assertFalse(out["applied"])

    def test_decline_via_phone(self):
        entry, _ = self._queue()
        with patch("src.engine.notify.send"):
            out = nc.apply_command("decline", "2026-07-02-P1", entry["confirm_token"])
        self.assertTrue(out["applied"])
        self.assertEqual(cf._load()[0]["status"], "declined")


class PollTest(TmpQueue):
    def test_noop_without_topic(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(nc, "_fetch_messages") as fetch:
            self.assertEqual(nc.poll_and_apply(), [])
        fetch.assert_not_called()

    def test_noop_when_nothing_pending(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
                patch.object(nc, "_fetch_messages") as fetch:
            self.assertEqual(nc.poll_and_apply(), [])
        fetch.assert_not_called()

    def test_poll_applies_and_advances_state_once(self):
        entry, _ = self._queue()
        msgs = [{"id": "m1", "event": "message",
                 "message": f"confirm 2026-07-02-P1 {entry['confirm_token']}"}]
        with patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
                patch.object(nc, "_fetch_messages", return_value=msgs) as fetch, \
                patch("src.engine.notify.send"):
            events = nc.poll_and_apply()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["applied"])
        self.assertEqual(json.loads(nc.STATE_FILE.read_text())["last_id"], "m1")
        # second poll: entry is confirmed (not pending) -> short-circuits
        with patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
                patch.object(nc, "_fetch_messages") as fetch2:
            self.assertEqual(nc.poll_and_apply(), [])
        fetch2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
