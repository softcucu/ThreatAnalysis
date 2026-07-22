import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from threat_analysis_harness.main import build_parser, main, run


class RunThreatAnalysisCliTests(unittest.TestCase):
    def test_run_calls_third_party_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "artifacts"
            args = build_parser().parse_args(
                [
                    "--code-path",
                    "/repo",
                    "--output-path",
                    str(output_path),
                    "--product-mcp",
                    "product-mcp",
                    "--resume",
                    "--attack-modes",
                    '{"mode": ["intro", "skill-name"]}',
                ]
            )

            with patch(
                "threat_analysis_harness.main.run_threat_analysis",
                return_value={"result": True, "value_asset_path": "value.json"},
            ) as fake_run:
                result = run(args)

        self.assertEqual(result, {"result": True, "value_asset_path": "value.json"})
        fake_run.assert_called_once_with(
            code_path="/repo",
            output_path=str(output_path),
            is_resume=True,
            product_mcp="product-mcp",
            attack_modes={"mode": ["intro", "skill-name"]},
        )

    def test_main_prints_json_and_returns_failure_exit_code(self):
        stream = io.StringIO()
        with patch(
            "threat_analysis_harness.main.run_threat_analysis",
            return_value={"result": False, "reason": "failed"},
        ), contextlib.redirect_stdout(stream):
            exit_code = main(["--code-path", "/repo", "--output-path", "/out"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stream.getvalue()), {"result": False, "reason": "failed"})


if __name__ == "__main__":
    unittest.main()
