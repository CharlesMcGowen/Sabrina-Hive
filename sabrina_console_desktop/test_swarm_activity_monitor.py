"""
Unit test suite for SwarmActivityMonitor and live console monitor synchronization.
"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sabrina_console_desktop.tools.swarm_activity_monitor import SwarmActivityMonitor


class TestSwarmActivityMonitor(unittest.TestCase):

    def test_record_model_activity_updates_sessions_and_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "assistant.sqlite3"
            monitor = SwarmActivityMonitor(db_path=db_p)

            res = monitor.record_model_activity(
                model="gemini-4-carbon",
                role="critic_verifier",
                activity="Reviewing test coverage for log-odds booster",
                state="running",
                progress="Step 2/3",
                task="Quant Verification",
                app="Codex.exe",
                objective_id="obj_test_123",
                detail={"assertions": 5}
            )
            self.assertTrue(res["ok"])

            conn = sqlite3.connect(str(db_p))
            try:
                conn.row_factory = sqlite3.Row
                sess = conn.execute("SELECT * FROM assistant_sessions WHERE id=?", (res["run_id"],)).fetchone()
                self.assertIsNotNone(sess)
                self.assertEqual(sess["model"], "gemini-4-carbon")
                self.assertEqual(sess["activity"], "Reviewing test coverage for log-odds booster")
                self.assertEqual(sess["state"], "running")
                self.assertEqual(sess["progress"], "Step 2/3")

                # Verify handoff created for Codex.exe node card
                handoff = conn.execute("SELECT * FROM assistant_cross_app_handoffs WHERE app='Codex.exe'").fetchone()
                self.assertIsNotNone(handoff)
                self.assertEqual(handoff["status"], "running")
                self.assertIn("Reviewing test coverage", handoff["summary"])
            finally:
                conn.close()

    def test_model_review_step_context_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "assistant.sqlite3"
            monitor = SwarmActivityMonitor(db_path=db_p)

            with monitor.model_review_step(
                model="gemini-4-argon",
                role="execution_specialist",
                step_title="Calibrating decision trees",
                task="Tree Optimization",
                app="Antigravity.exe"
            ):
                time.sleep(0.05)

            conn = sqlite3.connect(str(db_p))
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM assistant_sessions ORDER BY updated DESC").fetchall()
                self.assertGreater(len(rows), 0)
                latest = rows[0]
                self.assertEqual(latest["state"], "completed")
                self.assertIn("Completed in", latest["activity"])
                self.assertIn("Calibrating decision trees", latest["activity"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
