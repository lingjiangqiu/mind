import json
import unittest
from pathlib import Path

from app.services.safety_evaluation import SAFETY_EVALUATION_CASES, evaluate_safety_suite


class SafetyEvaluationTests(unittest.TestCase):
    def test_fixed_suite_has_twelve_cases_and_all_contracts_pass(self):
        report = evaluate_safety_suite()

        self.assertEqual(len(SAFETY_EVALUATION_CASES), 12)
        self.assertEqual(report["cases"], 12)
        self.assertTrue(all(item["status"] == "PASS" for item in report["results"]))
        self.assertTrue(all(value == 1.0 for value in report["metrics"].values()))

    def test_runtime_report_does_not_publish_raw_inputs_or_identifiers(self):
        serialized = json.dumps(evaluate_safety_suite(), ensure_ascii=False)

        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("qa@example.com", serialized)
        self.assertNotIn("11010119900307001X", serialized)
        self.assertNotIn("我不想活了", serialized)

    def test_public_snapshot_matches_runtime_safety_suite(self):
        snapshot_path = Path(__file__).parents[1] / "app" / "static" / "mindbridge-quality.v2.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertEqual(snapshot["safetyEvaluation"], evaluate_safety_suite())


if __name__ == "__main__":
    unittest.main()
