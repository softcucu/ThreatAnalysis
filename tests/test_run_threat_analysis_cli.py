import os
import tempfile
import unittest
from pathlib import Path

from scripts.run_threat_analysis import _latest_run_id


class RunThreatAnalysisCliTests(unittest.TestCase):
    def test_latest_run_id_uses_most_recent_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            old = runs / "old-run"
            new = runs / "new-run"
            old.mkdir(parents=True)
            new.mkdir()
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))

            self.assertEqual(_latest_run_id(tmp), "new-run")

    def test_latest_run_id_returns_none_without_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_latest_run_id(tmp))


if __name__ == "__main__":
    unittest.main()
