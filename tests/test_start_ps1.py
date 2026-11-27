"""scripts/start.ps1 must wait long enough for a slow bind and must not kill a starting host.

Incident (22 Nov 2026): apply-update of 2a5b956b failed because start.ps1's 45s authenticated
health probe gave up while the launcher was still in a ~96s CPU-heavy startup, then called
Stop-Launcher and rolled the payload back. launcher-admin status already treats a loopback
HTTP 401 as healthy. There is no Pester harness in this repo, so these tests lock the script
source contract.
"""
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / "scripts" / "start.ps1").read_text()


class StartPs1HealthWait(unittest.TestCase):
    def test_default_health_wait_is_at_least_180_and_configurable(self):
        # Startup measured ~414 s at ~1300 journalled attempts (Nov 27 2026): the default
        # must leave a wide margin above that, so it is now at least 1200 s.
        m = re.search(r"\$HealthWaitSeconds\s*=\s*(\d+)\b", START)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 1200)
        self.assertIn("HYVV_START_HEALTH_WAIT_S", START)
        # The wait is a wall-clock deadline built from the configurable value (an iteration
        # count also spent the process query and probe time on every pass).
        self.assertRegex(START, r"\(Get-Date\)\.AddSeconds\(\$HealthWaitSeconds\)")
        self.assertRegex(START, r"while\s*\(.*\(Get-Date\)\s+-lt\s+\$healthDeadline")
        self.assertNotRegex(START, r"for\s+\(\s*\$i\s*=\s*0;\s*\$i\s+-lt\s+45\b")

    def test_loopback_401_or_200_is_healthy_without_a_token(self):
        self.assertIn("127.0.0.1", START)
        self.assertIn("/v1/health", START)
        self.assertIn("Test-LoopbackHealthy", START)
        self.assertIn("-eq 401", START)
        self.assertIn("-eq 200", START)
        # Must not require the API token for the start health probe (admin status does not).
        self.assertNotIn("orbit-api.token", START)
        self.assertNotRegex(START, r"Authorization")
        # Probe loopback, not the configured bind (which may not be reachable the same way).
        self.assertNotRegex(START, r'Invoke-WebRequest.*\$c\.bind')
        self.assertNotRegex(START, r'Invoke-RestMethod.*\$c\.bind')

    def test_does_not_stop_a_launcher_that_is_still_coming_up(self):
        # Stale cleanup before Start-Process may still call Stop-Launcher (required).
        start_at = START.index("Start-Process")
        tail = START[start_at:]
        self.assertIn("leaving it running", tail)
        # A comment may mention Stop-Launcher; the health-timeout path must not invoke it.
        self.assertNotRegex(tail, r'Stop-Launcher\s*\|')
        self.assertNotRegex(tail, r'(?m)^[^#\n]*\bStop-Launcher\b')
        self.assertIn("Get-LauncherProcesses", tail)

    def test_stale_cleanup_and_port_check_still_precede_start(self):
        self.assertIn("Stop-Launcher", START)
        self.assertIn("Get-NetTCPConnection", START)
        self.assertLess(START.index("Get-NetTCPConnection"), START.index("Start-Process"))


if __name__ == "__main__":
    unittest.main()
