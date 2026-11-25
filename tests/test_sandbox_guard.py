"""O-1: a session must never run on tradier_sandbox by accident."""
import unittest


class SandboxGuardTest(unittest.TestCase):
    def test_guard_text_present_and_refuses(self):
        src = open("src/scheduler.py").read()
        self.assertIn("REFUSING TO START", src)
        self.assertIn("allow_sandbox_session", src,
                      "an explicit override flag must exist")
        i = src.index("REFUSING TO START")
        self.assertIn("SystemExit", src[max(0, i-400):i],
                      "the guard must REFUSE (SystemExit), not warn")

    def test_override_flag_is_not_enabled_in_config(self):
        import yaml
        cfg = yaml.safe_load(open("config/config.yaml").read())
        self.assertFalse((cfg.get("agent") or {}).get("allow_sandbox_session", False),
                         "the sandbox override must stay OFF by default")
